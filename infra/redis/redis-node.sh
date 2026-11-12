#!/bin/sh
# Redis veri düğümü (master veya replica) başlatıcı.
# Parola komut satırına değil config dosyasına yazılır (`ps` çıktısında görünmez).
set -eu

: "${REDIS_PASSWORD:?REDIS_PASSWORD gerekli}"
: "${ANNOUNCE_HOST:?ANNOUNCE_HOST gerekli}"

CONF=/tmp/redis.conf
umask 077
cat > "$CONF" <<EOF
port 6379
bind * -::*
dir /data

# --- Kimlik doğrulama ---
requirepass ${REDIS_PASSWORD}
masterauth ${REDIS_PASSWORD}

# --- Replikasyon / Sentinel ---
# Container IP'leri yeniden başlatmada değişir; Sentinel'e hostname duyurulur.
replica-announce-ip ${ANNOUNCE_HOST}
replica-read-only yes
# Split-brain/veri kaybı penceresini daralt: en az 1 replika 10 sn içinde
# senkron değilse master YAZMAYI REDDEDER (izole kalmış eski master'a yazılmaz).
min-replicas-to-write 1
min-replicas-max-lag 10

# --- Kalıcılık (stream, kilit, idempotency, stok sayacı burada) ---
appendonly yes
appendfsync everysec

# --- Bellek ---
# Bu instance kalıcı-ish veri (stream, idempotency, stok) taşıdığı için eviction YOK.
# Saf cache için AYRI bir instance + allkeys-lru kullanılmalı (bkz. docs/04).
maxmemory 256mb
maxmemory-policy noeviction

# --- Gözlemlenebilirlik ---
slowlog-log-slower-than 10000
latency-monitor-threshold 100
EOF

if [ -n "${REPLICA_OF:-}" ]; then
  echo "replicaof ${REPLICA_OF} 6379" >> "$CONF"
fi

exec redis-server "$CONF"
