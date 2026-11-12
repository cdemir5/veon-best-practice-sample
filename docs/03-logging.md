# 3 · Logging

## İlkeler

1. **Tek satır JSON, stdout'a.** Dosyaya, Mongo'ya, ES'e veya Loki'ye uygulamadan doğrudan yazılmaz.
2. **İstek akışı log yüzünden asla beklemez.** Log yazımı ayrı thread'de, sınırlı kuyrukla; kuyruk dolarsa
   kayıt düşer ve **sayılır** (`app_logs_dropped_total`).
3. **Her log trace'e bağlanabilir.** `otel_trace_id`, `otel_span_id`, `request_id` otomatik eklenir.
4. **İstek başına tek access log satırı** (route şablonu, status, süre). "Başladı" + "bitti" çifti yok.
5. **Kişisel/gizli veri loglanmaz.** Body, query string, header, client IP, token yok; maskeleme ikinci savunmadır.
6. Taşıma, buffer, retry, maskeleme ve yönlendirme **collector'ın** işidir (Alloy / Fluent Bit).

## Log şeması

```json
{"ts":"2026-11-11T20:38:03.161Z","level":"INFO","logger":"app.access","message":"http_request",
 "service":"veon-sample-backend","version":"0.1.0","env":"local",
 "http_method":"GET","http_route":"/api/products/{sku}","http_status":200,"duration_ms":4.87,
 "otel_trace_id":"6af1d4decc48565a27828da473d451d2","otel_span_id":"…","request_id":"…"}
```

| Alan | Kaynak | Not |
|------|--------|-----|
| `ts`, `level`, `logger`, `message` | LogRecord | `message` sabit/az değişkenli olsun (`"product updated"`), değerler `extra=` ile |
| `service`, `version`, `env` | ayarlar | Loki label'ı `service` buradan |
| `otel_trace_id`, `otel_span_id` | aktif span | Alan adı VEON Grafana Loki derived-field regex'iyle aynı |
| `request_id` | `X-Request-ID` (nginx) | Trace örneklenmese de korelasyon sağlar |
| `error_type`, `stack` | `exc_info` | `logger.exception(...)` |
| diğerleri | `extra={...}` | düz, kısa, sınırlı değerler |

Kullanım:

```python
logger.info("product updated", extra={"sku": sku, "version": version})   # ✅
logger.info(f"product {sku} updated to {body}")                           # ❌ body + f-string
```

## Bloklamayan logging — [logging.py](../backend/app/observability/logging.py)

```
logger.info(...)               (istek task'ı — event loop)
  └─ QueueHandler
       ├─ ContextFilter   → trace/request id burada okunur (ContextVar'lar bu thread'de geçerli)
       ├─ JsonFormatter   → JSON + redaction (CPU, ucuz)
       └─ queue.Queue(maxsize=10000).put_nowait   → dolu ise DÜŞÜR + say
QueueListener thread'i → sys.stdout.write   (I/O burada; pipe dolsa bile event loop bloklanmaz)
```

İnce nokta: format işlemi **çağıran tarafta** yapılır. QueueListener thread'inde yapılsaydı
OTel context'i ve `request_id` ContextVar'ı orada boş olurdu.

uvicorn'un kendi handler'ları kaldırılıp root'a yönlendirilir; `uvicorn.access` kapatılır (access log'u
middleware route şablonu ve trace id ile yazar). Kütüphane gürültüsü (`pymongo`, `httpx`) WARNING'e çekilir.

## Maskeleme — [redaction.py](../backend/app/observability/redaction.py)

- Anahtar bazlı: `password`, `token`, `api_key`, `authorization`, `cookie`, `mongo_uri` … → `[REDACTED]`
- Metin kalıpları: `Bearer …`, `token=…`, `://user:pass@`, e-posta → maskelenir; uzun metin kesilir.
- Collector'da ikinci hat: Fluent Bit Lua filtresi ([redact.lua](../infra/fluent-bit/redact.lua)), VEON Alloy `loki.process "redact"`.
- **Maskeleme best-effort'tur.** Asıl kural veriyi loga hiç koymamaktır.

## Toplama: stdout → collector → backend

### Local (Docker)

```yaml
logging:
  driver: fluentd
  options:
    fluentd-address: localhost:24224
    fluentd-async: "true"
    mode: non-blocking        # sürücü yavaşsa uygulamanın stdout'u BLOKLANMAZ
    max-buffer-size: 4m
    labels: com.docker.compose.service
```

`docker compose logs` çalışmaya devam eder (Docker dual logging).

### Kubernetes

Uygulama chart'ı log için **hiçbir şey kurmaz**; node'daki DaemonSet `/var/log/containers` dosyalarını okur:
- VEON: Grafana **Alloy** → Loki (observability bundle, `alloy.logNamespaces` ile namespace allowlist)
- Alternatif/legacy: **Fluent Bit** → Loki veya Elasticsearch ([examples/fluent-bit-efk.values.yaml](../deploy/helm/examples/fluent-bit-efk.values.yaml))

> ⚠️ Aynı namespace'i iki toplayıcı izlerse loglar **çiftlenir**. Geçiş namespace bazında yapılır,
> eski toplayıcıda o namespace hariç tutulur (VEON bundle README'sindeki "Log cutover" adımları).

## Loki mi, ELK/EFK mi?

| | Loki (+ Alloy/Fluent Bit) | Elasticsearch (EFK/ELK) |
|-|---------------------------|--------------------------|
| İndeksleme | Yalnız **label**'lar; gövde sıkıştırılmış chunk | Tüm alanlar (tam metin indeks) |
| Maliyet | Düşük depolama/RAM | Yüksek (indeks, JVM heap) |
| Sorgu | Label ile daralt + gövdede grep/`| json` | Serbest alan sorgusu, agregasyon, Kibana |
| Uygun olduğu yer | Operasyonel loglar, trace korelasyonu, Grafana ekosistemi | Uzun saklama, karmaşık analitik/arama, güvenlik/SIEM |
| VEON'da | Varsayılan (bundle ile gelir) | İhtiyaç varsa ek hedef |

**Pratik öneri:** Operasyonel loglar Loki'de (VEON standardı). Analitik/uyum gereksinimi olan akışlar için
collector **fan-out** ile aynı kaydı ES'e de gönderebilir — bu repodaki `make up-efk` tam olarak bunu yapar:
tek Fluent Bit, iki çıkış (Loki + ES). Uygulama kodunda değişiklik yoktur.

## Loki label kuralları

- Label = **düşük kardinalite**: `service`, `level`, `namespace`, `app`, `container`.
- `trace_id`, `request_id`, `user_id`, `sku` → **gövdede** (LogQL: `|= "<id>"` veya `| json | otel_trace_id="…"`).
- Örnek sorgular:
  ```logql
  {service="veon-sample-backend"} | json | level="ERROR"
  {service=~"veon-sample-.+"} |= "6af1d4decc48565a27828da473d451d2"
  sum by (http_route) (rate({service="veon-sample-backend"} | json | http_status >= 500 [5m]))
  ```

## Buffer, backpressure, kayıp

| Katman | Mekanizma | Kayıp durumu |
|--------|-----------|--------------|
| Uygulama | Sınırlı kuyruk (10k) | Doluysa düşer → `app_logs_dropped_total` alarmı |
| Docker sürücüsü | `mode: non-blocking`, 4 MB | Doluysa en eski düşer |
| Fluent Bit | `storage.type filesystem`, `storage.total_limit_size` | Disk limiti aşılırsa |
| ES çıkışı | `Generate_ID On` | Retry'da çift kayıt olmaz |
| Loki / Alloy | write buffer (VEON pilotunda WAL kapalı) | Uzun kesintide; `loki_write_dropped_entries_total` alarmı |

Loglar **"en iyi çaba"** ile taşınır. Kaybı kabul edilemeyen kayıtlar (audit, faturalama) log değil
**olaydır** → [05-async-log-pipeline.md](05-async-log-pipeline.md).

## Saklama

- Loki: `limits_config.retention_period` + compactor retention (VEON pilot: 7 gün).
- ES: ILM politikası (hot → warm → delete) ve index template; `Logstash_Format` günlük index üretir.
- Audit olayları: Mongo TTL index (`EVENTS_RETENTION_DAYS`, varsayılan 30 gün).
