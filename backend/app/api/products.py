"""Ürün API'si — cache-aside okuma, delete-after-write invalidation, audit event."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from app.api.deps import Ctx, SkuPath
from app.state import AppContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/products", tags=["products"])


class ProductUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    price: float | None = Field(None, gt=0, lt=1_000_000)


@router.get("")
async def list_products(ctx: Ctx, response: Response) -> dict:
    items, status = await ctx.product_cache.get_or_load(
        ctx.keys.product_list(), ctx.products.list, ttl=30
    )
    response.headers["X-Cache"] = status
    return {"items": items}


@router.get("/{sku}")
async def get_product(sku: SkuPath, ctx: Ctx, response: Response, protect: bool = True) -> dict:
    # `protect=false` yalnız lab ortamında anlamlıdır (stampede'i göstermek için).
    if not ctx.settings.lab_endpoints_enabled:
        protect = True

    async def loader() -> dict | None:
        if ctx.settings.lab_endpoints_enabled:
            await _count_lab_db_load(ctx, sku)
        return await ctx.products.get(sku)

    product, status = await ctx.product_cache.get_or_load(
        ctx.keys.product(sku), loader, protect=protect
    )
    response.headers["X-Cache"] = status
    if product is None:
        raise HTTPException(status_code=404, detail={"code": "product_not_found"})
    return product


@router.put("/{sku}")
async def update_product(sku: SkuPath, body: ProductUpdate, ctx: Ctx) -> dict:
    if body.name is None and body.price is None:
        raise HTTPException(status_code=422, detail={"code": "nothing_to_update"})
    product = await ctx.products.update(sku, name=body.name, price=body.price)
    if product is None:
        raise HTTPException(status_code=404, detail={"code": "product_not_found"})

    # 1) Kaynağı güncelle  2) cache'i SİL (set etme)  3) olayı asenkron yayınla
    await ctx.product_cache.invalidate(ctx.keys.product(sku), ctx.keys.product_list())
    ctx.publisher.publish(
        "product.updated",
        {"sku": sku, "price": product.get("price"), "version": product.get("version")},
        event_id=f"product.updated:{sku}:{product.get('version')}",
    )
    logger.info("product updated", extra={"sku": sku, "version": product.get("version")})
    return product


async def _count_lab_db_load(ctx: AppContext, sku: str) -> None:
    """Lab: tüm replikalar genelinde kaynağa kaç kez gidildiğini sayar."""
    key = ctx.keys.lab_db_loads(sku)
    try:
        async with ctx.redis.primary.pipeline(transaction=False) as pipe:
            pipe.incr(key)
            pipe.expire(key, 3600)
            await pipe.execute()
    except RedisError:
        pass
