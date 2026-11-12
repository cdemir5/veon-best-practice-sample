"""Process ömrü boyunca yaşayan bağımlılıklar (lifespan'de bir kez kurulur).

İstek başına Redis/Mongo client açmak yerine tek havuz paylaşılır;
router'lar bu nesneye `Depends(get_ctx)` ile erişir.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.config import HOSTNAME, Settings
from app.db.mongo import build_mongo
from app.db.products import ProductRepository
from app.events.publisher import EventPublisher
from app.redis_kit.cache import CacheAside
from app.redis_kit.client import RedisHandles, build_redis
from app.redis_kit.idempotency import IdempotencyStore
from app.redis_kit.keys import Keys
from app.redis_kit.rate_limit import SlidingWindowRateLimiter
from app.redis_kit.scripts import FLASH_SALE_RESERVE


@dataclass
class AppContext:
    settings: Settings
    keys: Keys
    redis: RedisHandles
    mongo: AsyncMongoClient
    db: AsyncDatabase
    products: ProductRepository
    product_cache: CacheAside
    publisher: EventPublisher
    idempotency: IdempotencyStore
    flash_reserve: Any  # redis AsyncScript
    lab_rate_limiter: SlidingWindowRateLimiter
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Referansı tutulan arka plan görevi (GC'nin görevi yarıda bırakmaması için)."""
        task = asyncio.create_task(coroutine, name=name)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def close(self) -> None:
        for task in list(self.background_tasks):
            task.cancel()
        await asyncio.gather(*self.background_tasks, return_exceptions=True)
        await self.redis.close()
        await self.mongo.close()


def build_context(settings: Settings) -> AppContext:
    keys = Keys(settings.redis_key_prefix)
    redis = build_redis(settings, client_name=f"{settings.otel_service_name}:{HOSTNAME}")
    mongo = build_mongo(settings, appname=settings.otel_service_name)
    db = mongo[settings.mongo_db]
    return AppContext(
        settings=settings,
        keys=keys,
        redis=redis,
        mongo=mongo,
        db=db,
        products=ProductRepository(db, simulated_latency_ms=settings.simulated_db_latency_ms),
        product_cache=CacheAside(
            redis.primary,
            name="product",
            ttl_seconds=settings.cache_ttl_seconds,
            negative_ttl_seconds=settings.cache_negative_ttl_seconds,
            lock_ttl_ms=settings.cache_lock_ttl_ms,
            wait_timeout_ms=settings.cache_wait_timeout_ms,
        ),
        publisher=EventPublisher(redis.primary, keys, settings, source=settings.otel_service_name),
        idempotency=IdempotencyStore(redis.primary, keys),
        flash_reserve=redis.primary.register_script(FLASH_SALE_RESERVE),
        lab_rate_limiter=SlidingWindowRateLimiter(
            redis.primary, keys, name="lab", limit=10, window_seconds=10
        ),
    )
