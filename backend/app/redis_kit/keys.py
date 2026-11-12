"""Redis anahtar şeması — tek yerde tanımlanır, kod içinde string birleştirilmez.

Kurallar:
  * Biçim:  <prefix>:<alan>:<sürüm?>:<kimlik>     örn. vsa:product:v1:SKU-1
  * Değer şeması değişince sürüm artırılır (v1 → v2): eski/yeni pod'lar
    rolling update sırasında birbirinin cache'ini bozmaz.
  * DB index yerine prefix (Cluster uyumu). Ortamlar AYNI Redis'i paylaşmamalı;
    paylaşmak zorundaysa prefix'e ortam adı eklenir (REDIS_KEY_PREFIX=vsa-stage).
  * Çoklu-anahtarlı Lua/transaction'larda {hash-tag}: Cluster'da tüm anahtarlar
    aynı slot'a düşer (aksi halde CROSSSLOT hatası).
  * Kişisel veri (e-posta, token) anahtara YAZILMAZ; gerekiyorsa HMAC/SHA-256 özeti.
    (agentic-core'daki `hash(token)` process'e özgü rastgele seed kullanır →
    pod'lar arasında aynı token farklı anahtar üretir.)
"""
from __future__ import annotations

import hashlib


def digest(value: str, length: int = 32) -> str:
    """Deterministik, process'ten bağımsız özet (Python `hash()` DEĞİL)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


class Keys:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    # --- cache ---
    def product(self, sku: str) -> str:
        return f"{self.prefix}:product:v1:{sku}"

    def product_list(self) -> str:
        return f"{self.prefix}:product:v1:__list__"

    @staticmethod
    def cache_lock(cache_key: str) -> str:
        return f"{cache_key}:lock"

    # --- flash sale (aynı hash-tag → aynı slot) ---
    def flash_stock(self, sku: str) -> str:
        return f"{self.prefix}:{{flash:{sku}}}:stock"

    def flash_buyers(self, sku: str) -> str:
        return f"{self.prefix}:{{flash:{sku}}}:buyers"

    # --- concurrency primitives ---
    def idempotency(self, scope: str, idempotency_key: str) -> str:
        return f"{self.prefix}:idem:{scope}:{digest(idempotency_key)}"

    def rate_limit(self, limiter: str, client_id: str) -> str:
        return f"{self.prefix}:rl:{limiter}:{digest(client_id, 16)}"

    def lock(self, name: str) -> str:
        return f"{self.prefix}:lock:{name}"

    def fence(self, name: str) -> str:
        return f"{self.prefix}:fence:{name}"

    # --- event pipeline ---
    def events_stream(self) -> str:
        return f"{self.prefix}:events:audit"

    def events_dlq(self) -> str:
        return f"{self.prefix}:events:audit:dlq"

    # --- misc ---
    def heartbeat(self) -> str:
        return f"{self.prefix}:heartbeat"

    def lab_db_loads(self, sku: str) -> str:
        return f"{self.prefix}:lab:dbloads:{sku}"
