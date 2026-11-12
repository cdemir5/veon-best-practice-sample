"""Ürün repository'si — cache'in arkasındaki "gerçek kaynak" (source of truth)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.observability.metrics import DB_LOADS

_PROJECTION = {"name": 1, "price": 1, "currency": 1, "version": 1, "updated_at": 1}


def _serialize(document: dict[str, Any] | None) -> dict[str, Any] | None:
    """Mongo belgesini JSON/cache-uyumlu sözlüğe çevirir."""
    if document is None:
        return None
    result = {"sku": document["_id"], **{k: v for k, v in document.items() if k != "_id"}}
    if isinstance(result.get("updated_at"), datetime):
        result["updated_at"] = result["updated_at"].isoformat()
    return result


class ProductRepository:
    def __init__(self, db: AsyncDatabase, *, simulated_latency_ms: int = 0) -> None:
        self._collection = db["products"]
        self._latency = simulated_latency_ms / 1000

    async def get(self, sku: str) -> dict[str, Any] | None:
        await self._simulate_latency()
        DB_LOADS.labels("product").inc()
        return _serialize(await self._collection.find_one({"_id": sku}, _PROJECTION))

    async def list(self, limit: int = 100) -> list[dict[str, Any]]:
        await self._simulate_latency()
        DB_LOADS.labels("product_list").inc()
        cursor = self._collection.find({}, _PROJECTION).sort("_id", 1).limit(limit)
        return [_serialize(doc) async for doc in cursor]

    async def update(self, sku: str, *, name: str | None, price: float | None) -> dict | None:
        changes: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if name is not None:
            changes["name"] = name
        if price is not None:
            changes["price"] = price
        document = await self._collection.find_one_and_update(
            {"_id": sku},
            {"$set": changes, "$inc": {"version": 1}},  # version → optimistic concurrency için
            projection=_PROJECTION,
            return_document=ReturnDocument.AFTER,
        )
        return _serialize(document)

    async def _simulate_latency(self) -> None:
        # Lab: yavaş bir kaynağı taklit eder (stampede etkisini görünür kılmak için).
        if self._latency:
            await asyncio.sleep(self._latency)
