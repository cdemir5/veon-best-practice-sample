"""Gerçek Redis (Sentinel) üzerinde eşzamanlılık testleri.

Çalıştırma:  make test   (compose'daki Sentinel kümesine karşı)
Ortam değişkeni TEST_REDIS_INTEGRATION=1 değilse atlanır.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.config import Settings
from app.redis_kit.cache import CacheAside
from app.redis_kit.client import build_redis
from app.redis_kit.idempotency import IdempotencyStore
from app.redis_kit.keys import Keys
from app.redis_kit.rate_limit import SlidingWindowRateLimiter
from app.redis_kit.scripts import FLASH_SALE_RESERVE

pytestmark = pytest.mark.skipif(
    os.getenv("TEST_REDIS_INTEGRATION") != "1", reason="TEST_REDIS_INTEGRATION=1 değil"
)


def _run(coro):
    return asyncio.run(coro)


async def _with_redis(test):
    settings = Settings()
    handles = build_redis(settings, client_name="pytest")
    keys = Keys(f"test-{uuid.uuid4().hex[:8]}")
    try:
        await test(handles.primary, keys)
    finally:
        async for key in handles.primary.scan_iter(match=f"{keys.prefix}:*", count=500):
            await handles.primary.unlink(key)
        await handles.close()


def test_flash_sale_never_oversells_under_concurrency() -> None:
    async def scenario(redis, keys):
        await redis.set(keys.flash_stock("SKU-T"), 10, ex=60)
        script = redis.register_script(FLASH_SALE_RESERVE)
        results = await asyncio.gather(
            *(
                script(keys=[keys.flash_stock("SKU-T"), keys.flash_buyers("SKU-T")],
                       args=[f"user-{i}", 1])
                for i in range(200)
            )
        )
        reserved = sum(1 for code, _ in results if code == 1)
        assert reserved == 10
        assert int(await redis.get(keys.flash_stock("SKU-T"))) == 0
        assert await redis.ttl(keys.flash_buyers("SKU-T")) > 0  # TTL'siz anahtar kalmamalı

    _run(_with_redis(scenario))


def test_cache_stampede_loads_source_once() -> None:
    async def scenario(redis, keys):
        cache = CacheAside(redis, name="test", ttl_seconds=30, negative_ttl_seconds=5,
                           lock_ttl_ms=3000, wait_timeout_ms=3000)
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.2)
            return {"value": 42}

        results = await asyncio.gather(
            *(cache.get_or_load(keys.product("X"), loader) for _ in range(50))
        )
        assert calls == 1
        assert all(value == {"value": 42} for value, _ in results)
        statuses = {status for _, status in results}
        assert statuses <= {"MISS", "COALESCED"}

    _run(_with_redis(scenario))


def test_rate_limiter_is_shared_and_exact() -> None:
    async def scenario(redis, keys):
        limiter = SlidingWindowRateLimiter(redis, keys, name="t", limit=5, window_seconds=5)
        results = await asyncio.gather(*(limiter.hit("client-a") for _ in range(20)))
        assert sum(r.allowed for r in results) == 5
        assert all(r.retry_after_ms > 0 for r in results if not r.allowed)

    _run(_with_redis(scenario))


def test_idempotency_single_execution() -> None:
    async def scenario(redis, keys):
        store = IdempotencyStore(redis, keys)
        outcomes = await asyncio.gather(
            *(store.begin("t", "key-12345678", "fp") for _ in range(10))
        )
        assert [o.kind for o in outcomes].count("new") == 1
        await store.complete("t", "key-12345678", "fp", 201, {"ok": True})
        replay = await store.begin("t", "key-12345678", "fp")
        assert replay.kind == "replay" and replay.status_code == 201
        mismatch = await store.begin("t", "key-12345678", "other")
        assert mismatch.kind == "mismatch"

    _run(_with_redis(scenario))
