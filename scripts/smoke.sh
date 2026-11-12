#!/usr/bin/env bash
# Uçtan uca smoke test: tüm best-practice senaryolarını nginx üzerinden doğrular.
# Kullanım: ./scripts/smoke.sh   (BASE_URL ve PROM_URL .env'deki portlardan türetilir)
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[ -f .env ] && set -a && . ./.env && set +a
BASE="${BASE_URL:-http://localhost:${FRONTEND_PORT:-8080}}"
PROM="${PROM_URL:-http://localhost:${PROMETHEUS_PORT:-9090}}"
JSON='Content-Type: application/json'
FAIL=0

py() { python3 -c "import sys,json; d=json.load(sys.stdin); $1"; }
check() { # check "<açıklama>" <beklenen> <gerçek>
  if [ "$2" == "$3" ]; then echo "  ✔ $1 ($3)"; else echo "  ✘ $1: beklenen=$2 gerçek=$3"; FAIL=1; fi
}
fanout() { curl -fsS -X POST "$BASE/api/lab/fanout" -H "$JSON" -d "$1"; }

echo "== Sağlık"
check "API erişilebilir" 200 "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/system/info")"

echo "== Cache-aside + stampede"
curl -fsS -X POST "$BASE/api/lab/cache/SKU-1/flush" >/dev/null
fanout '{"path":"/api/products/SKU-1?protect=true","count":40}' >/dev/null
check "korumalı: Mongo okuma sayısı" 1 "$(curl -fsS "$BASE/api/lab/cache/SKU-1/db-loads" | py 'print(d["db_loads"])')"

echo "== Flash-sale (stok 10, 100 alıcı)"
curl -fsS -X POST "$BASE/api/lab/flash-sale/SKU-SMOKE/reset" -H "$JSON" -d '{"stock":10}' >/dev/null
sold=$(fanout '{"method":"POST","path":"/api/flash-sale/SKU-SMOKE/reserve","count":100,"body":{"user_id":"u-{i}","quantity":1}}' | py 'print(d["summary"]["status"].get("201",0))')
check "atomik: satılan" 10 "$sold"

echo "== Idempotency"
curl -fsS -X POST "$BASE/api/lab/flash-sale/SKU-IDEM/reset" -H "$JSON" -d '{"stock":5}' >/dev/null
key="smoke-$(date +%s)-$RANDOM"
fanout "{\"method\":\"POST\",\"path\":\"/api/flash-sale/SKU-IDEM/reserve\",\"count\":10,\"headers\":{\"Idempotency-Key\":\"$key\"},\"body\":{\"user_id\":\"idem\",\"quantity\":1}}" >/dev/null
check "aynı anahtarla 10 istek → stok" 4 "$(curl -fsS "$BASE/api/flash-sale/SKU-IDEM" | py 'print(d["stock"])')"

echo "== Rate limit (10 / 10 sn)"
allowed=$(fanout "{\"path\":\"/api/lab/rate-limited\",\"count\":25,\"headers\":{\"X-Client-Id\":\"smoke-$key\"}}" | py 'print(d["summary"]["status"].get("200",0))')
check "izin verilen" 10 "$allowed"

echo "== Dağıtık kilit"
started=$(fanout '{"method":"POST","path":"/api/lab/jobs/warm-cache","count":8}' | py 'print(d["summary"]["status"].get("202",0))')
check "aynı anda çalışan iş" 1 "$started"

echo "== Asenkron pipeline"
before=$(curl -fsS "$BASE/api/system/pipeline" | py 'print(d["persisted_total"])')
curl -fsS -X POST "$BASE/api/lab/events/burst" -H "$JSON" -d '{"count":500}' >/dev/null
sleep 4
after=$(curl -fsS "$BASE/api/system/pipeline" | py 'print(d["persisted_total"])')
check "500 olay Mongo'ya yazıldı" true "$( [ $((after - before)) -ge 500 ] && echo true || echo false)"

echo "== Gözlemlenebilirlik"
down=$(curl -fsS "$PROM/api/v1/targets" | py 'print(sum(1 for t in d["data"]["activeTargets"] if t["health"]=="down"))')
check "Prometheus: down hedef sayısı" 0 "$down"
ex=$(curl -fsS "$PROM/api/v1/query_exemplars" --data-urlencode 'query=http_request_duration_seconds_bucket' \
  --data-urlencode "start=$(( $(date +%s) - 900 ))" --data-urlencode "end=$(date +%s)" | py 'print(len(d["data"])>0)')
check "exemplar (metrik → trace) var" True "$ex"

JAEGER="${JAEGER_URL:-http://localhost:${JAEGER_PORT:-16686}}"
tid=$(curl -fsS -D - -o /dev/null "$BASE/api/products/SKU-2" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-trace-id"{print $2}')
found=false
for _ in 1 2 3 4 5 6 7 8 9 10; do  # Alloy batch (2 sn) + Jaeger yazımı
  if curl -fsS "$JAEGER/api/traces/$tid" 2>/dev/null | py 'print(len(d["data"][0]["spans"])>0)' 2>/dev/null | grep -q True; then
    found=true; break
  fi
  sleep 1
done
check "trace Jaeger'da da var (Alloy fan-out)" true "$found"

echo
if [ "$FAIL" -eq 0 ]; then echo "SMOKE TEST: BAŞARILI"; else echo "SMOKE TEST: HATA"; exit 1; fi
