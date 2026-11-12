#!/bin/sh
# EFK profili: Kibana hazır olunca log index'i için data view oluşturur (idempotent).
set -eu
KIBANA="${KIBANA_URL:-http://kibana:5601}"

echo "Kibana bekleniyor: $KIBANA"
until curl -fsS "$KIBANA/api/status" | grep -q '"level":"available"'; do
  sleep 5
done

if curl -fsS "$KIBANA/api/data_views" -H 'kbn-xsrf: true' | grep -q 'veon-sample-logs-\*'; then
  echo "Data view zaten var."
  exit 0
fi

curl -fsS -o /dev/null -X POST "$KIBANA/api/data_views/data_view" \
  -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
  -d '{"data_view":{"id":"veon-sample-logs","title":"veon-sample-logs-*","name":"VEON sample logs","timeFieldName":"@timestamp"}}'
echo "Data view oluşturuldu: veon-sample-logs-*"
