"""Şema/index migration'ı: `python -m app.migrate`

Neden ayrı bir adım?
  * Index oluşturma uygulama başlangıcında yapılırsa: N replika aynı anda dener,
    uygulama kullanıcısına createIndex yetkisi gerekir, büyük koleksiyonda açılış uzar.
  * Helm'de pre-install/pre-upgrade hook Job'ı, compose'da tek seferlik servis olarak
    çalışır. İdempotenttir: tekrar çalıştırmak güvenlidir.

agentic-core gözlemi: logs koleksiyonunda `event_id` için unique index yok →
eşzamanlı upsert'ler aynı olayı iki kez yazabilir. Burada unique index zorunlu.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, IndexModel

from app.config import get_settings
from app.db.mongo import build_mongo
from app.observability.logging import setup_logging, shutdown_logging

logger = logging.getLogger("app.migrate")

DEMO_PRODUCTS = [
    {"_id": "SKU-1", "name": "Mekanik Klavye", "price": 2499.0},
    {"_id": "SKU-2", "name": "Kablosuz Mouse", "price": 899.0},
    {"_id": "SKU-3", "name": "27\" Monitör", "price": 8999.0},
    {"_id": "SKU-4", "name": "USB-C Dock", "price": 3299.0},
    {"_id": "SKU-5", "name": "Kulaklık", "price": 1899.0},
]


async def migrate() -> None:
    settings = get_settings()
    client = build_mongo(settings, appname="veon-sample-migrate")
    db = client[settings.mongo_db]
    try:
        await db["events"].create_indexes(
            [
                # İdempotent consumer'ın temeli: aynı olay iki kez yazılamaz.
                IndexModel([("event_id", ASCENDING)], name="uniq_event_id", unique=True),
                IndexModel([("type", ASCENDING), ("ts", DESCENDING)], name="type_ts"),
                # Saklama süresi (retention): Mongo arka planda eski olayları siler.
                IndexModel(
                    [("ts", ASCENDING)],
                    name="ttl_ts",
                    expireAfterSeconds=settings.events_retention_days * 86400,
                ),
                IndexModel([("trace_id", ASCENDING)], name="trace_id", sparse=True),
            ]
        )
        await db["products"].create_index([("updated_at", DESCENDING)], name="updated_at")

        seeded = 0
        if settings.seed_demo_data:
            now = datetime.now(timezone.utc)
            for product in DEMO_PRODUCTS:
                result = await db["products"].update_one(
                    {"_id": product["_id"]},
                    {"$setOnInsert": {**product, "currency": "TRY", "version": 1,
                                      "updated_at": now}},
                    upsert=True,
                )
                seeded += 1 if result.upserted_id is not None else 0
        logger.info("migration completed", extra={"database": settings.mongo_db,
                                                  "seeded_products": seeded})
    finally:
        await client.close()


def main() -> None:
    settings = get_settings()
    setup_logging(
        level=settings.log_level,
        service="veon-sample-migrate",
        version=settings.app_version,
        environment=settings.deployment_environment,
    )
    try:
        asyncio.run(migrate())
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
