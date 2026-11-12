"""Lab uç noktaları — YALNIZ LAB_ENDPOINTS_ENABLED=true iken yüklenir.

Geliştiricilerin eşzamanlılık davranışlarını tek tıkla gözlemlemesi içindir:
  * /fanout        : sunucu tarafında N eşzamanlı HTTP isteği (tarayıcının host başına
                     ~6 bağlantı sınırını aşmak ve TÜM replikalara yük dağıtmak için).
                     Alt istekler traceparent ile aynı trace'e bağlanır.
  * /jobs/...      : dağıtık kilit + fencing token
  * /redis/failover: Sentinel üzerinden kontrollü failover
  * /events/burst  : asenkron pipeline'a toplu olay

Üretimde bu router hiç yüklenmez (Helm values: config.labEndpointsEnabled=false).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from opentelemetry.propagate import inject
from pydantic import BaseModel, Field, field_validator
from pymongo.errors import DuplicateKeyError
from redis.exceptions import LockError, RedisError

from app.api.deps import Ctx, SkuPath
from app.config import HOSTNAME
from app.context import request_id_var
from app.observability.metrics import LOCK_ACQUIRE
from app.observability.tracing import trace_id_hex, tracer
from app.redis_kit.rate_limit import rate_limit_dependency
from app.state import AppContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lab", tags=["lab"])

_FORWARDED_HEADERS = {"idempotency-key", "x-client-id"}
_JOB = "job:warm-cache"


# --------------------------------------------------------------------------- fanout
class FanoutRequest(BaseModel):
    method: Literal["GET", "POST", "PUT"] = "GET"
    # Hedef host config'ten gelir (SSRF yok); yalnız bu API'nin /api/ yolları çağrılabilir.
    path: str = Field(pattern=r"^/api/[A-Za-z0-9/_\-?=&.]*$", max_length=200)
    count: int = Field(50, ge=1, le=300)
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None  # string değerlerdeki "{i}" → istek sırası

    @field_validator("path")
    @classmethod
    def _no_recursion(cls, value: str) -> str:
        if value.startswith("/api/lab/fanout"):
            raise ValueError("fanout kendini çağıramaz")
        return value


def _render(value: Any, index: int) -> Any:
    if isinstance(value, str):
        return value.replace("{i}", str(index))
    if isinstance(value, dict):
        return {k: _render(v, index) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, index) for v in value]
    return value


def _response_code(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    detail = data.get("detail")
    if isinstance(detail, dict):
        return detail.get("code")
    return data.get("code") or data.get("status")


@router.post("/fanout")
async def fanout(request: FanoutRequest, ctx: Ctx) -> dict:
    headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARDED_HEADERS}
    parent_request_id = request_id_var.get() or uuid.uuid4().hex
    started = time.perf_counter()

    with tracer.start_as_current_span(
        "lab.fanout", attributes={"coform.operation": "lab.fanout", "lab.count": request.count}
    ):
        limits = httpx.Limits(max_connections=request.count, max_keepalive_connections=0)
        async with httpx.AsyncClient(
            base_url=ctx.settings.lab_fanout_base_url, timeout=20.0, limits=limits
        ) as client:

            async def one(index: int) -> dict[str, Any]:
                sub_headers = {**headers, "X-Request-ID": f"{parent_request_id}-{index}"}
                inject(sub_headers)  # traceparent → alt istekler bu trace'in çocuğu olur
                t0 = time.perf_counter()
                try:
                    response = await client.request(
                        request.method,
                        request.path,
                        json=_render(request.body, index) if request.body is not None else None,
                        headers=sub_headers,
                    )
                except httpx.HTTPError as exc:
                    return {"i": index, "status": 0, "code": type(exc).__name__}
                return {
                    "i": index,
                    "status": response.status_code,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "served_by": response.headers.get("x-served-by"),
                    "cache": response.headers.get("x-cache"),
                    "replayed": response.headers.get("idempotent-replayed") == "true",
                    "code": _response_code(response),
                }

            results = await asyncio.gather(*(one(i) for i in range(request.count)))
        trace_id = trace_id_hex()

    def tally(field: str) -> dict[str, int]:
        return dict(Counter(str(r.get(field)) for r in results))

    return {
        "count": request.count,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "trace_id": trace_id,
        "summary": {
            "status": tally("status"),
            "served_by": tally("served_by"),
            "cache": tally("cache"),
            "code": tally("code"),
            "replayed": sum(1 for r in results if r.get("replayed")),
        },
        "results": results,
    }


# --------------------------------------------------------------------------- cache
@router.post("/cache/{sku}/flush")
async def flush_cache(sku: SkuPath, ctx: Ctx) -> dict:
    await ctx.redis.primary.unlink(
        ctx.keys.product(sku), ctx.keys.product_list(), ctx.keys.lab_db_loads(sku)
    )
    return {"sku": sku, "flushed": True}


@router.get("/cache/{sku}/db-loads")
async def db_loads(sku: SkuPath, ctx: Ctx) -> dict:
    value = await ctx.redis.primary.get(ctx.keys.lab_db_loads(sku))
    return {"sku": sku, "db_loads": int(value or 0)}


# --------------------------------------------------------------------------- flash sale
class FlashResetRequest(BaseModel):
    stock: int = Field(10, ge=0, le=100_000)


@router.post("/flash-sale/{sku}/reset")
async def reset_flash_sale(sku: SkuPath, request: FlashResetRequest, ctx: Ctx) -> dict:
    # MULTI/EXEC: iki anahtar aynı {hash-tag} → Cluster'da da aynı slot.
    async with ctx.redis.primary.pipeline(transaction=True) as pipe:
        pipe.set(ctx.keys.flash_stock(sku), request.stock, ex=3600)
        pipe.delete(ctx.keys.flash_buyers(sku))
        await pipe.execute()
    return {"sku": sku, "stock": request.stock}


# --------------------------------------------------------------------------- rate limit
@router.get("/rate-limited", dependencies=[Depends(rate_limit_dependency("lab_rate_limiter"))])
async def rate_limited() -> dict:
    return {"ok": True, "served_by": HOSTNAME}


# --------------------------------------------------------------------------- distributed lock
@router.post("/jobs/warm-cache", status_code=202, response_model=None)
async def start_warm_cache(ctx: Ctx) -> dict | JSONResponse:
    redis = ctx.redis.primary
    # Kilit TTL'li: sahibi çökerse kilit kendiliğinden açılır (deadlock yok).
    lock = redis.lock(ctx.keys.lock(_JOB), timeout=10, blocking=False, thread_local=False)
    if not await lock.acquire():
        LOCK_ACQUIRE.labels(_JOB, "busy").inc()
        return JSONResponse({"code": "job_already_running", "served_by": HOSTNAME}, 409)
    LOCK_ACQUIRE.labels(_JOB, "acquired").inc()
    try:
        # Fencing token: her kilit alımında artan sayı. Yazma tarafı eski token'ı reddeder.
        fence = int(await redis.incr(ctx.keys.fence(_JOB)))
    except RedisError:
        await lock.release()
        raise
    ctx.spawn(_warm_cache(ctx, lock, fence), name="warm-cache")
    return {"status": "started", "fencing_token": fence, "served_by": HOSTNAME}


@router.get("/jobs/warm-cache")
async def warm_cache_status(ctx: Ctx) -> dict:
    lock_ttl_ms = await ctx.redis.primary.pttl(ctx.keys.lock(_JOB))
    last_run = await ctx.db["job_runs"].find_one({"_id": "warm-cache"})
    if last_run and isinstance(last_run.get("finished_at"), datetime):
        last_run["finished_at"] = last_run["finished_at"].isoformat()
    return {"running": lock_ttl_ms > 0, "lock_ttl_ms": max(lock_ttl_ms, 0), "last_run": last_run}


async def _warm_cache(ctx: AppContext, lock: Any, fence: int) -> None:
    with tracer.start_as_current_span(
        "job.warm_cache", attributes={"coform.operation": "job.warm_cache", "job.fence": fence}
    ):
        warmed = 0
        try:
            for product in await ctx.products.list():
                await ctx.product_cache.put(ctx.keys.product(product["sku"]), product)
                await asyncio.sleep(0.3)  # "uzun" iş adımı
                await lock.reacquire()  # TTL'i uzat; kilit kaybedildiyse LockError
                warmed += 1
            await _record_job_run(ctx, fence, warmed)
        except LockError:
            LOCK_ACQUIRE.labels(_JOB, "lost").inc()
            logger.warning("lock lost during job; aborting", extra={"fence": fence})
        except Exception:
            logger.exception("warm-cache job failed", extra={"fence": fence})
        finally:
            try:
                await lock.release()
            except (LockError, RedisError):
                pass


async def _record_job_run(ctx: AppContext, fence: int, warmed: int) -> None:
    """Fencing: yalnız daha BÜYÜK token'lı yazar kazanır.

    Kilidi GC duraklaması/ağ kopması yüzünden kaybetmiş "eski" bir çalışan geç
    kalıp yazmaya çalışırsa filtre eşleşmez → upsert insert dener → _id çakışır
    → DuplicateKeyError → eski yazma reddedilir.
    """
    try:
        await ctx.db["job_runs"].update_one(
            {"_id": "warm-cache", "fence": {"$lt": fence}},
            {
                "$set": {
                    "fence": fence,
                    "warmed": warmed,
                    "served_by": HOSTNAME,
                    "finished_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        logger.info("warm-cache job finished", extra={"fence": fence, "warmed": warmed})
    except DuplicateKeyError:
        logger.warning("stale fencing token rejected", extra={"fence": fence})


# --------------------------------------------------------------------------- redis HA
@router.post("/redis/failover", status_code=202, response_model=None)
async def trigger_failover(ctx: Ctx) -> dict | JSONResponse:
    if ctx.redis.sentinel is None:
        return JSONResponse({"code": "not_sentinel_mode"}, 400)
    last_error = "no_sentinel"
    for sentinel_client in ctx.redis.sentinel.sentinels:
        kwargs = sentinel_client.connection_pool.connection_kwargs
        address = f"{kwargs.get('host')}:{kwargs.get('port')}"
        try:
            await sentinel_client.sentinel_failover(ctx.redis.master_name)
        except RedisError as exc:
            last_error = type(exc).__name__
            continue
        logger.warning("redis failover requested", extra={"sentinel": address})
        return {"status": "failover_requested", "via": address}
    return JSONResponse({"code": "failover_failed", "error_type": last_error}, 503)


# --------------------------------------------------------------------------- events
class BurstRequest(BaseModel):
    count: int = Field(500, ge=1, le=20_000)


@router.post("/events/burst")
async def events_burst(request: BurstRequest, ctx: Ctx) -> dict:
    burst_id = uuid.uuid4().hex[:8]
    accepted = sum(
        ctx.publisher.publish(
            "lab.burst", {"burst_id": burst_id, "seq": i}, propagate_trace=False
        )
        for i in range(request.count)
    )
    return {"burst_id": burst_id, "accepted": accepted, "dropped": request.count - accepted}
