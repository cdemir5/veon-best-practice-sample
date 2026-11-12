"""Bağımlılık gerektirmeyen birim testleri."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

from opentelemetry.sdk.trace import TracerProvider
from redis.crc import key_slot

from app.observability.logging import ContextFilter, JsonFormatter
from app.observability.redaction import redact_sensitive, redact_text
from app.redis_kit.keys import Keys
from app.worker import to_document


def test_redaction_masks_keys_and_patterns() -> None:
    data = {
        "password": "p@ss",
        "nested": {"Authorization": "Bearer abc.def"},
        "note": "uri=mongodb://user:secret@db:27017 token=xyz mail me: a.b@example.com",
    }
    result = redact_sensitive(data)
    assert result["password"] == "[REDACTED]"
    assert result["nested"]["Authorization"] == "[REDACTED]"
    assert "secret" not in result["note"]
    assert "xyz" not in result["note"]
    assert "a.b@example.com" not in result["note"]
    assert data["password"] == "p@ss"  # girdi değişmemeli


def test_redact_text_bearer() -> None:
    assert redact_text("Authorization: Bearer eyJhbGciOi.x.y") == "Authorization: Bearer [REDACTED]"


def test_json_formatter_adds_trace_context(monkeypatch) -> None:
    # OTEL_SDK_DISABLED=true iken SDK no-op tracer döndürür (beklenen davranış).
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    tracer = TracerProvider().get_tracer("test")
    formatter = JsonFormatter(service="svc", version="1", environment="test")
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.sku = "SKU-1"
    with tracer.start_as_current_span("unit") as span:
        ContextFilter().filter(record)
        trace_id = format(span.get_span_context().trace_id, "032x")
    line = formatter.format(record)
    document = json.loads(line)
    assert document["message"] == "hello world"
    assert document["otel_trace_id"] == trace_id
    assert document["sku"] == "SKU-1"
    assert document["service"] == "svc"
    # Grafana/Loki derived field regex'i ile uyumlu biçim
    assert f'"otel_trace_id": "{trace_id}"' in line


def test_digest_is_stable_across_processes_unlike_builtin_hash() -> None:
    """agentic-core auth cache anahtarı `hash(token)` kullanıyor: her process'te farklı.

    Aynı token iki pod'da iki farklı Redis anahtarı üretir → cache paylaşılmaz,
    restart sonrası eski anahtarlar TTL dolana kadar çöp olarak kalır.
    """
    code = "from app.redis_kit.keys import digest; print(digest('token-123'), hash('token-123'))"
    outputs = [
        subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        for seed in ("1", "2")
    ]
    assert outputs[0][0] == outputs[1][0]  # sha256 tabanlı digest: her yerde aynı
    assert outputs[0][1] != outputs[1][1]  # yerleşik hash(): process'e göre değişir


def test_flash_sale_keys_share_cluster_slot() -> None:
    keys = Keys("vsa")
    assert key_slot(keys.flash_stock("SKU-1").encode()) == key_slot(
        keys.flash_buyers("SKU-1").encode()
    )


def test_to_document_parses_stream_message() -> None:
    fields = {
        "event_id": "e1",
        "type": "flash_sale.reserved",
        "ts": "1700000000.5",
        "source": "api",
        "payload": '{"sku":"SKU-1"}',
        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        "request_id": "",
    }
    document = to_document("1-0", fields)
    assert document["event_id"] == "e1"
    assert document["payload"] == {"sku": "SKU-1"}
    assert document["trace_id"] == "a" * 32
    assert "request_id" not in document  # boş alan yazılmaz (sparse index dostu)


def test_api_and_worker_modules_import(monkeypatch) -> None:
    """Uygulama modülleri import edilebilmeli (pydantic şema/regex hataları build'de yakalanır)."""
    import importlib

    monkeypatch.setenv("LAB_ENDPOINTS_ENABLED", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    main = importlib.import_module("app.main")
    importlib.import_module("app.worker")
    routes = {getattr(route, "path", "") for route in main.app.routes}
    assert "/api/lab/fanout" in routes
    assert "/api/flash-sale/{sku}/reserve" in routes
