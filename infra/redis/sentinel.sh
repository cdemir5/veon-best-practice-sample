#!/bin/sh
# Redis Sentinel başlatıcı.
# Sentinel çalışırken config dosyasını YENİDEN YAZAR (güncel master, epoch, diğer
# sentinel'ler). Bu yüzden dosya yalnız ilk açılışta üretilir; container restart'ında
# Sentinel kaldığı yerden devam eder.
set -eu

: "${REDIS_PASSWORD:?REDIS_PASSWORD gerekli}"
: "${SENTINEL_PASSWORD:?SENTINEL_PASSWORD gerekli}"
: "${ANNOUNCE_HOST:?ANNOUNCE_HOST gerekli}"
MASTER_HOST="${MASTER_HOST:-redis-master}"
QUORUM="${QUORUM:-2}"

CONF=/data/sentinel.conf
umask 077
if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<EOF
port 26379
dir /data

# Hostname desteği (Redis >= 6.2): IP değişse de topoloji bozulmaz.
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip ${ANNOUNCE_HOST}

# Sentinel'in kendisine erişim parolası (istemciler + diğer sentinel'ler)
requirepass ${SENTINEL_PASSWORD}
sentinel sentinel-pass ${SENTINEL_PASSWORD}

# quorum=2: 3 sentinel'den en az 2'si master'ı "down" görmeli (tek sentinel'in ağ
# sorunu failover tetiklemez). Sentinel sayısı TEK ve ayrı node/AZ'lerde olmalı.
sentinel monitor mymaster ${MASTER_HOST} 6379 ${QUORUM}
sentinel auth-pass mymaster ${REDIS_PASSWORD}
sentinel down-after-milliseconds mymaster ${DOWN_AFTER_MS:-3000}
sentinel failover-timeout mymaster ${FAILOVER_TIMEOUT_MS:-10000}
sentinel parallel-syncs mymaster 1
EOF
fi

exec redis-sentinel "$CONF"
