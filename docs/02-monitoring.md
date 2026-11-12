# 2 · Monitoring (Prometheus + Grafana)

## Ne ölçülür?

| Katman | Yöntem | Örnek metrikler |
|--------|--------|-----------------|
| HTTP (kullanıcı deneyimi) | **RED**: Rate, Errors, Duration | `http_requests_total{method,route,status}`, `http_request_duration_seconds{method,route}`, `http_requests_in_progress` |
| Redis / cache | Sonuç sayaçları | `cache_requests_total{cache,result}`, `db_loads_total`, `redis_errors_total{operation,error}` |
| Eşzamanlılık | Karar sayaçları | `redis_lock_acquire_total{lock,result}`, `rate_limit_decisions_total`, `idempotency_requests_total{outcome}` |
| İş metrikleri | Domain olayları | `flash_sale_reservations_total{mode,result}` |
| Event pipeline | Akış + birikme | `events_enqueued/published/persisted/dropped_total`, `events_stream_lag`, `events_stream_pending`, `events_end_to_end_latency_seconds` |
| Logging sağlığı | Kayıp | `app_logs_dropped_total` |
| Altyapı | Exporter'lar | `redis_up`, `redis_instance_info{role}`, `redis_connected_slaves`, process_* (CPU/RSS/FD) |
| Kimlik | Sabit | `app_info{component,version,env} = 1` |

Tanımlar tek yerde: [metrics.py](../backend/app/observability/metrics.py).

## Kural 1 — Label kardinalitesi

Her farklı label değeri **ayrı bir zaman serisidir**. Sınırsız değerli label Prometheus'u çökertir.

| ❌ Yanlış | ✅ Doğru |
|-----------|---------|
| `path=request.url.path` → `/api/sessions/7f3a…` (her session yeni seri) | `route=/api/sessions/{session_id}` (route **şablonu**) |
| `user_id`, `session_id`, `sku`, `trace_id` label | Bunlar log/trace'e; metrikte yalnız sınırlı kümeler |
| 404 tarama trafiği ham path ile | `route="__unmatched__"` |
| `service`, `env`, `pod` label'ını uygulama ekler | Scrape tarafı ekler (`job`, `namespace`, `pod`) |

Route şablonu saf ASGI middleware'de `scope["route"].path` ile alınır: [middleware.py](../backend/app/observability/middleware.py).

## Kural 2 — Ayrı metrik portu, tek process

- `/metrics` **9100** portunda (`prometheus_client.start_http_server`); uygulama portu 8000. Ingress yalnız
  uygulama portunu açar, NetworkPolicy 9100'e yalnız `observability` namespace'inden izin verir.
- Pod başına **tek process** → `PROMETHEUS_MULTIPROC_DIR` ve dosya tabanlı birleştirme gerekmez. Ölçek
  replika/HPA ile. (Çoklu uvicorn worker zorunluysa multiprocess modu + `mmap` dizini gerekir; tavsiye edilmez.)

## Kural 3 — Exemplar: metrikten trace'e

Histogram gözlemine o anki trace id'si eklenir:

```python
HTTP_LATENCY.labels(method, route).observe(duration, exemplar=current_exemplar())
```

Gereksinimler (hepsi bu repoda yapılandırılmış):
1. Exposition **OpenMetrics** olmalı — `start_http_server` Prometheus'un `Accept` header'ına göre otomatik seçer.
2. Prometheus: `--enable-feature=exemplar-storage` (Operator'da Prometheus CR `enableFeatures`).
3. Grafana Prometheus datasource: `exemplarTraceIdDestinations: [{name: trace_id, datasourceUid: tempo}]`.

Grafana'da gecikme grafiğindeki noktalar exemplar'dır; üzerine gelip **Query with Tempo** ile ilgili isteğin trace'i açılır.
p99 sıçramasını gördüğünüzde "hangi istek yavaştı?" sorusunun cevabı tek tıktır.

## Kural 4 — Sağlık probe'ları doğru ayrılır

| Probe | Uç | Kontrol eder | Etmez |
|-------|----|--------------|-------|
| startup | `/healthz` | process ayağa kalktı mı | — |
| liveness | `/healthz` | process kilitlenmedi mi | **bağımlılık** (Redis/Mongo düştü diye restart = restart fırtınası) |
| readiness | `/readyz` | zorunlu bağımlılık (Mongo) | degrade edilebilir bağımlılık (Redis → cache bypass) |
| worker liveness | `python -m app.healthcheck` | ana döngü heartbeat dosyası < 60 sn | — |

## Alarmlar — semptom odaklı

Kural: **kullanıcı etkisine** alarm (5xx oranı, gecikme, veri kaybı), **nedenlere** dashboard.
Local: [infra/prometheus/alerts.yml](../infra/prometheus/alerts.yml) · K8s: chart'taki `PrometheusRule`.

| Alarm | İfade (özet) | Neden önemli |
|-------|--------------|--------------|
| HighErrorRate | 5xx / toplam > %5, 5 dk | Kullanıcı hata görüyor |
| HighLatencyP95 | p95 > 1 sn, 10 dk (lab uçları hariç) | Kullanıcı yavaşlık görüyor |
| EventsDropped | `increase(events_dropped_total[10m]) > 0` | Audit verisi kaybı |
| LogsDropped | `increase(app_logs_dropped_total[10m]) > 0` | Gözlemlenebilirlik kaybı |
| EventLagHigh | lag veya pending > 1000, 5 dk | Worker yetişemiyor → ölçekle |
| DeadLetters | DLQ'ya yeni kayıt | Zehirli mesaj, elle inceleme |
| WorkerDown / WorkerStalled | çalışan worker yok / 5 dk başarılı batch yok + lag var | Pipeline durdu |
| RedisDown / ReplicaMissing / MemoryHigh | exporter metrikleri | HA zayıfladı, `noeviction` ile yazmalar reddedilecek |

VEON bundle'ındaki genel alarmlar (deployment unavailable, crashloop, OOM, disk) bunları tamamlar.

## Dashboard-as-code

- [veon-sample-overview.json](../infra/grafana/dashboards/veon-sample-overview.json) repoda; Grafana'da `allowUiUpdates: false`.
- Datasource'lar **değişken** (`${ds_prometheus}`, `${ds_loki}`): aynı JSON compose'da ve kümede çalışır
  (VEON'daki gibi sabit UID varsayımı yok). Job filtresi `app_info{component="api"}` üzerinden otomatik bulunur.
- K8s'te chart dashboard'u `grafana_dashboard: "1"` label'lı ConfigMap olarak kurar (Grafana sidecar yükler).

Panel grupları: API RED · Redis (cache sonuçları, eşzamanlılık primitifleri, **rol zaman çizelgesi** — failover
anı görünür, hata oranı, komut/sn, bellek) · Event pipeline (akış, birikme, uçtan uca gecikme) · Loglar.

## Tempo'dan türetilen metrikler

Tempo metrics-generator span'lerden RED metrikleri ve servis grafiği üretip Prometheus'a yazar
(`traces_service_graph_request_total`, `traces_spanmetrics_*`). Grafana → Explore → Tempo → **Service Graph**.
Not: Bunlar **örneklenmiş** trace'lerden türetilir; doğru sayımlar için uygulama metrikleri esas alınır.
