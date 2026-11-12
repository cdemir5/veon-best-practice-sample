"""Dağıtık rate limiter (sliding window log, Lua ile atomik).

  * Sayaç Redis'te olduğu için limit TÜM replikalar için ortaktır
    (pod başına bellek içi limiter, replika sayısı kadar gevşer).
  * Zaman Redis sunucusundan okunur (TIME) → pod saat farkları etkilemez.
  * Redis erişilemezse FAIL-OPEN: isteğe izin verilir, metrik artar.
    (Güvenlik amaçlı limitlerde — ör. login brute-force — fail-closed seçilebilir.)
  * İstemci kimliği anahtara özetlenerek yazılır (kişisel veri sızmaz).
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.observability.metrics import RATE_LIMIT_DECISIONS, REDIS_ERRORS
from app.redis_kit.keys import Keys
from app.redis_kit.scripts import RATE_LIMIT_SLIDING_WINDOW

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_ms: int


class SlidingWindowRateLimiter:
    def __init__(
        self, redis: Redis, keys: Keys, *, name: str, limit: int, window_seconds: int
    ) -> None:
        self._keys = keys
        self.name = name
        self.limit = limit
        self._window_ms = window_seconds * 1000
        self._script = redis.register_script(RATE_LIMIT_SLIDING_WINDOW)

    async def hit(self, client_id: str) -> RateLimitResult:
        key = self._keys.rate_limit(self.name, client_id)
        try:
            allowed, remaining, retry_after = await self._script(
                keys=[key], args=[self._window_ms, self.limit, secrets.token_hex(4)]
            )
        except RedisError as exc:
            REDIS_ERRORS.labels("rate_limit", type(exc).__name__).inc()
            RATE_LIMIT_DECISIONS.labels(self.name, "fail_open").inc()
            logger.warning("rate limiter unavailable, failing open", extra={"limiter": self.name})
            return RateLimitResult(True, self.limit, self.limit, 0)
        result = RateLimitResult(bool(allowed), self.limit, int(remaining), int(retry_after))
        RATE_LIMIT_DECISIONS.labels(self.name, "allowed" if result.allowed else "limited").inc()
        return result


def client_identity(request: Request) -> str:
    """Örnek amaçlı: X-Client-Id → yoksa bağlantı IP'si.

    Üretimde kimliği doğrulanmış kullanıcı/tenant id'si kullanılmalı; istemcinin
    gönderdiği header tek başına güvenilir değildir (istemci değiştirebilir).
    """
    header = request.headers.get("x-client-id")
    if header:
        return header[:128]
    return request.client.host if request.client else "unknown"


def rate_limit_dependency(limiter_attr: str):
    """FastAPI dependency'si üretir: limit aşılırsa 429 + Retry-After."""

    async def dependency(request: Request, response: Response) -> None:
        limiter: SlidingWindowRateLimiter = getattr(request.app.state.ctx, limiter_attr)
        result = await limiter.hit(client_identity(request))
        headers = {
            "RateLimit-Limit": str(result.limit),
            "RateLimit-Remaining": str(result.remaining),
        }
        if not result.allowed:
            retry_after_s = max(1, -(-result.retry_after_ms // 1000))  # yukarı yuvarla
            headers["Retry-After"] = str(retry_after_s)
            raise HTTPException(
                status_code=429, detail={"code": "rate_limited"}, headers=headers
            )
        response.headers.update(headers)

    return dependency
