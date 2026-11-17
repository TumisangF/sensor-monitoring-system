#!/usr/bin/env bash
# Quick Milestone 3 verification: existing counts plus 20-row fixture twice.
# Does not load the full dataset.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "Missing .env. Run: cp .env.example .env"; exit 1; }
set -a; source .env; set +a
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
DB="${MONGO_DB:-sensor_db}"; APP_USER="${APP_DB_USER:-sensor_app}"
BATCH_SIZE="${BATCH_SIZE:-500}"; API="http://localhost:${API_PORT:-8000}"

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; exit 1; }
cid()  { $DC ps -a -q "$1"; }
app_eval() {
  $DC exec -T mongodb mongosh -u "$APP_USER" -p "$APP_DB_PASSWORD" \
    --authenticationDatabase "$DB" --quiet "$DB" --eval "$1" | tr -d '[:space:]'
}
wait_for() { local what="$1"; shift; for _ in $(seq 1 60); do "$@" >/dev/null 2>&1 && return 0; sleep 3; done; fail "Timed out waiting for $what"; }
db_init_done() { [ "$(docker inspect --format '{{.State.Status}}' "$(cid db-init)")" = "exited" ]; }
is_healthy() { [ "$(docker inspect --format '{{.State.Health.Status}}' "$(cid "$1")")" = "healthy" ]; }
field() { echo "$1" | grep "FILE DONE" | tail -n1 | sed -n "s/.*$2=\([^ ]*\).*/\1/p"; }

CLEAN_JS='db.sensor_readings.deleteMany({sensor_id: /^fixture-/}); db.aggregated_metrics.deleteMany({sensor_id: /^fixture-/}); db.sensor_health.deleteMany({sensor_id: /^fixture-/}); db.ingestion_batches.deleteMany({file: "bad_rows.csv"}); db.rejection_events.deleteMany({file: "bad_rows.csv"}); print("cleaned")'
echo "== Milestone 3 checks =="
echo "== Milestone 3: quick remaining checks (no full CSV reload) =="
# The full dataset and duplicate-only reload have already succeeded in prior runs.
# Validate the latest recorded run and current database state without repeating ingestion.
counts=$(app_eval 'const a=db.sensor_readings.aggregate([{$group:{_id:"$metric_type",n:{$sum:1}}}]).toArray(); print(a.length+","+a.map(x=>x.n).join(","))')
IFS=, read -r metric_count c1 c2 c3 c4 c5 c6 c7 <<< "$counts"
[ "$metric_count" = 7 ] && [ "$c1" = "$c2" ] && [ "$c2" = "$c3" ] && [ "$c3" = "$c4" ] && [ "$c4" = "$c5" ] && [ "$c5" = "$c6" ] && [ "$c6" = "$c7" ] && [ "$c1" = 405171 ] || fail "Metric counts differ: $counts"
pass "Seven metrics each have 405171 readings"
latest=$(app_eval 'const b=db.ingestion_batches.find({file:"iot_telemetry_data.csv"}).sort({started_at:-1}).limit(1).toArray()[0]; print(b ? b.run_id : "NONE")')
[ "$latest" != NONE ] || fail "No ingestion batch found"
pass "Ingestion batch history exists"
rejections_before=$(app_eval 'const a=db.sensor_health.aggregate([{$group:{_id:null,n:{$sum:{$ifNull:["$rejected_readings",0]}}}}]).toArray(); print(a.length ? a[0].n : 0)')
echo "Current rejected reading count: $rejections_before"
app_eval "$CLEAN_JS" >/dev/null
echo "Loading the bad-rows fixture..."
log3=$(mktemp)
MSYS_NO_PATHCONV=1 $DC run --rm -T -v "$(pwd -W)/tests/fixtures:/fixtures:ro" -e DATA_DIR=/fixtures ingestion > "$log3" 2>&1 || { tail -n 40 "$log3"; fail "Fixture ingestion failed; log: $log3"; }
grep -E "batch|rejected|FILE DONE" "$log3" || true
[ "$(app_eval 'const b = db.ingestion_batches.find({file: "bad_rows.csv"}).sort({started_at: -1}).limit(1).toArray()[0]; print(b.outcome + "," + b.input_rows + "," + b.accepted_rows + "," + b.rejected_rows)')" = "completed_with_errors,20,16,4" ] \
  && pass "The bad-rows file reports completed with errors (input 20, accepted 16, rejected 4)" || fail "Batch outcome for the fixture is wrong"
grep -q "out_of_range:smoke" "$log3" && grep -q "missing_device" "$log3" \
  && pass "The log identifies the rejected rows and reasons" || fail "Rejected rows are not listed in the log"
[ "$(app_eval 'const h = db.sensor_health.findOne({sensor_id: "fixture-sensor-1"}); print(h.rejected_readings + "," + h.last_rejected_reason)')" = "2,not_a_number:humidity" ] \
  && pass "Rejections are recorded per sensor for the Degraded status" || fail "sensor_health rejection data is wrong"
[ "$(app_eval 'print(db.rejection_events.countDocuments({file:"bad_rows.csv"}))')" = 3 ] \
  && pass "Rejected rows with device IDs have unique event records" || fail "Unexpected rejection event count"

echo "Reloading only the 20-row fixture to test idempotence..."
log4=$(mktemp)
MSYS_NO_PATHCONV=1 $DC run --rm -T -v "$(pwd -W)/tests/fixtures:/fixtures:ro" -e DATA_DIR=/fixtures ingestion > "$log4" 2>&1 || { tail -n 40 "$log4"; fail "Fixture reload failed; log: $log4"; }
grep 'FILE DONE' "$log4" || true
[ "$(app_eval 'print(db.rejection_events.countDocuments({file:"bad_rows.csv"}))')" = 3 ] \
  && pass "Rejection events remain unique after reload" || fail "Rejection events duplicated on reload"
[ "$(app_eval 'const h=db.sensor_health.findOne({sensor_id:"fixture-sensor-1"}); print(h.rejected_readings)')" = 2 ] \
  && pass "Sensor-health rejection count remains 2 after reload" || fail "Sensor-health rejection count increased on reload"
[ "$(app_eval 'print(db.sensor_readings.countDocuments({sensor_id:/^fixture-/}))')" = 112 ] \
  && pass "Fixture readings remain 112 after reload" || fail "Fixture readings changed on reload"

app_eval "$CLEAN_JS" >/dev/null
pass "Fixture data removed"

curl -sf "$API/api/query?metric=temperature" | grep -q '"value"' && pass "The API still serves the data" || fail "API query failed"
echo "== Remaining Milestone 3 checks passed =="
