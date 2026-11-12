"""İstek kapsamındaki korelasyon değerleri (asyncio-safe ContextVar)."""
from __future__ import annotations

from contextvars import ContextVar

# nginx'in ürettiği / istemcinin gönderdiği X-Request-ID. Log ve event'lere eklenir.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# `tracing.untraced()` bloğu içinde True olur; log'lara sahte trace id yazılmasını engeller.
untraced_var: ContextVar[bool] = ContextVar("untraced", default=False)
