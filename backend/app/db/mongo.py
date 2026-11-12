"""MongoDB client fabrikası.

  * Motor (agentic-core'da kullanılan) MongoDB tarafından deprecate edildi ve
    Mayıs 2026'da desteği bitti. Yerine PyMongo'nun native asyncio API'si
    (`AsyncMongoClient`) kullanılır; thread-pool'a iş atmadığı için OTel context'i
    doğru yayılır.
  * Process başına TEK client (içinde bağlantı havuzu var).
  * timeoutMS (Client-Side Operation Timeout): her operasyonun toplam süresi
    sınırlı → yavaş Mongo istekleri sonsuza kadar bekletmez.
  * retryWrites/retryReads: replica set failover'ında tek seferlik otomatik retry.
  * close() async'tir — agentic-core main.py'de senkron close() üzerinde await
    kullanılıyor (kapanışta TypeError).
"""
from __future__ import annotations

from pymongo import AsyncMongoClient

from app.config import Settings


def build_mongo(settings: Settings, *, appname: str) -> AsyncMongoClient:
    return AsyncMongoClient(
        settings.mongo_uri.get_secret_value(),
        appname=appname,
        maxPoolSize=settings.mongo_max_pool_size,
        minPoolSize=0,
        connectTimeoutMS=2000,
        serverSelectionTimeoutMS=settings.mongo_timeout_ms,
        timeoutMS=settings.mongo_timeout_ms,
        retryWrites=True,
        retryReads=True,
        tz_aware=True,
    )
