"""API giriş noktası.

Çalıştırma (container CMD):
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log --timeout-graceful-shutdown 20

Pod başına TEK process (uvicorn worker sayısı 1). Ölçekleme replika/HPA ile yapılır:
  * prometheus_client multiprocess modu ve dosya paylaşımı gerekmez,
  * OTel provider fork sorunları yaşanmaz,
  * Kubernetes kaynak limitleri ve probe'lar tek process'i doğru ölçer.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError
from redis.exceptions import RedisError

from app.api import flash_sale, lab, products, system
from app.config import get_settings
from app.context import request_id_var
from app.observability.logging import setup_logging
from app.observability.metrics import APP_INFO, REDIS_ERRORS, start_metrics_server
from app.observability.middleware import ObservabilityMiddleware
from app.observability.tracing import mark_span_error, setup_tracing, shutdown_tracing
from app.state import build_context

settings = get_settings()
setup_logging(
    level=settings.log_level,
    service=settings.otel_service_name,
    version=settings.app_version,
    environment=settings.deployment_environment,
    queue_size=settings.log_queue_size,
)
tracer_provider = setup_tracing(settings, service_name=settings.otel_service_name)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    start_metrics_server(settings.metrics_port)
    APP_INFO.labels("api", settings.app_version, settings.deployment_environment).set(1)
    ctx = build_context(settings)
    app.state.ctx = ctx
    await ctx.publisher.start()
    logger.info(
        "api started",
        extra={
            "redis_mode": ctx.redis.mode,
            "lab_endpoints": settings.lab_endpoints_enabled,
            "tracing": tracer_provider is not None,
        },
    )
    try:
        yield
    finally:
        # SIGTERM → uvicorn yeni bağlantı almayı keser, açık istekleri bitirir, sonra burası.
        logger.info("api shutting down")
        await ctx.publisher.stop(timeout=5)  # bellek kuyruğundaki olayları boşalt
        await ctx.close()
        shutdown_tracing(tracer_provider)  # kuyruktaki span'leri gönder


app = FastAPI(
    title="VEON Best-Practice Sample API",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)
app.add_middleware(ObservabilityMiddleware)


def _dependency_unavailable(dependency: str, exc: Exception) -> JSONResponse:
    mark_span_error(exc)
    logger.warning(
        "dependency unavailable",
        extra={"dependency": dependency, "error_type": type(exc).__name__},
    )
    return JSONResponse(
        {"code": "dependency_unavailable", "dependency": dependency,
         "request_id": request_id_var.get()},
        status_code=503,
        headers={"Retry-After": "2"},
    )


@app.exception_handler(RedisError)
async def _redis_error_handler(_: Request, exc: RedisError) -> JSONResponse:
    REDIS_ERRORS.labels("request", type(exc).__name__).inc()
    return _dependency_unavailable("redis", exc)


@app.exception_handler(PyMongoError)
async def _mongo_error_handler(_: Request, exc: PyMongoError) -> JSONResponse:
    return _dependency_unavailable("mongodb", exc)


app.include_router(system.router)
app.include_router(products.router)
app.include_router(flash_sale.router)
if settings.lab_endpoints_enabled:
    app.include_router(lab.router)

if tracer_provider is not None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        # Sık çağrılan sağlık/polling uçları trace üretmez (maliyet + gürültü).
        excluded_urls="healthz,readyz,api/system/heartbeat,api/system/redis,api/system/pipeline",
        # ASGI "http send/receive" iç span'leri gürültüdür.
        exclude_spans=["receive", "send"],
    )
