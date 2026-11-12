"""Redis client fabrikası — standalone ve Sentinel (HA) modları.

Mevcut agentic-core `redis_sentinel.py` ile aynı ortam değişkenlerini kullanır,
ancak eksik olan üretim ayarlarını ekler:

  * Process başına TEK bağlantı havuzu (istek başına client AÇILMAZ).
  * Sınırlı havuz (max_connections) — standalone'da BlockingConnectionPool:
    havuz doluysa hata yerine `pool_timeout` kadar bekler.
  * socket_timeout / socket_connect_timeout: asılı kalan master'da istek takılmaz.
  * health_check_interval: boşta kalmış (NAT/LB tarafından kesilmiş) bağlantıları yakalar.
  * Retry + jitter'lı exponential backoff — YALNIZ ConnectionError'da.
      - Bağlantı hatası (refused/reset/DNS, failover sırası) → komut sunucuya
        ulaşmamıştır, tekrar denemek güvenlidir.
      - TimeoutError → komut sunucuda ÇALIŞMIŞ olabilir. INCR/XADD/EVAL gibi
        idempotent olmayan komutlarda körlemesine retry çift yazıma yol açar.
        Bu yüzden timeout'ta retry yapılmaz; idempotency anahtarı ile çözülür.
  * Sentinel: master_for() ile yazma, slave_for() ile (gecikmeye toleranslı) okuma.
    Failover sonrası eski master'a giden komut READONLY alırsa redis-py bağlantıyı
    düşürüp Sentinel'e yeni master'ı sorar (ConnectionError → retry).
  * min_other_sentinels=1: izole kalmış tek bir Sentinel'in bayat master
    bilgisi vermesini engeller.
  * client_name: `CLIENT LIST` çıktısında hangi pod'un bağlandığı görünür.
  * DB index (SELECT 0/1/2) KULLANILMAZ; ayrım anahtar prefix'i ile yapılır
    (Redis Cluster'da yalnız DB 0 vardır).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.asyncio.sentinel import Sentinel
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import Settings, secret_value

logger = logging.getLogger(__name__)


@dataclass
class RedisHandles:
    mode: str
    primary: Redis
    replica: Redis | None = None
    sentinel: Sentinel | None = None
    master_name: str | None = None

    async def close(self) -> None:
        await self.primary.aclose()
        if self.replica is not None:
            await self.replica.aclose()
        if self.sentinel is not None:
            for sentinel_client in self.sentinel.sentinels:
                await sentinel_client.aclose()


def parse_sentinel_endpoint(value: str) -> tuple[str, int]:
    parsed = urlsplit(f"//{value.strip()}")
    if not parsed.hostname or parsed.port is None:
        raise ValueError(f"Geçersiz Sentinel adresi: {value!r} (host:port bekleniyor)")
    return parsed.hostname, parsed.port


def build_redis(
    settings: Settings,
    *,
    client_name: str,
    socket_timeout: float | None = None,
) -> RedisHandles:
    """`socket_timeout` override'ı BLOCKING komutlar (XREADGROUP BLOCK, BLPOP) içindir:
    blok süresinden kısa bir socket timeout her çağrıda TimeoutError üretir."""
    retry = Retry(
        ExponentialWithJitterBackoff(base=0.05, cap=1.0),
        settings.redis_retries,
        supported_errors=(RedisConnectionError,),
    )
    connection_kwargs = {
        "socket_timeout": socket_timeout or settings.redis_socket_timeout,
        "socket_connect_timeout": settings.redis_connect_timeout,
        "socket_keepalive": True,
        "health_check_interval": settings.redis_health_check_interval,
        "retry": retry,
        "client_name": client_name,
        "decode_responses": True,
        "max_connections": settings.redis_max_connections,
    }
    password = secret_value(settings.redis_password)

    if settings.redis_mode == "sentinel":
        if not settings.redis_sentinels:
            raise ValueError("REDIS_MODE=sentinel için REDIS_SENTINELS (JSON liste) zorunlu")
        endpoints = [parse_sentinel_endpoint(item) for item in settings.redis_sentinels]
        sentinel = Sentinel(
            endpoints,
            min_other_sentinels=1 if len(endpoints) > 1 else 0,
            sentinel_kwargs={
                "password": secret_value(settings.redis_sentinel_password),
                "socket_timeout": settings.redis_socket_timeout,
                "socket_connect_timeout": settings.redis_connect_timeout,
                "decode_responses": True,
            },
        )
        name = settings.redis_sentinel_master_name
        primary = sentinel.master_for(name, password=password, **connection_kwargs)
        replica = sentinel.slave_for(name, password=password, **connection_kwargs)
        logger.info(
            "redis client configured",
            extra={"redis_mode": "sentinel", "sentinels": len(endpoints), "master_name": name},
        )
        return RedisHandles("sentinel", primary, replica, sentinel, name)

    pool = BlockingConnectionPool.from_url(
        settings.redis_url,
        password=password,
        timeout=settings.redis_pool_timeout,
        **connection_kwargs,
    )
    logger.info("redis client configured", extra={"redis_mode": "standalone"})
    return RedisHandles("standalone", Redis.from_pool(pool))
