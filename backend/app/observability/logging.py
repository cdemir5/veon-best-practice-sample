"""Yapılandırılmış (JSON) ve request path'ini bloklamayan logging.

Akış:
    logger.info(...)                         (istek task'ı)
      └─ QueueHandler
           ├─ ContextFilter   → otel_trace_id / otel_span_id / request_id eklenir
           ├─ JsonFormatter   → tek satır JSON (+ redaction)
           └─ bounded queue   → doluysa kayıt DÜŞÜRÜLÜR ve sayılır (asla bloklamaz)
    QueueListener thread'i → stdout

    stdout ─► Docker fluentd driver / K8s node agent (Alloy, Fluent Bit)
          ─► Loki ve/veya Elasticsearch

Neden stdout?
  * Uygulama log backend'ini bilmez (12-factor). Loki/ES/Mongo değişse kod değişmez.
  * Log backend'i yavaşladığında/çöktüğünde istek gecikmesi etkilenmez.
  * Retry, buffer, backpressure, maskeleme collector'ın işidir.

Trace korelasyonu: `otel_trace_id` alan adı VEON Grafana Loki datasource'undaki
derived field regex'i ile birebir uyumludur:  "otel_trace_id"\\s*:\\s*"([a-f0-9]{32})"
"""
from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import queue
import sys
from datetime import datetime, timezone

from opentelemetry import trace

from app.context import request_id_var, untraced_var
from app.observability.metrics import LOGS_DROPPED
from app.observability.redaction import redact_sensitive

# LogRecord'un standart alanları; bunların dışındakiler `extra=` ile gelmiştir.
_RESERVED_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {
    "message",
    "asctime",
    "color_message",  # uvicorn'un ANSI renkli kopyası
}

_listener: logging.handlers.QueueListener | None = None


class ContextFilter(logging.Filter):
    """Çağıran task'ın context'inde çalışır → ContextVar/OTel context burada okunur.

    (QueueListener thread'inde okunsaydı trace/request id kaybolurdu.)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid and not untraced_var.get():
            record.otel_trace_id = format(span_context.trace_id, "032x")
            record.otel_span_id = format(span_context.span_id, "016x")
        request_id = request_id_var.get()
        if request_id is not None:
            record.request_id = request_id
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, *, service: str, version: str, environment: str) -> None:
        super().__init__()
        self._static = {"service": service, "version": version, "env": environment}

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **self._static,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_") and key not in document:
                document[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            document["error_type"] = record.exc_info[0].__name__
            document["stack"] = self.formatException(record.exc_info)
        return json.dumps(redact_sensitive(document), default=str, ensure_ascii=False)


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """Kuyruk doluysa beklemek yerine kaydı düşürür ve metriğe yazar."""

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            LOGS_DROPPED.inc()


def setup_logging(
    *, level: str, service: str, version: str, environment: str, queue_size: int = 10_000
) -> None:
    global _listener
    if _listener is not None:
        return

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=queue_size)
    queue_handler = _DroppingQueueHandler(log_queue)
    queue_handler.addFilter(ContextFilter())
    # Format işlemi (CPU) çağıran tarafta yapılır; I/O (stdout yazımı) listener thread'inde.
    queue_handler.setFormatter(
        JsonFormatter(service=service, version=version, environment=environment)
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(message)s"))
    _listener = logging.handlers.QueueListener(log_queue, stdout_handler)
    _listener.start()

    root = logging.getLogger()
    root.handlers[:] = [queue_handler]
    root.setLevel(level.upper())

    # uvicorn kendi handler'larını kurar; onları root'a yönlendiriyoruz.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # Access log'u kendi middleware'imiz (route şablonu + trace id ile) yazıyor.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("pymongo", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    atexit.register(shutdown_logging)


def shutdown_logging() -> None:
    """Kuyruktaki kayıtları stdout'a boşaltır (process çıkışında)."""
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None
