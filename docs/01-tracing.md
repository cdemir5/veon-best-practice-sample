# 1 · Tracing (OpenTelemetry)

## Hedef

Bir kullanıcı isteğinin **servisler, Redis, kuyruk ve veritabanı** boyunca izini tek bir `trace_id` ile sürmek;
log ve metriklerden bu trace'e tek tıkla geçebilmek.

```
POST /api/flash-sale/{sku}/reserve        veon-sample-backend  SERVER     2.6 ms
 ├─ flash_sale.reserve                    veon-sample-backend  INTERNAL   1.0 ms
 │   └─ EVALSHA                           veon-sample-backend  CLIENT     0.9 ms   (Redis Lua)
 └─ events.process                        veon-sample-worker   CONSUMER  10.9 ms   (Redis Stream üzerinden!)
     └─ events.persist                    veon-sample-worker   INTERNAL   9.4 ms
         └─ veon_sample.update            veon-sample-worker   CLIENT     7.3 ms   (MongoDB bulk upsert)
```
*(Bu çıktı local stack'te Tempo API'sinden alınmıştır.)*

## Mimari

```
                                                                                  ┌──OTLP/gRPC──► Tempo   (Grafana ile)
uygulama (OTel SDK) ──OTLP/HTTP :4318──► Alloy (memory_limiter → scrub → batch) ──┤
                                                                                  └──OTLP/gRPC──► Jaeger  (Jaeger UI)
```

- Uygulama **Tempo'yu da Jaeger'ı da bilmez**; yalnız `OTEL_EXPORTER_OTLP_ENDPOINT` bilir. Backend değişirse kod değişmez.
- Collector; bellek sınırı, hassas attribute temizliği, batch, retry ve **fan-out**'u üstlenir.
- VEON üretiminde bu rol `veon-observability-alloy.observability.svc.cluster.local:4318`'dir.

## Tempo ve Jaeger birlikte

Bu repo aynı trace'leri iki backend'e gönderir ([config.alloy](../infra/alloy/config.alloy)):

```alloy
otelcol.processor.batch "default" {
  output {
    traces = [otelcol.exporter.otlp.tempo.input, otelcol.exporter.otlp.jaeger.input]
  }
}
```

- Her exporter'ın kendi kuyruğu ve retry'ı vardır; biri erişilemezse diğeri etkilenmez.
- Jaeger v2, OpenTelemetry Collector tabanlıdır ve **OTLP'yi doğrudan** kabul eder (eski Thrift/agent gerekmez).
- Local'de Jaeger bellek içi depolama kullanır (restart'ta trace'ler silinir). Üretimde Jaeger için
  Elasticsearch/OpenSearch/Cassandra depolaması gerekir.

| | **Tempo** (+ Grafana) | **Jaeger** |
|-|-----------------------|------------|
| Depolama | Object storage (S3/GCS/disk), ucuz, indekssiz | Elasticsearch/OpenSearch/Cassandra (indeksli) |
| Arama | TraceQL (`{ span.http.route = "/api/products/{sku}" && duration > 200ms }`) | Servis / operasyon / tag / süre filtreleri |
| Korelasyon | Log ↔ trace ↔ metrik (exemplar) aynı Grafana'da | Kendi arayüzü; Grafana'ya datasource olarak da eklenebilir |
| Ekstra | Service graph, span metrics (metrics-generator) | Trace karşılaştırma, kritik yol, Monitor (SPM) sekmesi |
| VEON'da | **Standart** (observability bundle) | Ekiplerin alışkın olduğu arayüz / opsiyonel |

**Öneri:** Kalıcı backend olarak VEON standardı Tempo kalsın. Jaeger arayüzünü tercih eden ekipler için aynı
collector'dan fan-out yeterlidir. Uygulama tarafında iki ayrı exporter **kurulmaz**.

Jaeger'da bakılacak yerler: http://localhost:16686 → Service `veon-sample-backend` → **Find Traces**;
trace içinde **Trace Graph / Critical Path** görünümleri. Lab arayüzündeki her trace bağlantısının yanında
**"Jaeger ↗"** vardır. Grafana'da da `Jaeger` datasource'u tanımlıdır (Explore → Jaeger).

## Kurulum — [tracing.py](../backend/app/observability/tracing.py)

| Ayar | Değer | Neden |
|------|-------|-------|
| `OTEL_SDK_DISABLED` | varsayılan `true` | VEON imajlarıyla aynı: açıkça açılmadıkça sıfır maliyet |
| Exporter | OTLP HTTP/protobuf, timeout 2 sn | Collector yavaşsa uygulama beklemez |
| `BatchSpanProcessor` | kuyruk 2048, batch 128, export 2 sn | Sınırlı bellek; kuyruk dolarsa span **düşer**, istek etkilenmez |
| Sampler | `ParentBased(TraceIdRatio)` | Üst servis örneklediyse devam et; kök kararı oranla |
| `OTEL_TRACES_SAMPLER_ARG` | local `1.0`, üretim `0.05–0.1` | Maliyet / görünürlük dengesi |
| Resource | `service.name`, `service.version`, `service.instance.id`, `deployment.environment.name` | Tempo'da filtreleme ve servis grafiği |

**Process modeli:** Pod başına tek uvicorn process. `gunicorn --preload` veya fork öncesi provider kurulumu
YAPILMAZ (fork sonrası exporter thread'i ölür). Celery prefork kullanan servislerde `worker_process_init` içinde kurulur.

## Otomatik instrumentation

| Kütüphane | Instrumentor | Not |
|-----------|--------------|-----|
| FastAPI | `FastAPIInstrumentor.instrument_app` | `excluded_urls` ile health/polling uçları hariç; `exclude_spans=["receive","send"]` gürültüyü keser |
| redis-py (asyncio dahil) | `RedisInstrumentor` | Her komut bir CLIENT span'i; Lua `EVALSHA` olarak görünür |
| PyMongo **async** | `PymongoInstrumentor` | `AsyncMongoClient` ile context doğru yayılır (doğrulandı). Motor thread-pool kullandığı için context kaybolabilir — Motor zaten EOL. |

## Manuel span'ler ve VEON Alloy uyumu

İş açısından anlamlı adımlara manuel span açılır:

```python
with tracer.start_as_current_span(
    "flash_sale.reserve",
    attributes={"coform.operation": "flash_sale.reserve", "flash_sale.mode": "atomic"},
):
    ...
```

VEON üretim Alloy'u **gizlilik için span attribute allowlist** uygular ve span adlarını yeniden yazar:
yalnız `coform.operation`, `http.*`, `messaging.*`, `gen_ai.*`, `error.type` gibi alanlar kalır; `coform.operation`
varsa span adı ondan türetilir. Bu yüzden örnekteki tüm manuel span'ler `coform.operation` taşır ve mesajlaşma
span'leri OTel semantik konvansiyonlarını (`messaging.system`, `messaging.operation.type`) kullanır.

**Attribute'lara asla konmayacaklar:** prompt/yanıt, request/response body, kullanıcı kimliği, e-posta, token,
SQL/Mongo sorgu metni, ham URL+query. (Collector da siler ama ilk savunma uygulamadır.)

## Asenkron sınırda context taşıma (en kritik kısım)

HTTP'de `traceparent` header'ı otomatik taşınır. **Kuyrukta taşınmaz** — elle yapılmalıdır:

1. **Üretici** ([publisher.py](../backend/app/events/publisher.py)): olay oluşturulurken aktif context
   `TraceContextTextMapPropagator().inject(carrier)` ile alınır ve event alanı olarak (`traceparent`) yazılır.
2. **Tüketici** ([worker.py](../backend/app/worker.py)): her mesaj için `extract()` edilen context ile
   `events.process` (kind=CONSUMER) span'i açılır → üreticinin trace'ine bağlanır.
3. **Toplu yazım:** Bir Mongo `bulk_write` birden çok trace'e aittir. `events.persist` span'i ilk mesajın
   trace'inin çocuğu olur, diğer mesajlara **span link** verilir.
   > VEON üretim Alloy'u şu an link'leri siler (`set(links, nil)`); bu yüzden "ilk mesajın çocuğu" tercihi
   > tek-mesajlık akışlarda uçtan uca görünürlüğü korur.
4. Trace bağlamı olmayan toplu/sentetik olaylarda (burst) worker span açmaz — binlerce kök trace üretilmez.
5. Mongo'ya yazılan her olay `trace_id` alanı taşır → audit kaydından Tempo'ya geçiş.

## Gürültü ve maliyet kontrolü

- Health/readiness/polling uçları (`/healthz`, `/readyz`, `/api/system/heartbeat` …) trace üretmez.
- Arka plan döngüleri (stream polling, flush) `untraced()` içinde çalışır. **Tespit:** redis instrumentation
  `suppress_instrumentation`'a uymuyor; bu yüzden `untraced()` örneklenmemiş (sampled=0) bir parent context
  aktive eder → `ParentBased` sampler altındaki tüm span'ler kayıt dışı kalır. Aksi halde worker her 2 sn'de
  bir `XREADGROUP` için kök trace üretirdi.
- `untraced()` içinde yazılan loglara sahte trace id eklenmez.

## Tarayıcı tarafı

VEON politikası gereği tarayıcıdan OTLP'ye span **gönderilmez** (public OTLP ingress yok). Bunun yerine:

- Frontend her isteğe `X-Request-ID` ekler; nginx yoksa üretir ve backend'e iletir.
- Backend yanıta `X-Trace-Id` ve `X-Served-By` ekler; arayüz bunu "Tempo'da aç" bağlantısına çevirir.
- Destek talebinde kullanıcıdan istenecek tek şey `X-Request-ID` / `X-Trace-Id`'dir.

> Güvenilmeyen istemciden gelen `traceparent`, `ParentBased` sampler ile örnekleme kararını dışarıya bırakır.
> Public edge'de (ingress) gelen `traceparent`'ı sıfırlamak veya örnekleme kararını edge'de vermek düşünülmelidir.

## Sinyaller arası geçiş

| Nereden | Nereye | Nasıl |
|---------|--------|-------|
| Log satırı | Trace | Loki derived field: `"otel_trace_id"\s*:\s*"([a-f0-9]{32})"` → Tempo |
| Metrik (histogram) | Trace | Exemplar `trace_id` → Grafana "Query with Tempo" |
| Trace | Loglar | Tempo/Jaeger `tracesToLogsV2` → Loki: `{job="fluent-bit"} \|= "<traceId>"` |
| Trace | Servis grafiği | Tempo metrics-generator → Prometheus → Grafana "Service Graph" |
| Mongo audit kaydı | Trace | `events.trace_id` alanı |

## Doğrulama

```bash
TID=$(curl -s -D - -o /dev/null -X POST localhost:8080/api/flash-sale/SKU-FLASH/reserve \
  -H 'Content-Type: application/json' -d '{"user_id":"u1"}' | awk -F': ' 'tolower($1)=="x-trace-id"{print $2}' | tr -d '\r')
echo "http://localhost:3000/explore → Tempo → $TID"
```
