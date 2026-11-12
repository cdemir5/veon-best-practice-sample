"""Cache-aside (lazy loading) + stampede (thundering herd) koruması.

Okuma akışı:
    GET key ── hit ──► dön
       │ miss
       ├─ SET key:lock <token> NX PX <ttl>   (yalnız BİR istek kaynağa gider)
       │     ├─ kazandı → tekrar GET (double-check) → Mongo'dan yükle → SET key EX ttl±%10
       │     └─ kaybetti → kısa aralıklarla GET (lider doldurana kadar) → "coalesced"
       │                    zaman aşımı → kendisi yükler ("timeout_fallback")
       └─ Redis hatası → cache'i ATLA, doğrudan kaynaktan oku ("bypass")
                         (cache bir hızlandırıcıdır; düşmesi servisi düşürmemeli)

Diğer kurallar:
  * HER anahtarın TTL'i vardır. TTL'e ±%10 jitter → aynı anda süresi dolan
    binlerce anahtarın aynı saniyede kaynağa yüklenmesi (avalanche) önlenir.
  * "Bulunamadı" sonucu da kısa TTL ile cache'lenir (negative caching) →
    olmayan id'lerle kaynağın dövülmesi (penetration) önlenir.
  * Yazma sonrası anahtar SİLİNİR (SET edilmez): delete-after-write.
    Yarış penceresi kısa TTL ile sınırlanır.
  * Değerler {"v": ...} zarfı ile JSON saklanır (None ile "yok"u ayırmak için).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import LockError, RedisError

from app.observability.metrics import CACHE_LOAD_SECONDS, CACHE_REQUESTS, REDIS_ERRORS
from app.observability.tracing import tracer
from app.redis_kit.keys import Keys

logger = logging.getLogger(__name__)

_MISS = object()
_ERROR = object()

Loader = Callable[[], Awaitable[Any]]


class CacheAside:
    def __init__(
        self,
        redis: Redis,
        *,
        name: str,
        ttl_seconds: int,
        negative_ttl_seconds: int,
        lock_ttl_ms: int,
        wait_timeout_ms: int,
    ) -> None:
        self._redis = redis
        self._name = name
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._lock_ttl = lock_ttl_ms / 1000
        self._wait_timeout = wait_timeout_ms / 1000

    async def get_or_load(
        self, key: str, loader: Loader, *, ttl: int | None = None, protect: bool = True
    ) -> tuple[Any, str]:
        """(değer, durum) döndürür. Durum: HIT | MISS | COALESCED | BYPASS | TIMEOUT_FALLBACK."""
        cached = await self._read(key)
        if cached is _ERROR:
            return await self._load(loader), self._count("BYPASS")
        if cached is not _MISS:
            return cached, self._count("HIT")

        if not protect:  # yalnız lab: korumasız davranışı göstermek için
            value = await self._load(loader)
            await self._write(key, value, ttl)
            return value, self._count("MISS")

        lock = self._redis.lock(
            Keys.cache_lock(key), timeout=self._lock_ttl, blocking=False, thread_local=False
        )
        try:
            acquired = await lock.acquire()
        except RedisError as exc:
            self._redis_error("lock", exc)
            return await self._load(loader), self._count("BYPASS")

        if acquired:
            try:
                cached = await self._read(key)  # double-check: biz beklerken dolmuş olabilir
                if cached is not _MISS and cached is not _ERROR:
                    return cached, self._count("COALESCED")
                value = await self._load(loader)
                await self._write(key, value, ttl)
                return value, self._count("MISS")
            finally:
                await self._release(lock)

        # Başka bir istek (bu pod'da ya da başka pod'da) yüklüyor → bekle.
        deadline = time.monotonic() + self._wait_timeout
        delay = 0.02
        while time.monotonic() < deadline:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.2)
            cached = await self._read(key)
            if cached is _ERROR:
                break
            if cached is not _MISS:
                return cached, self._count("COALESCED")
        return await self._load(loader), self._count("TIMEOUT_FALLBACK")

    async def put(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """Cache'i önceden doldurma (warm-up) için."""
        await self._write(key, value, ttl)

    async def invalidate(self, *keys: str) -> None:
        try:
            await self._redis.unlink(*keys)  # UNLINK: büyük değerlerde bloklamayan silme
        except RedisError as exc:
            # Silme başarısızsa veri TTL kadar bayat kalabilir → metrik + log ile görünür.
            self._redis_error("invalidate", exc)

    # --- iç yardımcılar ----------------------------------------------------------
    async def _read(self, key: str) -> Any:
        try:
            raw = await self._redis.get(key)
        except RedisError as exc:
            self._redis_error("get", exc)
            return _ERROR
        if raw is None:
            return _MISS
        return json.loads(raw)["v"]

    async def _write(self, key: str, value: Any, ttl: int | None) -> None:
        base_ttl = self._negative_ttl if value is None else (ttl or self._ttl)
        jittered = max(1, int(base_ttl * random.uniform(0.9, 1.1)))
        try:
            await self._redis.set(key, json.dumps({"v": value}, default=str), ex=jittered)
        except RedisError as exc:
            self._redis_error("set", exc)

    async def _load(self, loader: Loader) -> Any:
        with tracer.start_as_current_span(
            "cache.load", attributes={"coform.operation": "cache.load", "cache.name": self._name}
        ):
            started = time.perf_counter()
            try:
                return await loader()
            finally:
                CACHE_LOAD_SECONDS.labels(self._name).observe(time.perf_counter() - started)

    async def _release(self, lock: Any) -> None:
        try:
            await lock.release()
        except LockError:
            # Kilit TTL'i yükleme süresinden kısa kalmış: başka biri devralmış olabilir.
            logger.warning("cache lock expired before release", extra={"cache": self._name})
        except RedisError as exc:
            self._redis_error("unlock", exc)

    def _count(self, result: str) -> str:
        CACHE_REQUESTS.labels(self._name, result.lower()).inc()
        return result

    def _redis_error(self, operation: str, exc: Exception) -> None:
        REDIS_ERRORS.labels(operation, type(exc).__name__).inc()
        logger.warning(
            "redis unavailable, degrading",
            extra={"cache": self._name, "operation": operation, "error_type": type(exc).__name__},
        )
