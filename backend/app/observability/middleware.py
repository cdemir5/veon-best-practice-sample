"""HTTP gözlemlenebilirlik middleware'i (saf ASGI — BaseHTTPMiddleware DEĞİL).

Her istek için:
  * X-Request-ID'yi alır (nginx üretir) ya da üretir → ContextVar → log/event
  * Yanıta X-Request-ID, X-Trace-Id, X-Served-By header'larını ekler
  * RED metrikleri: route ŞABLONU ile (/api/products/{sku}) — ham path ile değil
  * Tek satır access log (başlangıç + bitiş diye iki log değil), trace id ile

Neden saf ASGI? BaseHTTPMiddleware streaming yanıtlarda ve ContextVar
yayılımında sorun çıkarır, ayrıca her istekte ek task/kopya maliyeti vardır.
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from opentelemetry import trace
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import HOSTNAME
from app.context import request_id_var
from app.observability.metrics import (
    HTTP_IN_PROGRESS,
    HTTP_LATENCY,
    HTTP_REQUESTS,
    current_exemplar,
)

access_logger = logging.getLogger("app.access")

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_UNMATCHED_ROUTE = "__unmatched__"  # 404 tarayıcı/bot trafiği label patlatmasın
_QUIET_ROUTES = frozenset({"/healthz", "/readyz", "/api/system/heartbeat"})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        method = scope["method"]
        incoming_id = _header(scope, b"x-request-id")
        request_id = (
            incoming_id
            if incoming_id and _REQUEST_ID_PATTERN.match(incoming_id)
            else uuid.uuid4().hex
        )
        token = request_id_var.set(request_id)
        status_code = 500
        HTTP_IN_PROGRESS.labels(method).inc()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Served-By"] = HOSTNAME
                span_context = trace.get_current_span().get_span_context()
                if span_context.is_valid:
                    headers["X-Trace-Id"] = format(span_context.trace_id, "032x")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - started
            route_obj = scope.get("route")
            route = getattr(route_obj, "path", None) or _UNMATCHED_ROUTE
            HTTP_IN_PROGRESS.labels(method).dec()
            HTTP_REQUESTS.labels(method, route, str(status_code)).inc()
            HTTP_LATENCY.labels(method, route).observe(duration, exemplar=current_exemplar())
            if route not in _QUIET_ROUTES or status_code >= 500:
                level = (
                    logging.ERROR
                    if status_code >= 500
                    else logging.WARNING
                    if status_code >= 400
                    else logging.INFO
                )
                # Query string, body, header, client IP loglanmaz (PII/secret riski).
                access_logger.log(
                    level,
                    "http_request",
                    extra={
                        "http_method": method,
                        "http_route": route,
                        "http_status": status_code,
                        "duration_ms": round(duration * 1000, 2),
                    },
                )
            request_id_var.reset(token)
