# 5 · Asenkron log / event yazımı (Mongo'ya istek içinde yazmadan)

## Problem

agentic-core'da `LOG_RESULTS_TYPE=mongo` iken çalıştırma/konuşma olayları istek akışı içinde yazılır:

```python
# mongo_log_service_client.py — her olay, isteğin içinde await ile
await self._log_db_client.insert(self._collection_name, redact_sensitive(log_entry))
await self._log_db_client.upsert(self._collection_name, {"event_id": event_id}, safe_entry)  # replace_one(upsert=True)
```

| Sonuç | Etki |
|-------|------|
| Mongo yavaşlarsa | **Kullanıcı isteği** yavaşlar (p99 = Mongo p99 + iş) |
| Mongo erişilemezse | İstek hata alır ya da olay kaybolur (buffer yok) |
| Olay başına ayrı round-trip | Batch yok → yük altında Mongo'da gereksiz yazma IOPS |
| `event_id` için unique index yok | Eşzamanlı upsert'ler aynı olayı iki kez yazabilir |

## İki ayrı ihtiyaç, iki ayrı yol

| | **Operasyonel log** | **Audit / iş olayı** |
|-|---------------------|----------------------|
| Örnek | "http_request 200 4ms", "redis unavailable" | "rezervasyon yapıldı", "LLM çağrısı tamamlandı (token sayısı)" |
| Sorgulanma | Arıza ayıklama, birkaç gün | Uygulama/raporlama sorgular, haftalar/aylar |
| Kayıp toleransı | Kabul edilebilir (best effort) | Düşük |
| Yol | **stdout → Alloy/Fluent Bit → Loki/ES** ([03](03-logging.md)) | **Bu doküman:** kuyruk → Redis Stream → worker → MongoDB |

Operasyonel logu Mongo'ya yazmak bir anti-pattern'dir (yanlış araç). Audit olayları Mongo'da sorgulanacaksa
bile **istek akışından ayrılmalıdır**.

## Mimari

```
 API isteği                        arka plan görevi (aynı pod)              worker pod'ları (N adet)
 ───────────                       ──────────────────────────              ────────────────────────
 publish(event)  ─put_nowait─►  asyncio.Queue(10k) ─batch─► XADD MAXLEN~100k ─►  Redis Stream
   O(1), I/O yok                  doluysa DÜŞÜR+say          pipeline, 20 sn      vsa:events:audit
   traceparent eklenir                                       bütçeli retry              │
                                                                          XREADGROUP (consumer group)
                                                                                         ▼
                                                     bulk_write(UpdateOne(event_id, $setOnInsert, upsert))
                                                                                         ▼
                                                                            XACK  (yalnız yazım başarılıysa)
```

Kod: [publisher.py](../backend/app/events/publisher.py) · [worker.py](../backend/app/worker.py) · [migrate.py](../backend/app/migrate.py)

## Garantiler ve hata senaryoları

**Model:** at-least-once teslim + idempotent tüketici ⇒ Mongo'da her olay **bir kez**.

| Arıza | Ne olur? | Veri |
|-------|----------|------|
| Mongo yavaş/erişilemez | Worker yazamaz, ACK etmez; mesajlar PEL'de bekler, worker 1 sn backoff ile tekrar dener. **API etkilenmez.** | Kayıp yok |
| Worker çöker (batch ortasında) | ACK'lenmemiş mesajlar 30 sn sonra başka worker'ca `XCLAIM` ile devralınır; yeniden başlayan pod önce kendi PEL'ini işler | Kayıp yok (tekrar olabilir → idempotent) |
| Zehirli mesaj (parse edilemiyor / 5 kez başarısız) | `vsa:events:audit:dlq` stream'ine taşınır + ACK; alarm | İnceleme için saklanır |
| Redis failover | Publisher 20 sn bütçeyle tekrar dener, olaylar bellek kuyruğunda bekler | Ölçüm: **0 kayıp** |
| Redis uzun süre yok / kuyruk dolu | Yeni olaylar düşer, `events_dropped_total{reason}` artar → alarm | Kayıp (görünür) |
| API pod'u SIGTERM | Lifespan kuyruğu 5 sn içinde boşaltmaya çalışır; kalanlar `reason="shutdown"` | Kayıp (görünür, nadir) |
| API pod'u SIGKILL/OOM | Bellek kuyruğundaki (genelde < 1 sn'lik) olaylar | Kayıp (görünmez) |

> **Kayıp hiç kabul edilemiyorsa** olay, iş verisiyle **aynı transaction'da** yazılır (Transactional
> **Outbox**): ör. Mongo'da iş belgesi + `outbox` belgesi tek transaction; ayrı bir relay outbox'ı stream'e/kuyruğa
> taşır. Ya da mevcut **RabbitMQ** (quorum queue + publisher confirm) kullanılır — agentic-core durable
> messaging zaten RabbitMQ + Redis idempotency kullanıyor.

## Neden Redis Streams?

| Seçenek | Artı | Eksi |
|---------|------|------|
| **Redis Streams** (bu repo) | Zaten var; consumer group, PEL, XCLAIM, MAXLEN; düşük gecikme | Asenkron replikasyon → failover'da son birkaç ms kaybolabilir; bellekte tutulur |
| RabbitMQ (quorum queue) | Dayanıklı, confirm, DLX; VEON'da mevcut | Ek bağlantı/topoloji yönetimi |
| Kafka | Yüksek hacim, replay, uzun saklama | Operasyon maliyeti |
| Collector → Mongo | — | Fluent Bit'in resmi Mongo çıkışı yok; log hattını iş verisiyle karıştırır |

## Mongo tarafı

```python
IndexModel([("event_id", 1)], name="uniq_event_id", unique=True)              # idempotency'nin temeli
IndexModel([("type", 1), ("ts", -1)], name="type_ts")                          # sorgu deseni
IndexModel([("ts", 1)], name="ttl_ts", expireAfterSeconds=30 * 86400)          # saklama süresi
IndexModel([("trace_id", 1)], name="trace_id", sparse=True)                    # Tempo'ya geçiş
```

- Index'ler uygulama açılışında değil **migration Job'ında** oluşturulur (Helm `pre-install/pre-upgrade` hook).
- `UpdateOne({"event_id": id}, {"$setOnInsert": doc}, upsert=True)`: değişmez olay; tekrar gelirse **dokunulmaz**.
  Aynı `event_id`'nin **güncellenen** bir durumu temsil ettiği lifecycle olaylarında `replace_one(upsert=True)`
  (core'daki gibi) veya sürüm alanıyla koşullu güncelleme kullanılır.
- `bulk_write(ordered=False)`: bir hata diğerlerini durdurmaz; eşzamanlı upsert yarışındaki **E11000**
  (duplicate key) başarı sayılır.
- Deterministik `event_id` (ör. `reservation_id`, `product.updated:SKU-1:v7`) → üretici tekrar yayınlasa da tek kayıt.

## Ölçekleme ve izleme

- Worker replikaları consumer group içinde mesajları **paylaşır**; sıralama tek mesaj bazında değil, gerekiyorsa
  anahtar bazlı ayrı stream/partition tasarlanır.
- Ölçek kriteri CPU değil **birikme**: KEDA `redis-sentinel-streams` tetikleyicisi (`lagCount`) — chart'ta
  `worker.keda.enabled`.
- Metrikler: `events_enqueued/published/persisted/dropped_total`, `events_stream_lag`, `events_stream_pending`,
  `events_dlq_length`, `events_end_to_end_latency_seconds`, `worker_last_success_timestamp_seconds`.
- Alarmlar: EventsDropped, EventLagHigh, DeadLetters, WorkerDown, WorkerStalled.
- Eski consumer kayıtları (her deploy'da yeni pod adı) bakım adımında temizlenir.

## Deneyin

```bash
make worker-stop                 # worker'lar durur
# arayüz → "10.000 olay üret"   → Stream uzunluğu ve lag artar, API gecikmesi DEĞİŞMEZ
make worker-start                # birikmiş olaylar saniyeler içinde Mongo'ya akar
make failover                    # olay üretirken failover → events_dropped_total artmaz
```

Local ölçüm (2 worker replikası):

| Adım | Sonuç |
|------|-------|
| Worker'lar durdurulup 10.000 olay üretildi | `lag=10000`, kayıp 0; ürün GET medyanı ~3–10 ms (değişmedi) |
| Worker'lar başlatıldı | 10.000 olay `docker compose start` döndükten **1,6 sn** sonra Mongo'da (container açılışı dahil 3,8 sn) |
| 2.000 olay, worker'lar çalışırken | Uçtan uca (üretim → Mongo) ~170 ms |
| Kontrollü Redis failover sırasında sürekli üretim | `events_dropped_total` = 0, pending = 0 |
