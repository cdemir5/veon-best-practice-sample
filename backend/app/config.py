"""Uygulama ayarları (12-factor: tüm konfigürasyon ortam değişkenlerinden).

Ayar isimleri mevcut agentic-core ile uyumludur:
  REDIS_MODE, REDIS_URL, REDIS_SENTINELS (JSON liste), REDIS_SENTINEL_MASTER_NAME,
  REDIS_PASSWORD, REDIS_SENTINEL_PASSWORD, OTEL_*, APP_VERSION, DEPLOYMENT_ENVIRONMENT.

Gizli değerler (parola, URI) SecretStr olarak tutulur; repr/log çıktısında görünmez.
"""
from __future__ import annotations

import socket
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

HOSTNAME = socket.gethostname()


def secret_value(value: SecretStr | None) -> str | None:
    """Boş string'i None'a çevirerek SecretStr'ın içeriğini döndürür."""
    if value is None:
        return None
    raw = value.get_secret_value()
    return raw or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # --- Kimlik -----------------------------------------------------------
    otel_service_name: str = "veon-sample-backend"
    app_version: str = "0.1.0"
    deployment_environment: str = "local"

    # --- Logging ----------------------------------------------------------
    log_level: str = "INFO"
    log_queue_size: int = 10_000

    # --- Tracing (exporter endpoint'i OTEL_EXPORTER_OTLP_ENDPOINT'ten SDK okur) ---
    otel_sdk_disabled: bool = True  # VEON imajlarıyla aynı: varsayılan kapalı, açıkça açılır
    otel_traces_sampler_arg: float = Field(0.1, ge=0.0, le=1.0)

    # --- Metrics ----------------------------------------------------------
    metrics_port: int = 9100

    # --- Lab (yalnız demo/dev ortamı) -------------------------------------
    lab_endpoints_enabled: bool = False
    lab_fanout_base_url: str = "http://localhost:8000"
    simulated_db_latency_ms: int = 0

    # --- Redis ------------------------------------------------------------
    redis_mode: Literal["standalone", "sentinel"] = "standalone"
    redis_url: str = "redis://localhost:6379/0"
    redis_sentinels: list[str] = []
    redis_sentinel_master_name: str = "mymaster"
    redis_password: SecretStr | None = None
    redis_sentinel_password: SecretStr | None = None
    redis_socket_timeout: float = 0.5
    redis_connect_timeout: float = 0.5
    redis_max_connections: int = 100
    redis_pool_timeout: float = 1.0
    redis_retries: int = 5
    redis_health_check_interval: int = 15
    redis_key_prefix: str = "vsa"

    # --- MongoDB ----------------------------------------------------------
    mongo_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    mongo_db: str = "veon_sample"
    mongo_timeout_ms: int = 3000
    mongo_max_pool_size: int = 50

    # --- Cache ------------------------------------------------------------
    cache_ttl_seconds: int = 60
    cache_negative_ttl_seconds: int = 15
    cache_lock_ttl_ms: int = 3000
    cache_wait_timeout_ms: int = 2000

    # --- Asenkron event/log pipeline'ı -------------------------------------
    events_stream_maxlen: int = 100_000
    events_queue_size: int = 10_000
    events_batch_size: int = 200
    events_flush_interval_ms: int = 200
    events_retention_days: int = 30

    # --- Worker -----------------------------------------------------------
    worker_group: str = "audit-persister"
    worker_batch_size: int = 200
    worker_block_ms: int = 2000
    worker_claim_idle_ms: int = 30_000
    worker_max_deliveries: int = 5
    worker_heartbeat_file: str = "/tmp/worker-heartbeat"

    # --- Migration --------------------------------------------------------
    seed_demo_data: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
