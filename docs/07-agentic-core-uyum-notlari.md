# 7 · agentic-core için çıkarımlar

Kapsam: `agentic-core-mongo-redis-aciklamali-20261109/source/` altındaki kaynak snapshot'ı (09.11.2026) ve
`veon-monitoring-helm` paketi. Canlı ortam doğrulanmadı; satır numaraları snapshot'a aittir.
Bu repo core'u değiştirmez — her madde için örnekteki karşılık gösterilmiştir.

Kısaltma: `CORE = ../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core`

## Öncelik: yüksek (doğruluk / veri / kesinti riski)

### 1. Auth cache anahtarı `hash(token)` ile üretiliyor
[`redis_auth_cache.py:59`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/clients/authentication_service_clients/redis_auth_cache.py)

```python
token_hash = hash(token)
```
- Python'da `str` hash'i **process başına rastgele seed** (`PYTHONHASHSEED`) kullanır. Aynı token farklı pod'larda
  (ve aynı pod'un restart'ından sonra) **farklı anahtar** üretir → Redis auth cache replikalar arasında paylaşılmaz,
  hit oranı replika sayısıyla düşer, eski anahtarlar TTL dolana kadar çöp olarak kalır.
- `hash()` kriptografik değildir (64 bit, çakışma mümkün).
- **Öneri:** `hmac.new(server_secret, token, sha256).hexdigest()`. Kanıt testi:
  `test_digest_is_stable_across_processes_unlike_builtin_hash` ([test_unit.py](../backend/tests/test_unit.py)).

### 2. Mongo `close()` senkron, kapanışta `await` ediliyor
[`mongodb_client.py:46`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/clients/db_clients/mongodb_client.py) `def close(self) -> None` ·
[`main.py:583`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/main.py) `await mongodb_client.close()`
- `None` await edilemez → kapanışta `TypeError`; sonraki kapanış adımları (varsa) çalışmayabilir.
- **Öneri:** PyMongo Async'e geçişte `await client.close()` doğal olarak async'tir ([mongo.py](../backend/app/db/mongo.py)).

### 3. Session güncellemesi atomik değil (lost update)
[`redis_session_registry.py:130-148`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/registries/session_registries/redis_session_registry.py)
`GET → birleştir → SET` iki ayrı komut. Aynı session'a eşzamanlı iki güncelleme (ör. paralel tool çağrısı, iki
sekme) gelirse sonra yazan diğerinin değişikliğini **ezer**.
- **Öneri:** `version` alanı + Lua compare-and-set (oluşturmada zaten `REPLACE_EXPIRED_SESSION_SCRIPT` ile yapılan
  desen) veya `WATCH/MULTI/EXEC`. Bkz. [04 §4.6b](04-redis-ha-concurrency.md).

### 4. Session anahtarlarında native TTL yok
`create_session` `SET NX`, `update_session_data` düz `SET` — `EX` yok; süre yalnız JSON içindeki `expires_at` ile
okuma anında kontrol ediliyor (rehber de bunu doğruluyor). **Hiç okunmayan** süresi dolmuş session'lar Redis'te
kalıcı olur → bellek büyür; `noeviction` ise sonunda yazmalar reddedilir.
- **Öneri:** `SET ... EX <ttl>` (oluşturma) ve güncellemede `EX` (kayan süre) — `expires_at` uygulama mantığı için kalabilir.

### 5. Olaylar istek içinde Mongo'ya yazılıyor; `event_id` unique index yok
[`mongo_log_service_client.py`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/clients/log_service_clients/mongo_log_service_client.py)
- Mongo gecikmesi doğrudan kullanıcı gecikmesine eklenir; kesintide olay kaybı/hata.
- `replace_one({"event_id"}, upsert=True)` unique index olmadan eşzamanlı çalışırsa **çift kayıt** oluşabilir.
- **Öneri:** [05-async-log-pipeline.md](05-async-log-pipeline.md) deseni + migration Job'da unique index. Operasyonel
  loglar Mongo'ya değil stdout → Alloy → Loki.

### 6. Redis client'larında timeout / retry / havuz sınırı yok
[`redis_sentinel.py:66-91`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/redis_sentinel.py) `redis.from_url(...)` / `master_for(...)` —
yalnız auth cache `socket_timeout=5` veriyor.
- Donmuş master'da (ağ bölünmesi) istekler OS TCP timeout'una kadar **asılı kalabilir**; failover'da retry/backoff yok.
- **Öneri:** [client.py](../backend/app/redis_kit/client.py): `socket_timeout`, `socket_connect_timeout`,
  `health_check_interval`, yalnız `ConnectionError`'da jitter'lı retry, `max_connections`, `min_other_sentinels=1`,
  `client_name`. Ölçümler: [04 §4.3](04-redis-ha-concurrency.md).

## Öncelik: orta (gözlemlenebilirlik / maliyet)

### 7. Metrik label'ında ham path
[`main.py:703-715`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/main.py) `path=request.url.path`
- `/sessions/<id>` gibi her benzersiz path yeni zaman serisi → Prometheus bellek/kardinalite sorunu.
  Ayrıca `service`/`env` label'larını uygulama ekliyor (scrape tarafının işi).
- **Öneri:** route şablonu (`scope["route"].path`), eşleşmeyen için `__unmatched__` ([middleware.py](../backend/app/observability/middleware.py)).

### 8. İstek başına iki log satırı + query string
[`main.py:634-685`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/main.py)
"HTTP request started" + "completed" (log hacmi ×2), `query_params` ve `client_ip` loglanıyor (token/PII riski).
- **Öneri:** tek access log satırı; query/body/header/IP yok; `otel_trace_id` + `request_id` otomatik
  ([logging.py](../backend/app/observability/logging.py)). VEON Loki derived field'ı `otel_trace_id` bekler.

### 9. Tek `/health`, her zaman `ok`
[`main.py:765-767`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/main.py)
- **Öneri:** `/healthz` (liveness, bağımlılıksız) + `/readyz` (zorunlu bağımlılıklar) + startup probe ([system.py](../backend/app/api/system.py)).

### 10. Mongo operasyon zaman aşımı yok
[`mongodb_client.py:5-14`](../../agentic-core-mongo-redis-aciklamali-20261109/source/src/agentic_core/clients/db_clients/mongodb_client.py) yalnız `serverSelectionTimeoutMS=5000`.
- **Öneri:** `timeoutMS` (client-side operation timeout), `maxPoolSize`, `appname`, `retryWrites/retryReads`.

### 11. Motor kullanımı
Motor MongoDB tarafından deprecate edildi ve desteği Mayıs 2026'da sona erdi; thread-pool tabanlı olduğu için
OTel context yayılımı da güvenilir değil. **Öneri:** `pymongo.AsyncMongoClient` (bu repoda doğrulandı:
pymongo instrumentation async client ile trace'e doğru bağlanıyor).

### 12. Redis DB index'leri (0/1/2) ile ayrım
`REDIS_URL + DB_INDEX` deseni Sentinel'de çalışır ama Redis **Cluster**'a geçişi engeller (yalnız DB 0) ve ACL
yazımını zorlaştırır. **Öneri:** anahtar prefix'leri ([keys.py](../backend/app/redis_kit/keys.py)).

### 13. Prometheus multiprocess dizini
`PROMETHEUS_MULTIPROC_DIR` ile çok worker'lı uvicorn desteği var. **Öneri:** pod başına tek process + HPA;
multiprocess modu gauge semantiğini ve exemplar'ları kısıtlar, dosya temizliği gerektirir.

## Zaten iyi olanlar (korunmalı)

- `redact_sensitive` ile log/olay maskeleme (bu repoda aynı yaklaşım genişletildi).
- Session oluşturmada `SET NX` + süresi dolmuş kaydı Lua CAS ile değiştirme.
- Durable messaging: Redis'te idempotency, lease, sıra; RabbitMQ retry exchange'leri.
- Sentinel desteği ve ayrı sentinel parolası.
- VEON observability bundle: sınırlı span kuyruğu, attribute allowlist, Alloy maskeleme, namespace-scoped RBAC,
  "iki toplayıcı aynı namespace'i izlemesin" kuralı.

## Önerilen geçiş sırası

1. (Düşük risk, yüksek getiri) #1 `hash()` → HMAC, #2 `close()`, #6 Redis timeout/retry, #9 probe'lar.
2. #7–#8 metrik/log düzeltmeleri (dashboard/alarm sorgularını güncelleyin).
3. #4 native TTL, #3 session CAS (eşzamanlılık testleriyle).
4. #5 asenkron olay hattı + unique index (önce gölge yazım/karşılaştırma ile).
5. #11 Motor → PyMongo Async, #12 prefix'e geçiş (anahtar migrasyon planıyla).
