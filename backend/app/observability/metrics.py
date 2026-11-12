"""Prometheus metrikleri.

Kurallar:
  * Label değerleri SINIRLI kümeden gelir (route şablonu, status, sonuç tipi).
    ASLA ham URL path, kullanıcı id, session id, sku vb. label yapılmaz
    (agentic-core'daki `path=request.url.path` cardinality patlamasına yol açar).
  * service / env / pod gibi kimlik label'ları uygulamada değil, scrape tarafında
    (Prometheus target label'ları) eklenir.
  * Histogram'larda exemplar olarak trace_id taşınır → Grafana'da metrikten trace'e geçiş.
  * Metrikler ayrı portta (9100) yayınlanır; Ingress'e açılmaz, NetworkPolicy ile korunur.
"""
from __future__ import annotations

from opentelemetry import trace
from prometheus_client import Counter, Gauge, Histogram, start_http_server

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

APP_INFO = Gauge(
    "app_info", "Build/sürüm bilgisi (değer daima 1)", ["component", "version", "env"]
)

# --- HTTP (RED: Rate, Errors, Duration) ----------------------------------------
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP istek sayısı", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP istek süresi",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)
HTTP_IN_PROGRESS = Gauge(
    "http_requests_in_progress", "Şu an işlenen HTTP istekleri", ["method"]
)

# --- Cache / Redis ---------------------------------------------------------------
CACHE_REQUESTS = Counter(
    "cache_requests_total",
    "Cache erişimleri (hit|miss|coalesced|bypass|timeout_fallback)",
    ["cache", "result"],
)
CACHE_LOAD_SECONDS = Histogram(
    "cache_load_duration_seconds",
    "Cache miss sonrası kaynaktan yükleme süresi",
    ["cache"],
    buckets=_LATENCY_BUCKETS,
)
DB_LOADS = Counter("db_loads_total", "Kaynağa (MongoDB) giden okuma sayısı", ["entity"])
REDIS_ERRORS = Counter(
    "redis_errors_total", "Redis hataları", ["operation", "error"]
)
LOCK_ACQUIRE = Counter(
    "redis_lock_acquire_total", "Dağıtık kilit denemeleri", ["lock", "result"]
)
RATE_LIMIT_DECISIONS = Counter(
    "rate_limit_decisions_total", "Rate limit kararları", ["limiter", "decision"]
)
IDEMPOTENCY_REQUESTS = Counter(
    "idempotency_requests_total", "Idempotency-Key sonuçları", ["scope", "outcome"]
)
FLASH_SALE_RESERVATIONS = Counter(
    "flash_sale_reservations_total", "Flash-sale rezervasyon sonuçları", ["mode", "result"]
)

# --- Event pipeline: producer (API) ---------------------------------------------
EVENTS_ENQUEUED = Counter("events_enqueued_total", "Bellek kuyruğuna alınan event")
EVENTS_PUBLISHED = Counter("events_published_total", "Redis Stream'e yazılan event")
EVENTS_DROPPED = Counter(
    "events_dropped_total", "Kaybedilen event (queue_full|redis_unavailable|shutdown)", ["reason"]
)
EVENTS_QUEUE_SIZE = Gauge("events_queue_size", "Bellek kuyruğundaki event sayısı")
EVENTS_FLUSH_SECONDS = Histogram(
    "events_flush_duration_seconds", "XADD pipeline flush süresi", buckets=_LATENCY_BUCKETS
)
LOGS_DROPPED = Counter(
    "app_logs_dropped_total", "Log kuyruğu dolduğu için düşürülen log kaydı"
)

# --- Event pipeline: consumer (worker) --------------------------------------------
EVENTS_CONSUMED = Counter("events_consumed_total", "Stream'den okunan event")
EVENTS_PERSISTED = Counter("events_persisted_total", "MongoDB'ye yazılan event")
EVENTS_FAILED = Counter("events_failed_total", "İşlenemeyen batch/event", ["reason"])
EVENTS_DEAD_LETTERED = Counter("events_dead_lettered_total", "DLQ'ya taşınan event")
EVENTS_RECLAIMED = Counter(
    "events_reclaimed_total", "Ölü/yavaş consumer'dan devralınan (XCLAIM) event"
)
EVENTS_BATCH_SECONDS = Histogram(
    "events_batch_duration_seconds", "Worker batch işleme süresi", buckets=_LATENCY_BUCKETS
)
EVENTS_END_TO_END_SECONDS = Histogram(
    "events_end_to_end_latency_seconds",
    "Event üretiminden MongoDB'ye yazılmasına kadar geçen süre",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0),
)
STREAM_LENGTH = Gauge("events_stream_length", "Stream uzunluğu (XLEN)")
STREAM_LAG = Gauge("events_stream_lag", "Consumer group'un henüz okumadığı event sayısı")
STREAM_PENDING = Gauge("events_stream_pending", "Okunmuş ama ACK'lenmemiş event (PEL)")
STREAM_DLQ_LENGTH = Gauge("events_dlq_length", "Dead-letter stream uzunluğu")
WORKER_LAST_SUCCESS = Gauge(
    "worker_last_success_timestamp_seconds", "Son başarılı batch zamanı (unix)"
)


def current_exemplar() -> dict[str, str] | None:
    """Aktif ve örneklenmiş bir span varsa exemplar olarak trace_id döndürür."""
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid and span_context.trace_flags.sampled:
        return {"trace_id": format(span_context.trace_id, "032x")}
    return None


def start_metrics_server(port: int) -> None:
    """/metrics'i ayrı portta yayınlar (OpenMetrics content negotiation → exemplar)."""
    start_http_server(port)
