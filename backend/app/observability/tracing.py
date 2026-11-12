"""OpenTelemetry tracing kurulumu.

VEON observability bundle ile uyumlu davranış:
  * OTEL_SDK_DISABLED=true (varsayılan) → hiçbir şey kurulmaz, sıfır maliyet.
  * Export: OTLP HTTP/protobuf → Alloy (:4318) → Tempo.
    Endpoint standart OTEL_EXPORTER_OTLP_ENDPOINT değişkeninden okunur.
  * Kuyruk sınırlı: 2048 span, 128'lik batch, 2 sn export timeout.
    Collector erişilemezse span'ler DÜŞER; iş akışı ve HTTP yanıtı ETKİLENMEZ.
  * Sampling: ParentBased(TraceIdRatio). Oran OTEL_TRACES_SAMPLER_ARG (varsayılan 0.1).
  * gunicorn --preload / fork öncesi init YAPILMAZ: her process kendi provider'ını kurar.
"""
from __future__ import annotations

import logging
import random
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from app.config import HOSTNAME, Settings
from app.context import untraced_var

logger = logging.getLogger(__name__)

# Uygulama genelinde kullanılan tracer (provider kurulmadıysa no-op'tur).
tracer = trace.get_tracer("veon.sample")


def setup_tracing(settings: Settings, *, service_name: str) -> TracerProvider | None:
    if settings.otel_sdk_disabled:
        logger.info("tracing disabled", extra={"reason": "OTEL_SDK_DISABLED=true"})
        return None

    # Import'lar burada: tracing kapalıyken exporter/instrumentation yüklenmez.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "veon-sample",
            "service.version": settings.app_version,
            "service.instance.id": HOSTNAME,
            "deployment.environment.name": settings.deployment_environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_arg)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(timeout=2),
            max_queue_size=2048,
            max_export_batch_size=128,
            schedule_delay_millis=1000,
            export_timeout_millis=2000,
        )
    )
    trace.set_tracer_provider(provider)

    # Otomatik instrumentation: Redis komutları ve Mongo komutları span olur.
    RedisInstrumentor().instrument(tracer_provider=provider)
    PymongoInstrumentor().instrument(tracer_provider=provider)

    logger.info(
        "tracing enabled",
        extra={"sampling_ratio": settings.otel_traces_sampler_arg, "service_name": service_name},
    )
    return provider


def shutdown_tracing(provider: TracerProvider | None) -> None:
    if provider is None:
        return
    provider.force_flush(timeout_millis=2000)
    provider.shutdown()


@contextmanager
def untraced() -> Iterator[None]:
    """Arka plan döngüleri (stream polling, heartbeat, flush) için tracing'i kapatır.

    Redis instrumentation `suppress_instrumentation`'a uymadığından, örneklenmemiş
    (sampled=0) bir parent span context'i aktif ediyoruz: ParentBased sampler bu
    context altındaki TÜM span'leri kayıt dışı bırakır. Böylece her 2 saniyede bir
    XREADGROUP için kök trace üretilmez (trace spam'i / maliyet önlenir).
    """
    span_context = SpanContext(
        trace_id=random.getrandbits(128),
        span_id=random.getrandbits(64),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.DEFAULT),
    )
    token = otel_context.attach(trace.set_span_in_context(NonRecordingSpan(span_context)))
    flag_token = untraced_var.set(True)
    try:
        with suppress_instrumentation():
            yield
    finally:
        untraced_var.reset(flag_token)
        otel_context.detach(token)


def trace_id_hex() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    return format(span_context.trace_id, "032x") if span_context.is_valid else None


def mark_span_error(exc: BaseException) -> None:
    span = trace.get_current_span()
    span.record_exception(exc)
    span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
