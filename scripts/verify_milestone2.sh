#!/usr/bin/env bash
# Verifies the Milestone 2 data model: collections with schema validation, indexes,
# duplicate rejection, a flexible schema for new metrics, a restricted application user,
# and thresholds that change through the config file only.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "Missing .env. Run: cp .env.example .env"; exit 1; }
set -a; source .env; set +a
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
DB="${MONGO_DB:-sensor_db}"
APP_USER="${APP_DB_USER:-sensor_app}"
API="http://localhost:${API_PORT:-8000}"

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; exit 1; }
cid()  { $DC ps -a -q "$1"; }
# Run JavaScript in mongosh as the restricted application user
app_eval() {
  $DC exec -T mongodb mongosh -u "$APP_USER" -p "$APP_DB_PASSWORD" \
    --authenticationDatabase "$DB" --quiet "$DB" --eval "$1" | tr -d '[:space:]'
}
wait_for() {
  local what="$1"; shift
  for _ in $(seq 1 60); do "$@" >/dev/null 2>&1 && return 0; sleep 3; done
  fail "Timed out waiting for $what"
}
is_healthy() { [ "$(docker inspect --format '{{.State.Health.Status}}' "$(cid "$1")")" = "healthy" ]; }
db_init_done() { [ "$(docker inspect --format '{{.State.Status}}' "$(cid db-init)")" = "exited" ]; }

echo "== Milestone 2 checks =="
$DC up -d --build >/dev/null
wait_for "MongoDB to be healthy" is_healthy mongodb
wait_for "db-init to finish" db_init_done
[ "$(docker inspect --format '{{.State.ExitCode}}' "$(cid db-init)")" = "0" ] \
  && pass "db-init completed successfully" || fail "db-init failed. See: $DC logs db-init"

[ "$(app_eval 'print(db.getCollectionNames().sort().join(","))')" = "aggregated_metrics,alerts,ingestion_batches,sensor_health,sensor_readings" ] \
  && pass "All five collections exist" || fail "Collections missing or unexpected"

[ "$(app_eval 'print(db.getCollectionInfos().filter(c => c.options && c.options.validator).length)')" = "5" ] \
  && pass "All five collections have schema validation" || fail "Schema validation missing on a collection"

[ "$(app_eval 'const need = {sensor_readings:["uniq_reading"], aggregated_metrics:["uniq_hour"], sensor_health:["uniq_sensor"], ingestion_batches:["uniq_batch"], alerts:["active_recent"]}; print(Object.entries(need).every(([c, ns]) => { const have = db[c].getIndexes().map(i => i.name); return ns.every(n => have.includes(n)); }))')" = "true" ] \
  && pass "Required indexes exist" || fail "An index is missing"

[ "$(app_eval 'const t = new Date("2020-01-01T00:00:00Z"); let a = "ok", b = "ok"; try { db.sensor_readings.insertOne({sensor_id:"_m2_test", device_timestamp:t, metric_type:"temperature", value:21.5}); } catch (e) { a = e.code; } try { db.sensor_readings.insertOne({sensor_id:"_m2_test", device_timestamp:t, metric_type:"temperature", value:21.5}); } catch (e) { b = e.code; } db.sensor_readings.deleteMany({sensor_id:"_m2_test"}); print(a + "," + b)')" = "ok,11000" ] \
  && pass "A duplicate reading is rejected" || fail "Duplicate reading was not rejected"

[ "$(app_eval 'let a = "ok", b = "ok"; try { db.sensor_readings.insertOne({sensor_id:"_m2_test", device_timestamp:new Date(), value:1}); } catch (e) { a = e.code; } try { db.sensor_readings.insertOne({sensor_id:"_m2_test", device_timestamp:new Date(), metric_type:"temperature", value:"warm"}); } catch (e) { b = e.code; } db.sensor_readings.deleteMany({sensor_id:"_m2_test"}); print(a + "," + b)')" = "121,121" ] \
  && pass "Documents with a missing field or a wrong type are rejected" || fail "Invalid document was accepted"

[ "$(app_eval 'db.sensor_readings.insertOne({sensor_id:"_m2_test", device_timestamp:new Date(), metric_type:"co2", value:415, unit:"ppm", location:"depot"}); const n = db.sensor_readings.countDocuments({sensor_id:"_m2_test", metric_type:"co2"}); db.sensor_readings.deleteMany({sensor_id:"_m2_test"}); print(n)')" = "1" ] \
  && pass "A new metric (co2) with extra fields is accepted without a schema change" || fail "Flexible schema check failed"

[ "$(app_eval 'let r = "allowed"; try { r = db.getSiblingDB("admin").runCommand({usersInfo: 1}).ok === 1 ? "allowed" : "denied"; } catch (e) { r = "denied"; } print(r)')" = "denied" ] \
  && pass "The application user has no administrative rights" || fail "The application user is too privileged"

cleanup() { rm -f config/thresholds.test.json; }
trap cleanup EXIT
sed 's/"alert_max": 0.05/"alert_max": 0.07/' config/thresholds.json > config/thresholds.test.json
read_smoke() {
  MSYS_NO_PATHCONV=1 $DC run --rm --no-deps \
    -e THRESHOLDS_FILE="$1" ingestion \
    python -c "import thresholds; print(thresholds.load()['smoke']['alert_max'])" \
    | tail -n1 | tr -d '[:space:]'
}
default=$(read_smoke /config/thresholds.json)
changed=$(read_smoke /config/thresholds.test.json)
cleanup
[ "$default" = "0.05" ] && [ "$changed" = "0.07" ] \
  && pass "Thresholds come from the config file (smoke limit 0.05 -> 0.07 with no code change)" \
  || fail "Threshold check failed (default=$default, changed=$changed)"

csv=$(ls data/*.csv 2>/dev/null | head -n1 || true)
if [ -n "$csv" ]; then
  log=$(mktemp)
  $DC run --rm ingestion > "$log"
  grep -q "Thresholds loaded" "$log" && grep -q "inserted=0" "$log" \
    && pass "Ingestion works as the restricted user, loads the thresholds and inserts no duplicates" \
    || fail "Ingestion check failed. See $log"
else
  echo "SKIP  Ingestion re-run (no CSV in ./data)"
fi
curl -sf "$API/api/health" | grep -q '"ok"' && pass "API connects as the restricted user" || fail "API health failed"
curl -sf "$API/api/query?metric=temperature" | grep -q '"value"' \
  && pass "Existing temperature data is still served" || echo "NOTE  No temperature data yet; load the CSV and re-run"

echo "== All Milestone 2 checks passed =="
