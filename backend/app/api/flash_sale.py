"""Flash-sale: eşzamanlı isteklerde stok tutarlılığı + Idempotency-Key.

`mode=atomic` (varsayılan): kontrol + düşüm + alıcı kaydı TEK Lua script'inde.
`mode=naive` (yalnız lab): GET → kontrol → SET. Eşzamanlı isteklerde fazla satış
yapar; farkı göstermek için vardır, gerçek kodda böyle yazılmaz.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from app.api.deps import Ctx, SkuPath
from app.observability.metrics import FLASH_SALE_RESERVATIONS, IDEMPOTENCY_REQUESTS
from app.observability.tracing import tracer
from app.state import AppContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/flash-sale", tags=["flash-sale"])

_IDEMPOTENCY_SCOPE = "flash-reserve"


class ReserveRequest(BaseModel):
    user_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    quantity: int = Field(1, ge=1, le=5)


@router.get("/{sku}")
async def status(sku: SkuPath, ctx: Ctx) -> dict:
    redis = ctx.redis.primary
    async with redis.pipeline(transaction=False) as pipe:
        pipe.get(ctx.keys.flash_stock(sku))
        pipe.scard(ctx.keys.flash_buyers(sku))
        pipe.ttl(ctx.keys.flash_stock(sku))
        stock, buyers, ttl = await pipe.execute()
    if stock is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    return {"sku": sku, "stock": int(stock), "buyers": buyers, "ttl_seconds": ttl}


@router.post("/{sku}/reserve")
async def reserve(
    sku: SkuPath,
    body: ReserveRequest,
    ctx: Ctx,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ] = None,
    mode: Literal["atomic", "naive"] = "atomic",
) -> JSONResponse:
    if mode == "naive" and not ctx.settings.lab_endpoints_enabled:
        raise HTTPException(status_code=400, detail={"code": "naive_mode_disabled"})

    fingerprint = hashlib.sha256(
        f"{sku}|{body.user_id}|{body.quantity}|{mode}".encode()
    ).hexdigest()

    if idempotency_key:
        outcome = await ctx.idempotency.begin(_IDEMPOTENCY_SCOPE, idempotency_key, fingerprint)
        IDEMPOTENCY_REQUESTS.labels(_IDEMPOTENCY_SCOPE, outcome.kind).inc()
        if outcome.kind == "replay":
            return JSONResponse(
                outcome.body, outcome.status_code or 200, headers={"Idempotent-Replayed": "true"}
            )
        if outcome.kind == "in_progress":
            return JSONResponse(
                {"code": "request_in_progress"}, 409, headers={"Retry-After": "1"}
            )
        if outcome.kind == "mismatch":
            return JSONResponse({"code": "idempotency_key_reused"}, 422)

    try:
        handler = _reserve_atomic if mode == "atomic" else _reserve_naive
        status_code, payload = await handler(ctx, sku, body)
    except Exception:
        if idempotency_key:
            await _safe_abort(ctx, idempotency_key)
        raise

    if idempotency_key:
        await ctx.idempotency.complete(
            _IDEMPOTENCY_SCOPE, idempotency_key, fingerprint, status_code, payload
        )
    return JSONResponse(payload, status_code)


async def _reserve_atomic(
    ctx: AppContext, sku: str, body: ReserveRequest
) -> tuple[int, dict[str, Any]]:
    with tracer.start_as_current_span(
        "flash_sale.reserve",
        attributes={"coform.operation": "flash_sale.reserve", "flash_sale.mode": "atomic"},
    ):
        code, remaining = await ctx.flash_reserve(
            keys=[ctx.keys.flash_stock(sku), ctx.keys.flash_buyers(sku)],
            args=[body.user_id, body.quantity],
        )
    return _result(ctx, sku, body, int(code), int(remaining), mode="atomic")


async def _reserve_naive(
    ctx: AppContext, sku: str, body: ReserveRequest
) -> tuple[int, dict[str, Any]]:
    redis = ctx.redis.primary
    stock_key, buyers_key = ctx.keys.flash_stock(sku), ctx.keys.flash_buyers(sku)
    raw = await redis.get(stock_key)  # 1) oku
    if raw is None:
        return _result(ctx, sku, body, -2, 0, mode="naive")
    stock = int(raw)
    if await redis.sismember(buyers_key, body.user_id):
        return _result(ctx, sku, body, -1, stock, mode="naive")
    await asyncio.sleep(0.005)  # 2) "iş mantığı" — bu arada diğer istekler de aynı stoğu okur
    if stock < body.quantity:
        return _result(ctx, sku, body, 0, stock, mode="naive")
    await redis.set(stock_key, stock - body.quantity, keepttl=True)  # 3) yaz (kayıp güncelleme!)
    await redis.sadd(buyers_key, body.user_id)
    await redis.expire(buyers_key, 3600)
    return _result(ctx, sku, body, 1, stock - body.quantity, mode="naive")


def _result(
    ctx: AppContext, sku: str, body: ReserveRequest, code: int, remaining: int, *, mode: str
) -> tuple[int, dict[str, Any]]:
    if code == 1:
        reservation_id = uuid.uuid4().hex
        FLASH_SALE_RESERVATIONS.labels(mode, "reserved").inc()
        # event_id = reservation_id → tekrar yayınlansa bile Mongo'da tek kayıt.
        ctx.publisher.publish(
            "flash_sale.reserved",
            {"sku": sku, "user_id": body.user_id, "quantity": body.quantity, "mode": mode},
            event_id=reservation_id,
        )
        return 201, {"status": "reserved", "reservation_id": reservation_id, "remaining": remaining}
    if code == 0:
        FLASH_SALE_RESERVATIONS.labels(mode, "sold_out").inc()
        return 409, {"code": "sold_out", "remaining": remaining}
    if code == -1:
        FLASH_SALE_RESERVATIONS.labels(mode, "already_purchased").inc()
        return 409, {"code": "already_purchased", "remaining": remaining}
    FLASH_SALE_RESERVATIONS.labels(mode, "campaign_not_found").inc()
    return 404, {"code": "campaign_not_found"}


async def _safe_abort(ctx: AppContext, idempotency_key: str) -> None:
    try:
        await ctx.idempotency.abort(_IDEMPOTENCY_SCOPE, idempotency_key)
    except RedisError:
        logger.warning("idempotency abort failed; key will expire by TTL")
