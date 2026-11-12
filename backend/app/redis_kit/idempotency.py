"""Idempotency-Key desteği (IETF draft-ietf-httpapi-idempotency-key-header).

İstemci (veya mesaj kuyruğu) aynı isteği tekrar gönderdiğinde (timeout, retry,
çift tıklama) işlemin BİR kez yapılmasını garanti eder.

    SET idem:<scope>:<sha256(key)> {"state":"in_progress","fp":...} NX EX 30
      ├─ yazıldı           → işle → sonucu {"state":"done",...} EX 24h olarak sakla
      └─ zaten var
           ├─ fp farklı     → 422 (aynı anahtar farklı payload ile kullanılmış)
           ├─ in_progress   → 409 + Retry-After (eşzamanlı kopya)
           └─ done          → saklanan yanıtı aynen döndür (Idempotent-Replayed: true)

  * in_progress kaydının kısa TTL'i vardır: işleyen pod çökerse anahtar kilitli kalmaz.
  * 5xx sonuçlar saklanmaz (abort) → istemci güvenle tekrar deneyebilir.
  * Mevcut agentic-core durable_messaging/idempotency.py aynı fikri RabbitMQ
    mesajları için Lua + hash ile uygular; bu sınıf HTTP katmanı karşılığıdır.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from redis.asyncio import Redis

from app.redis_kit.keys import Keys

OutcomeKind = Literal["new", "replay", "in_progress", "mismatch"]


@dataclass(frozen=True)
class IdempotencyOutcome:
    kind: OutcomeKind
    status_code: int | None = None
    body: Any = None


class IdempotencyStore:
    def __init__(
        self,
        redis: Redis,
        keys: Keys,
        *,
        in_progress_ttl_seconds: int = 30,
        result_ttl_seconds: int = 24 * 3600,
    ) -> None:
        self._redis = redis
        self._keys = keys
        self._in_progress_ttl = in_progress_ttl_seconds
        self._result_ttl = result_ttl_seconds

    async def begin(self, scope: str, key: str, fingerprint: str) -> IdempotencyOutcome:
        redis_key = self._keys.idempotency(scope, key)
        marker = json.dumps({"state": "in_progress", "fp": fingerprint})
        for _ in range(2):  # NX ile GET arasında kaydın süresi dolarsa bir kez daha dene
            if await self._redis.set(redis_key, marker, nx=True, ex=self._in_progress_ttl):
                return IdempotencyOutcome("new")
            raw = await self._redis.get(redis_key)
            if raw is None:
                continue
            record = json.loads(raw)
            if record.get("fp") != fingerprint:
                return IdempotencyOutcome("mismatch")
            if record.get("state") == "in_progress":
                return IdempotencyOutcome("in_progress")
            return IdempotencyOutcome("replay", record["status"], record["body"])
        return IdempotencyOutcome("in_progress")

    async def complete(
        self, scope: str, key: str, fingerprint: str, status_code: int, body: Any
    ) -> None:
        if status_code >= 500:
            await self.abort(scope, key)
            return
        record = json.dumps(
            {"state": "done", "fp": fingerprint, "status": status_code, "body": body},
            default=str,
        )
        # XX: yalnız kendi in_progress kaydımız hâlâ duruyorsa üzerine yaz.
        await self._redis.set(
            self._keys.idempotency(scope, key), record, xx=True, ex=self._result_ttl
        )

    async def abort(self, scope: str, key: str) -> None:
        await self._redis.delete(self._keys.idempotency(scope, key))
