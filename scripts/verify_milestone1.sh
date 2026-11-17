#!/usr/bin/env bash
# Verifies the Milestone 1 walking skeleton end to end:
# CSV -> ingestion -> MongoDB -> API -> Grafana. Needs one CSV in ./data.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "Missing .env. Run: cp .env.example .env"; exit 1; }
set -a; source .env; set +a
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
DB="${MONGO_DB:-sensor_db}"; API="http://localhost:${API_PORT:-8000}"; GF="http://localhost:${GRAFANA_PORT:-3000}"

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; exit 1; }
cid()  { $DC ps -a -q "$1"; }
mongo_eval() {
  $DC exec -T mongodb mongosh -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASSWORD" \
    --authenticationDatabase admin --quiet "$DB" --eval "$1" | tr -d '[:space:]'
}
wait_for() {  # wait_for <description> <command...>
  local what="$1"; shift
  for _ in $(seq 1 60); do "$@" >/dev/null 2>&1 && return 0; sleep 3; done
  fail "Timed out waiting for $what"
}
is_healthy() { [ "$(docker inspect --format '{{.State.Health.Status}}' "$(cid "$1")")" = "healthy" ]; }
has_exited() { [ "$(docker inspect --format '{{.State.Status}}' "$(cid ingestion)")" = "exited" ]; }

echo "== Milestone 1 checks =="
csv=$(ls data/*.csv 2>/dev/null | head -n1 || true)
[ -n "$csv" ] || fail "No CSV found in ./data. Copy the Kaggle CSV there first."
rows=$(( $(wc -l < "$csv") - 1 ))
echo "Using $csv ($rows data rows)"

$DC up -d --build >/dev/null
wait_for "API to be healthy" is_healthy api
pass "API container is healthy"
wait_for "ingestion to finish" has_exited
[ "$(docker inspect --format '{{.State.ExitCode}}' "$(cid ingestion)")" = "0" ] \
  && pass "Ingestion finished with exit code 0" || fail "Ingestion failed. See: $DC logs ingestion"

stored=$(mongo_eval "db.sensor_readings.countDocuments({metric_type:'temperature'})")
[ "$stored" -gt 0 ] && pass "MongoDB holds $stored temperature documents (CSV rows: $rows)" || fail "No documents stored"
[ "$stored" = "$rows" ] || echo "NOTE  Stored count differs from CSV rows; rejected rows are listed in: $DC logs ingestion"

curl -sf "$API/api/health" | grep -q '"ok"' && pass "API /api/health reports ok" || fail "API health failed"
curl -sf "$API/api/query?metric=temperature" | grep -q '"value"' \
  && pass "API /api/query returns hourly temperature points" || fail "API query returned no data"

rerun=$($DC run --rm ingestion | grep "FILE DONE" | head -n1)
echo "$rerun" | grep -q "inserted=0" && pass "Reloading the same CSV inserts nothing (no duplicates)" || fail "Reload added documents: $rerun"
[ "$(mongo_eval "db.sensor_readings.countDocuments({metric_type:'temperature'})")" = "$stored" ] \
  && pass "Document count unchanged after reload" || fail "Document count changed after reload"

wait_for "Grafana to be ready" curl -sf "$GF/api/health"
auth="${GRAFANA_ADMIN_USER:-admin}:${GRAFANA_ADMIN_PASSWORD}"
curl -sf -u "$auth" "$GF/api/datasources" | grep -q "yesoreyeram-infinity-datasource" \
  && pass "Grafana has the Infinity data source" || fail "Infinity data source missing (check: $DC logs grafana)"
curl -sf -u "$auth" "$GF/api/search?query=Sensor" | grep -q "sensor-monitoring" \
  && pass "Grafana has the provisioned dashboard" || fail "Dashboard not provisioned"

body='{"queries":[{"refId":"A","datasource":{"type":"yesoreyeram-infinity-datasource","uid":"infinity"},"type":"json","source":"url","parser":"backend","format":"timeseries","url":"http://api:8000/api/query?metric=temperature&limit=2000","url_options":{"method":"GET","data":""},"root_selector":"","columns":[{"selector":"time","text":"Time","type":"timestamp"},{"selector":"value","text":"Temperature","type":"number"}]}],"from":"1594425600000","to":"1595289600000"}'
curl -sf -u "$auth" -H "Content-Type: application/json" -X POST "$GF/api/ds/query" -d "$body" | grep -q '"values":\[\[' \
  && pass "Grafana receives temperature data through the Infinity data source" || fail "Grafana query returned no data"

echo "== All Milestone 1 checks passed =="
echo "Now open $GF (login from .env) and confirm the dashboard shows a temperature line."
