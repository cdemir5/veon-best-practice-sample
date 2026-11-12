"""Sağlık kontrolleri ve operasyonel durum uç noktaları.

  /healthz (liveness)  : yalnız process'in yaşadığını söyler. Bağımlılık KONTROL ETMEZ;
                         Redis düştü diye pod'ları yeniden başlatmak sorunu çözmez,
                         büyütür (restart fırtınası).
  /readyz  (readiness) : "trafik alabilir miyim?" — ZORUNLU bağımlılık (Mongo) kontrol
                         edilir. Redis bu serviste "degrade edilebilir" bağımlılıktır
                         (cache bypass), bu yüzden durumu raporlanır ama 503 üretmez.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError, ResponseError

from app.api.deps import Ctx
from app.config import HOSTNAME
from app.observability.tracing import untraced
from app.state import AppContext

router = APIRouter(tags=["system"])


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(ctx: Ctx) -> JSONResponse:
    with untraced():
        mongo_ok, redis_ok = await asyncio.gather(_mongo_ping(ctx), _redis_ping(ctx))
    body = {"mongo": mongo_ok, "redis": redis_ok, "served_by": HOSTNAME}
    return JSONResponse(body, status_code=200 if mongo_ok else 503)


@router.get("/api/system/info")
async def info(ctx: Ctx) -> dict:
    s = ctx.settings
    return {
        "service": s.otel_service_name,
        "version": s.app_version,
        "environment": s.deployment_environment,
        "served_by": HOSTNAME,
        "redis_mode": ctx.redis.mode,
        "lab_endpoints": s.lab_endpoints_enabled,
        "tracing": not s.otel_sdk_disabled,
    }


@router.get("/api/system/heartbeat", response_model=None)
async def heartbeat(ctx: Ctx) -> dict | JSONResponse:
    """Frontend saniyede bir çağırır → failover sırasında kesinti penceresi görünür."""
    started = time.perf_counter()
    with untraced():
        try:
            counter = await ctx.redis.primary.incr(ctx.keys.heartbeat())
            master = await _master_address(ctx)
        except RedisError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    # Redis hata kodu: NOREPLICAS, READONLY, LOADING, MASTERDOWN ...
                    "error_code": str(exc).split(" ", 1)[0][:32] or None,
                    "latency_ms": _ms(started),
                    "served_by": HOSTNAME,
                },
                status_code=503,
            )
    return {
        "ok": True,
        "counter": counter,
        "master": master,
        "latency_ms": _ms(started),
        "served_by": HOSTNAME,
    }


@router.get("/api/system/redis")
async def redis_topology(ctx: Ctx) -> dict:
    with untraced():
        result: dict[str, Any] = {"mode": ctx.redis.mode, "served_by": HOSTNAME}
        if ctx.redis.sentinel is not None:
            result["master_name"] = ctx.redis.master_name
            result["sentinels"] = await _sentinel_views(ctx)
            try:
                result["master"] = await _master_address(ctx)
                replicas = await ctx.redis.sentinel.discover_slaves(ctx.redis.master_name)
                result["replicas"] = [f"{host}:{port}" for host, port in replicas]
            except RedisError as exc:
                result["discovery_error"] = type(exc).__name__
        try:
            primary = ctx.redis.primary
            replication = await primary.info("replication")
            memory = await primary.info("memory")
            stats = await primary.info("stats")
            clients = await primary.info("clients")
        except RedisError as exc:
            result["primary_error"] = type(exc).__name__
            return result
        hits, misses = stats.get("keyspace_hits", 0), stats.get("keyspace_misses", 0)
        result["primary"] = {
            "role": replication.get("role"),
            "connected_replicas": replication.get("connected_slaves"),
            "replication_offset": replication.get("master_repl_offset"),
            "replicas": [
                {
                    "address": f"{value.get('ip')}:{value.get('port')}",
                    "state": value.get("state"),
                    "offset_lag": replication.get("master_repl_offset", 0)
                    - int(value.get("offset", 0)),
                    "lag_seconds": value.get("lag"),
                }
                for key, value in replication.items()
                if key.startswith("slave") and isinstance(value, dict)
            ],
            "used_memory": memory.get("used_memory_human"),
            "maxmemory_policy": memory.get("maxmemory_policy"),
            "connected_clients": clients.get("connected_clients"),
            "ops_per_sec": stats.get("instantaneous_ops_per_sec"),
            "keyspace_hit_ratio": round(hits / (hits + misses), 3) if hits + misses else None,
        }
        return result


@router.get("/api/system/pipeline")
async def pipeline_status(ctx: Ctx) -> dict:
    with untraced():
        redis = ctx.redis.primary
        stream, dlq = ctx.keys.events_stream(), ctx.keys.events_dlq()
        result: dict[str, Any] = {
            "served_by": HOSTNAME,
            "publisher": {
                "queue_size": ctx.publisher.queue_size(),
                "queue_capacity": ctx.publisher.queue_capacity(),
                **ctx.publisher.stats,
            },
        }
        try:
            result["stream"] = {"name": stream, "length": await redis.xlen(stream)}
            result["dlq_length"] = await redis.xlen(dlq)
            try:
                groups = await redis.xinfo_groups(stream)
            except ResponseError:  # stream henüz yok
                groups = []
            result["groups"] = [
                {
                    "name": g.get("name"),
                    "consumers": g.get("consumers"),
                    "pending": g.get("pending"),
                    "lag": g.get("lag"),
                    "last_delivered_id": g.get("last-delivered-id"),
                }
                for g in groups
            ]
        except RedisError as exc:
            result["redis_error"] = type(exc).__name__

        cursor = (
            ctx.db["events"]
            .find(
                {},
                {"_id": 0, "event_id": 1, "type": 1, "source": 1, "ts": 1,
                 "persisted_at": 1, "trace_id": 1, "payload": 1},
            )
            .sort("ts", -1)
            .limit(10)
        )
        recent = []
        async for doc in cursor:
            ts, persisted = doc.get("ts"), doc.get("persisted_at")
            if isinstance(ts, datetime) and isinstance(persisted, datetime):
                doc["pipeline_latency_ms"] = round((persisted - ts).total_seconds() * 1000, 1)
            for key in ("ts", "persisted_at"):
                if isinstance(doc.get(key), datetime):
                    doc[key] = doc[key].isoformat()
            recent.append(doc)
        result["recent_events"] = recent
        result["persisted_total"] = await ctx.db["events"].estimated_document_count()
        return result


# --------------------------------------------------------------------------- helpers
async def _mongo_ping(ctx: AppContext) -> bool:
    try:
        await asyncio.wait_for(ctx.mongo.admin.command("ping"), timeout=1.0)
        return True
    except Exception:
        return False


async def _redis_ping(ctx: AppContext) -> bool:
    try:
        return bool(await asyncio.wait_for(ctx.redis.primary.ping(), timeout=1.0))
    except Exception:
        return False


async def _master_address(ctx: AppContext) -> str | None:
    if ctx.redis.sentinel is None:
        return None
    host, port = await ctx.redis.sentinel.discover_master(ctx.redis.master_name)
    return f"{host}:{port}"


async def _sentinel_views(ctx: AppContext) -> list[dict[str, Any]]:
    views = []
    for sentinel_client in ctx.redis.sentinel.sentinels:
        kwargs = sentinel_client.connection_pool.connection_kwargs
        address = f"{kwargs.get('host')}:{kwargs.get('port')}"
        try:
            state = await sentinel_client.sentinel_master(ctx.redis.master_name)
        except RedisError as exc:
            views.append({"address": address, "ok": False, "error_type": type(exc).__name__})
            continue
        views.append(
            {
                "address": address,
                "ok": True,
                "sees_master": f"{state.get('ip')}:{state.get('port')}",
                "flags": state.get("flags"),
                "quorum": state.get("quorum"),
                "other_sentinels": state.get("num-other-sentinels"),
                "replicas": state.get("num-slaves"),
            }
        )
    return views


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
