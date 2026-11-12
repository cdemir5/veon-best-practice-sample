"""Log ve event payload'larından hassas veriyi temizler.

Mevcut agentic-core `durable_messaging/redaction.py` ile aynı yaklaşım:
  1) anahtar adına göre maskeleme (password, token, api_key ...)
  2) serbest metinde kalıp (regex) maskeleme (Bearer, key=value, URL user:pass@, e-posta)

ÖNEMLİ: Bu katman "best effort"tür. Asıl kural; request/response body, prompt,
müşteri verisi ve kimlik bilgisini hiç loglamamaktır. Collector tarafındaki
(Alloy/Fluent Bit) maskeleme ikinci savunma hattıdır.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
MAX_STRING_LENGTH = 8192
_MAX_DEPTH = 8

SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "client_secret",
        "cookie",
        "mongo_uri",
        "password",
        "passwd",
        "refresh_token",
        "secret",
        "set-cookie",
        "token",
        "x-api-key",
    }
)

_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret)"
            r"(\s*[:=]\s*)[^\s,;&\"']+"
        ),
        r"\1\2[REDACTED]",
    ),
    # mongodb://user:pass@host, redis://:pass@host ...
    (re.compile(r"(://)[^/@\s]*:[^/@\s]*@"), r"\1[REDACTED]@"),
    # e-posta adresleri (KVKK/GDPR: kişisel veri)
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
)


def redact_text(text: str) -> str:
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > MAX_STRING_LENGTH:
        text = text[:MAX_STRING_LENGTH] + "...[TRUNCATED]"
    return text


def redact_sensitive(value: Any, _depth: int = 0) -> Any:
    """Değerin log-güvenli bir kopyasını döndürür (girdiyi değiştirmez)."""
    if _depth > _MAX_DEPTH:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        return {
            key: REDACTED
            if str(key).strip().lower() in SENSITIVE_KEYS
            else redact_sensitive(item, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive(item, _depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return f"[BINARY {len(value)} BYTES]"
    if isinstance(value, str):
        return redact_text(value)
    return value
