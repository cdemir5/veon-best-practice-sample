"""Event worker: Redis Stream (consumer group) → MongoDB toplu upsert.

Çalıştırma:  python -m app.worker   (API ile aynı imaj, farklı komut)

Akış:
    XREADGROUP GROUP audit-persister <pod> COUNT 200 BLOCK 2000 STREAMS vsa:events:audit >
      → her mesaj için `events.process` span'i (traceparent ile ÜRETİCİNİN trace'ine bağlı)
      → bulk_write(UpdateOne(event_id, $setOnInsert, upsert), ordered=False)
      → XACK
    Her 10 sn bakım:
      * XPENDING IDLE 30sn  → ölü/takılmış consumer'ın mesajlarını XCLAIM ile devral
      * teslim sayısı ≥ 5   → zehirli mesaj → DLQ stream'ine taşı + XACK
      * XINFO GROUPS        → lag/pending metrikleri (KEDA/HPA ve alarm için)
      * uzun süredir boşta, PEL'i boş eski consumer kayıtlarını temizle

Garantiler:
  * At-least-once: ACK, Mongo yazımı BAŞARILI olduktan sonra yapılır.
  * İdempotent: event_id unique index + $setOnInsert → tekrar teslim zararsız.
  * Mongo kesintisi: mesajlar PEL'de bekler, kayıp yok; servis düzelince işlenir.
  * Yatay ölçek: replika sayısı arttıkça consumer group mesajları paylaştırır.
  * Graceful shutdown: SIGTERM → mevcut batch biter, ACK'lenir, çıkılır.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Link, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pymongo import UpdateOne
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import BulkWriteError, PyMongoError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from app.config import HOSTNAME, Settings, get_settings
from app.db.mongo import build_mongo
from app.observability.logging import setup_logging, shutdown_logging
from app.observability.metrics import (
    APP_INFO,
    EVENTS_BATCH_SECONDS,
    EVENTS_CONSUMED,
    EVENTS_DEAD_LETTERED,
    EVENTS_END_TO_END_SECONDS,
    EVENTS_FAILED,
    EVENTS_PERSISTED,
    EVENTS_RECLAIMED,
    STREAM_DLQ_LENGTH,
    STREAM_LAG,
    STREAM_LENGTH,
    STREAM_PENDING,
    WORKER_LAST_SUCCESS,
    start_metrics_server,
)
from app.observability.tracing import setup_tracing, shutdown_tracing, tracer, untraced
from app.redis_kit.client import build_redis
from app.redis_kit.keys import Keys

logger = logging.getLogger("app.worker")
_propagator = TraceContextTextMapPropagator()

_MAINTENANCE_INTERVAL = 10.0
_STALE_CONSUMER_IDLE_MS = 3600 * 1000
_DLQ_MAXLEN = 10_000

Prepared = tuple[str, dict[str, Any], trace.Span]


def to_document(message_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """Stream mesajını Mongo belgesine çevirir; bozuk mesajda ValueError/KeyError."""
    document: dict[str, Any] = {
        "event_id": fields["event_id"],
        "type": fields["type"],
        "source": fields.get("source"),
        "payload": json.loads(fields.get("payload") or "{}"),
        "ts": datetime.fromtimestamp(float(fields["ts"]), tz=timezone.utc),
        "stream_id": message_id,
        "persisted_at": datetime.now(timezone.utc),
    }
    parts = (fields.get("traceparent") or "").split("-")
    if len(parts) == 4 and len(parts[1]) == 32:
        document["trace_id"] = parts[1]  # Mongo kaydından Tempo'daki trace'e geçiş
    if fields.get("request_id"):
        document["request_id"] = fields["request_id"]
    return document


class EventConsumer:
    def __init__(
        self,
        *,
        redis: Redis,
        stream_redis: Redis,
        db: AsyncDatabase,
        keys: Keys,
        settings: Settings,
        consumer_name: str,
    ) -> None:
        self._redis = redis
        self._stream_redis = stream_redis  # BLOCK için uzun socket timeout'lu ayrı client
        self._db = db
        self._stream = keys.events_stream()
        self._dlq = keys.events_dlq()
        self._group = settings.worker_group
        self._consumer = consumer_name
        self._batch_size = settings.worker_batch_size
        self._block_ms = settings.worker_block_ms
        self._claim_idle_ms = settings.worker_claim_idle_ms
        self._max_deliveries = settings.worker_max_deliveries
        self._heartbeat_file = settings.worker_heartbeat_file

    # ------------------------------------------------------------------ main loop
    async def run(self, stop: asyncio.Event) -> None:
        await self._ensure_group()
        await self._drain_own_pending()
        last_maintenance = 0.0
        while not stop.is_set():
            self._touch_heartbeat()
            try:
                if time.monotonic() - last_maintenance >= _MAINTENANCE_INTERVAL:
                    await self._maintenance()
                    last_maintenance = time.monotonic()
                with untraced():
                    response = await self._stream_redis.xreadgroup(
                        self._group,
                        self._consumer,
                        {self._stream: ">"},
                        count=self._batch_size,
                        block=self._block_ms,
                    )
                if response:
                    await self._process(response[0][1])
            except ResponseError as exc:
                if "NOGROUP" in str(exc):  # stream/grup silinmişse yeniden oluştur
                    await self._ensure_group()
                    continue
                await self._backoff(stop, "redis", exc)
            except RedisError as exc:
                await self._backoff(stop, "redis", exc)
            except PyMongoError as exc:
                # Mesajlar ACK'lenmedi → PEL'de kalır, bakım adımı tekrar dener.
                await self._backoff(stop, "mongodb", exc)

    # ------------------------------------------------------------------ processing
    async def _process(self, messages: list[tuple[str, dict[str, str] | None]]) -> None:
        started = time.perf_counter()
        prepared: list[Prepared] = []
        for message_id, fields in messages:
            if not fields:  # MAXLEN ile kırpılmış mesaj → yapılacak iş yok
                await self._redis.xack(self._stream, self._group, message_id)
                continue
            EVENTS_CONSUMED.inc()
            span = self._start_process_span(message_id, fields)
            try:
                document = to_document(message_id, fields)
            except (KeyError, ValueError) as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, "invalid_payload"))
                span.end()
                await self._dead_letter(message_id, fields, reason="invalid_payload")
                continue
            prepared.append((message_id, document, span))

        if not prepared:
            return
        try:
            await self._persist(prepared)
            await self._redis.xack(self._stream, self._group, *[mid for mid, _, _ in prepared])
        except Exception as exc:
            EVENTS_FAILED.labels("persist").inc(len(prepared))
            for _, _, span in prepared:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                span.end()
            raise

        now = time.time()
        for _, document, span in prepared:
            EVENTS_END_TO_END_SECONDS.observe(max(0.0, now - document["ts"].timestamp()))
            span.end()
        EVENTS_PERSISTED.inc(len(prepared))
        EVENTS_BATCH_SECONDS.observe(time.perf_counter() - started)
        WORKER_LAST_SUCCESS.set(now)

    def _start_process_span(self, message_id: str, fields: dict[str, str]) -> trace.Span:
        traceparent = fields.get("traceparent")
        if not traceparent:
            # Trace bağlamı olmayan (toplu/sentetik) olaylar için kök trace üretme.
            return trace.INVALID_SPAN
        return tracer.start_span(
            "events.process",
            context=_propagator.extract({"traceparent": traceparent}),
            kind=SpanKind.CONSUMER,
            attributes={
                "coform.operation": "events.process",
                "messaging.system": "redis",
                "messaging.operation.type": "process",
                "messaging.destination.name": self._stream,
                "messaging.message.id": message_id,
                "event.type": fields.get("type", ""),
            },
        )

    async def _persist(self, prepared: list[Prepared]) -> None:
        operations = [
            UpdateOne({"event_id": doc["event_id"]}, {"$setOnInsert": doc}, upsert=True)
            for _, doc, _ in prepared
        ]
        traced = [span for _, _, span in prepared if span.get_span_context().is_valid]
        if not traced:
            with untraced():
                await self._bulk_write(operations)
            return
        # Batch span'i: ilk mesajın trace'inin çocuğu, diğer mesajlara link.
        # (Böylece tek mesajlık akışta HTTP → stream → worker → Mongo tek trace'te görünür.)
        parent_context = trace.set_span_in_context(traced[0])
        links = [Link(span.get_span_context()) for span in traced[1:128]]
        with tracer.start_as_current_span(
            "events.persist",
            context=parent_context,
            links=links,
            attributes={
                "coform.operation": "events.persist",
                "messaging.batch.message_count": len(prepared),
            },
        ):
            await self._bulk_write(operations)

    async def _bulk_write(self, operations: list[UpdateOne]) -> None:
        try:
            await self._db["events"].bulk_write(operations, ordered=False)
        except BulkWriteError as exc:
            # E11000: eşzamanlı upsert yarışı → kayıt zaten var, idempotent başarı.
            errors = exc.details.get("writeErrors", [])
            if any(error.get("code") != 11000 for error in errors):
                raise

    # ------------------------------------------------------------------ recovery
    async def _drain_own_pending(self) -> None:
        """Yeniden başlayan pod, kendi adına bekleyen (ACK'lenmemiş) mesajları önce işler."""
        try:
            with untraced():
                response = await self._redis.xreadgroup(
                    self._group, self._consumer, {self._stream: "0"}, count=self._batch_size
                )
            if response and response[0][1]:
                logger.info("processing own pending events", extra={"events": len(response[0][1])})
                await self._process(response[0][1])
        except (RedisError, PyMongoError) as exc:
            logger.warning("own pending drain failed", extra={"error_type": type(exc).__name__})

    async def _maintenance(self) -> None:
        with untraced():
            pending = await self._redis.xpending_range(
                self._stream, self._group, min="-", max="+", count=100, idle=self._claim_idle_ms
            )
        limit = self._max_deliveries
        poison = [p for p in pending if p["times_delivered"] >= limit]
        retry_ids = [p["message_id"] for p in pending if p["times_delivered"] < limit]

        for entry in poison:
            message_id = entry["message_id"]
            with untraced():
                found = await self._redis.xrange(self._stream, message_id, message_id)
            fields = found[0][1] if found else {}
            await self._dead_letter(message_id, fields, reason="max_deliveries_exceeded")

        if retry_ids:
            with untraced():
                claimed = await self._redis.xclaim(
                    self._stream, self._group, self._consumer, self._claim_idle_ms, retry_ids
                )
            if claimed:
                EVENTS_RECLAIMED.inc(len(claimed))
                logger.warning("reclaimed stale pending events", extra={"events": len(claimed)})
                await self._process(claimed)

        await self._update_gauges()
        await self._cleanup_stale_consumers()

    async def _dead_letter(self, message_id: str, fields: dict[str, str], *, reason: str) -> None:
        entry = {
            **fields,
            "dlq_reason": reason,
            "dlq_original_id": message_id,
            "dlq_consumer": self._consumer,
            "dlq_ts": f"{time.time():.3f}",
        }
        with untraced():
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.xadd(self._dlq, entry, maxlen=_DLQ_MAXLEN, approximate=True)
                pipe.xack(self._stream, self._group, message_id)
                await pipe.execute()
        EVENTS_DEAD_LETTERED.inc()
        logger.error(
            "event moved to dead-letter stream",
            extra={"stream_id": message_id, "reason": reason, "event_type": fields.get("type")},
        )

    async def _update_gauges(self) -> None:
        with untraced():
            groups = await self._redis.xinfo_groups(self._stream)
            STREAM_LENGTH.set(await self._redis.xlen(self._stream))
            STREAM_DLQ_LENGTH.set(await self._redis.xlen(self._dlq))
        for group in groups:
            if group.get("name") == self._group:
                STREAM_PENDING.set(group.get("pending") or 0)
                if group.get("lag") is not None:  # Redis ≥ 7.0
                    STREAM_LAG.set(group["lag"])

    async def _cleanup_stale_consumers(self) -> None:
        """Her deploy'da yeni pod adı → eski consumer kayıtları birikmesin."""
        with untraced():
            consumers = await self._redis.xinfo_consumers(self._stream, self._group)
            for consumer in consumers:
                if (
                    consumer.get("name") != self._consumer
                    and consumer.get("pending") == 0
                    and (consumer.get("idle") or 0) > _STALE_CONSUMER_IDLE_MS
                ):
                    await self._redis.xgroup_delconsumer(
                        self._stream, self._group, consumer["name"]
                    )

    async def _ensure_group(self) -> None:
        try:
            # id="0": grup ilk kez oluşturulurken stream'de bekleyen olaylar da işlenir.
            await self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
            logger.info("consumer group created", extra={"group": self._group})
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # ------------------------------------------------------------------ helpers
    def _touch_heartbeat(self) -> None:
        try:
            with open(self._heartbeat_file, "w", encoding="ascii") as handle:
                handle.write(f"{time.time():.0f}")
        except OSError:
            pass

    async def _backoff(self, stop: asyncio.Event, dependency: str, exc: Exception) -> None:
        EVENTS_FAILED.labels(dependency).inc()
        logger.warning(
            "worker dependency error, backing off",
            extra={"dependency": dependency, "error_type": type(exc).__name__},
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    provider = setup_tracing(settings, service_name=settings.otel_service_name)
    start_metrics_server(settings.metrics_port)
    APP_INFO.labels("worker", settings.app_version, settings.deployment_environment).set(1)

    client_name = f"{settings.otel_service_name}:{HOSTNAME}"
    redis = build_redis(settings, client_name=client_name)
    # XREADGROUP BLOCK 2000 ms: socket timeout blok süresinden uzun OLMALI.
    stream_redis = build_redis(
        settings,
        client_name=f"{client_name}:reader",
        socket_timeout=settings.worker_block_ms / 1000 + 2,
    )
    mongo = build_mongo(settings, appname=settings.otel_service_name)
    consumer = EventConsumer(
        redis=redis.primary,
        stream_redis=stream_redis.primary,
        db=mongo[settings.mongo_db],
        keys=Keys(settings.redis_key_prefix),
        settings=settings,
        consumer_name=HOSTNAME,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info(
        "worker started",
        extra={"consumer": HOSTNAME, "group": settings.worker_group, "pid": os.getpid()},
    )
    try:
        await consumer.run(stop)
    finally:
        logger.info("worker stopping")
        await redis.close()
        await stream_redis.close()
        await mongo.close()
        shutdown_tracing(provider)


def main() -> None:
    settings = get_settings()
    setup_logging(
        level=settings.log_level,
        service=settings.otel_service_name,
        version=settings.app_version,
        environment=settings.deployment_environment,
        queue_size=settings.log_queue_size,
    )
    try:
        asyncio.run(run())
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
