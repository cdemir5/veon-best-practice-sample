"""Bloklamayan event yayıncısı: bellek kuyruğu → toplu XADD → Redis Stream.

agentic-core'da `LOG_RESULTS_TYPE=mongo` iken her olay istek akışı içinde
`await collection.insert_one(...)` ile MongoDB'ye yazılıyor. Sonuçları:
  * Mongo yavaşlarsa/erişilemezse KULLANICI İSTEĞİ yavaşlar veya hata alır.
  * Her olay ayrı round-trip + ayrı yazma (batch yok).
  * Mongo bakımı sırasında olaylar kaybolur (buffer yok).

Bu sınıfta:
    publish()  →  O(1), senkron, asla beklemez  → asyncio.Queue(maxsize)
                  kuyruk doluysa event DÜŞÜRÜLÜR + events_dropped_total artar
    _run()     →  arka planda: kuyruktan batch al → pipeline XADD MAXLEN ~ N
                  Redis hatasında 20 sn bütçeli backoff (failover penceresi), sonra düşür + metrik
    worker     →  consumer group ile okur, MongoDB'ye bulk upsert, ACK

Garanti modeli: at-least-once (tekrar gönderim olabilir) + idempotent consumer
(event_id unique index + $setOnInsert) ⇒ pratikte "tam bir kez" kayıt.
Process çökmesinde bellek kuyruğundaki birkaç yüz ms'lik olay kaybolabilir;
bu kabul edilemiyorsa olay iş transaction'ı ile aynı yere yazılmalı (outbox pattern).

Trace bağlamı: event'e W3C `traceparent` eklenir → worker aynı trace'e devam eder.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.context import request_id_var
from app.observability.metrics import (
    EVENTS_DROPPED,
    EVENTS_ENQUEUED,
    EVENTS_FLUSH_SECONDS,
    EVENTS_PUBLISHED,
    EVENTS_QUEUE_SIZE,
    REDIS_ERRORS,
)
from app.observability.redaction import redact_sensitive
from app.observability.tracing import untraced
from app.redis_kit.keys import Keys

logger = logging.getLogger(__name__)
_propagator = TraceContextTextMapPropagator()

_FLUSH_RETRY_BUDGET_SECONDS = 20.0


class EventPublisher:
    def __init__(self, redis: Redis, keys: Keys, settings: Settings, *, source: str) -> None:
        self._redis = redis
        self._stream = keys.events_stream()
        self._maxlen = settings.events_stream_maxlen
        self._batch_size = settings.events_batch_size
        self._idle_wait = settings.events_flush_interval_ms / 1000
        self._source = source
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue(
            maxsize=settings.events_queue_size
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Bu replikaya ait basit sayaçlar (lab ekranı için; metrikler Prometheus'ta).
        self.stats = {"enqueued": 0, "published": 0, "dropped": 0}

    # ------------------------------------------------------------------ public
    def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        propagate_trace: bool = True,
    ) -> bool:
        """İstek akışından çağrılır. ASLA I/O yapmaz, ASLA bloklamaz.

        propagate_trace=False: toplu/sentetik olaylarda (binlerce event) tek trace'i
        şişirmemek için trace bağlamı eklenmez.
        """
        carrier: dict[str, str] = {}
        if propagate_trace:
            _propagator.inject(carrier)
        event = {
            # Deterministik id verilirse (ör. reservation_id) tekrarlar worker'da elenir.
            "event_id": event_id or uuid.uuid4().hex,
            "type": event_type,
            "ts": f"{time.time():.6f}",
            "source": self._source,
            "request_id": request_id_var.get() or "",
            "traceparent": carrier.get("traceparent", ""),
            "payload": json.dumps(redact_sensitive(payload), default=str, separators=(",", ":")),
        }
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            EVENTS_DROPPED.labels("queue_full").inc()
            self.stats["dropped"] += 1
            return False
        EVENTS_ENQUEUED.inc()
        self.stats["enqueued"] += 1
        EVENTS_QUEUE_SIZE.set(self._queue.qsize())
        return True

    def queue_size(self) -> int:
        return self._queue.qsize()

    def queue_capacity(self) -> int:
        return self._queue.maxsize

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="event-publisher")

    async def stop(self, timeout: float = 5.0) -> None:
        """Graceful shutdown: kuyruğu boşaltmaya çalış, süre dolarsa kalanları say."""
        self._stopping.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout)
        except TimeoutError:
            self._task.cancel()
            remaining = self._queue.qsize()
            if remaining:
                EVENTS_DROPPED.labels("shutdown").inc(remaining)
                logger.warning("events lost on shutdown", extra={"events": remaining})

    # ---------------------------------------------------------------- internal
    async def _run(self) -> None:
        with untraced():  # her flush için kök trace üretme
            while True:
                batch = await self._next_batch()
                if batch:
                    await self._flush(batch)
                elif self._stopping.is_set():
                    return

    async def _next_batch(self) -> list[dict[str, str]]:
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=self._idle_wait)
        except TimeoutError:
            return []
        batch = [first]
        # Yük altında batch'ler doğal olarak büyür; düşük yükte gecikme eklenmez.
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        EVENTS_QUEUE_SIZE.set(self._queue.qsize())
        return batch

    async def _flush(self, batch: list[dict[str, str]]) -> None:
        """Zaman BÜTÇELİ retry: Sentinel failover penceresini (ölçülen: sert kesinti ~2 sn,
        kontrollü failover'da NOREPLICAS ile ~9 sn) atlatacak kadar dener.
        Bu sürede yeni olaylar bellek kuyruğunda birikir; istek akışı etkilenmez."""
        deadline = time.monotonic() + _FLUSH_RETRY_BUDGET_SECONDS
        delay = 0.2
        while True:
            started = time.perf_counter()
            try:
                async with self._redis.pipeline(transaction=False) as pipe:
                    for event in batch:
                        # MAXLEN ~ : stream sınırsız büyümez (yaklaşık kırpma, O(1)).
                        pipe.xadd(self._stream, event, maxlen=self._maxlen, approximate=True)
                    await pipe.execute()
            except RedisError as exc:
                REDIS_ERRORS.labels("xadd", type(exc).__name__).inc()
                if time.monotonic() + delay > deadline:
                    break
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:  # stop() zaman aşımı: elimizdeki batch kayıp
                    EVENTS_DROPPED.labels("shutdown").inc(len(batch))
                    raise
                delay = min(delay * 2, 2.0)
                continue
            EVENTS_FLUSH_SECONDS.observe(time.perf_counter() - started)
            EVENTS_PUBLISHED.inc(len(batch))
            self.stats["published"] += len(batch)
            return
        EVENTS_DROPPED.labels("redis_unavailable").inc(len(batch))
        self.stats["dropped"] += len(batch)
        logger.error("event batch dropped: redis unavailable", extra={"events": len(batch)})
