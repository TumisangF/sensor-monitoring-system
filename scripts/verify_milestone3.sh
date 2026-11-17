#!/usr/bin/env bash
# Verifies the Milestone 3 ingestion pipeline on the full dataset and on a small file with bad rows.
# Needs the Kaggle CSV in ./data. The full load can take several minutes.
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
csv=$(ls data/*.csv 2>/dev/null | head -n1 || true)
[ -n "$csv" ] || fail "No CSV found in ./data. Copy the Kaggle CSV there first."
name=$(basename "$csv")
rows=$(awk 'END{print NR-1}' "$csv")
echo "Dataset: $name ($rows data rows)"

$DC up -d --build mongodb db-init api >/dev/null
wait_for "MongoDB to be healthy" is_healthy mongodb
wait_for "db-init to finish" db_init_done
app_eval "$CLEAN_JS" >/dev/null

echo "Loading the full dataset (this can take several minutes)..."
log1=$(mktemp)
$DC run --rm -T ingestion > "$log1" 2>&1 || { tail -n 40 "$log1"; fail "Full ingestion failed; log: $log1"; }
grep -E "FILE DONE|INTERRUPTED|Cannot" "$log1" || true
out1=$(cat "$log1")
[ -n "$(field "$out1" rows)" ] || fail "Ingestion did not finish. See $log1"
[ "$(field "$out1" rows)" = "$rows" ] && pass "Ingestion read all $rows rows" || fail "Row count mismatch: CSV $rows, ingested $(field "$out1" rows)"
accepted=$(field "$out1" accepted); rejected=$(field "$out1" rejected)
echo "      accepted=$accepted rejected=$rejected outcome=$(field "$out1" outcome)"

stored=$(app_eval 'print(db.sensor_readings.countDocuments({sensor_id: {$not: /^fixture-/}}))')
# Earlier milestones already loaded temperature readings. The CSV also has duplicate
# sensor/timestamp pairs, so accepted rows * 7 is NOT a valid unique-document count.
# The six new metrics should each have the same number of unique readings.
new_counts=$(app_eval 'const a=db.sensor_readings.aggregate([{$match:{metric_type:{$in:["humidity","co","lpg","smoke","light","motion"]}}},{$group:{_id:"$metric_type",n:{$sum:1}}},{$sort:{_id:1}}]).toArray(); print(a.length+","+a.map(x=>x.n).join(","))')
IFS=, read -r nmetrics c1 c2 c3 c4 c5 c6 <<< "$new_counts"
[ "$nmetrics" = 6 ] && [ "$c1" = "$c2" ] && [ "$c2" = "$c3" ] && [ "$c3" = "$c4" ] && [ "$c4" = "$c5" ] && [ "$c5" = "$c6" ] \
  && pass "All six newly ingested metrics contain the same number of unique readings ($c1 each)" \
  || fail "Six-metric count mismatch: $new_counts"
[ "$c1" -gt 0 ] && [ "$c1" -le "$accepted" ] \
  && pass "Unique metric readings ($c1) do not exceed accepted CSV rows ($accepted)" \
  || fail "Unexpected unique reading count"
temp_count=$(app_eval 'print(db.sensor_readings.countDocuments({metric_type:"temperature"}))')
[ "$temp_count" -ge "$c1" ] \
  && pass "Existing temperature records preserved ($temp_count)" \
  || fail "Temperature readings missing"

[ "$(app_eval 'print(db.sensor_readings.countDocuments({metric_type: {$ne: "temperature"}, batch_id: {$exists: false}}))')" = "0" ] \
  && pass "Every new document carries a batch ID" || fail "Documents without a batch ID found"

[ "$(app_eval 'print(db.aggregated_metrics.aggregate([{$group: {_id: null, c: {$sum: "$count"}}}]).toArray()[0].c.toString())')" = "$stored" ] \
  && pass "Hourly aggregates account for every stored reading" || fail "Aggregates do not add up to the stored readings"

sensors=$(app_eval 'print(db.sensor_readings.distinct("sensor_id").length)')
[ "$(app_eval 'print(db.sensor_health.countDocuments({}))')" = "$sensors" ] \
  && pass "sensor_health has one record per sensor ($sensors)" || fail "sensor_health does not match the sensors"

run=$(app_eval "print(db.ingestion_batches.find({file: \"$name\"}).sort({started_at: -1}).limit(1).toArray()[0].run_id)")
[ "$(app_eval "print(db.ingestion_batches.aggregate([{\$match: {run_id: \"$run\"}}, {\$group: {_id: null, n: {\$sum: \"\$input_rows\"}}}]).toArray()[0].n)")" = "$rows" ] \
  && pass "Batch records account for all $rows input rows" || fail "Batch records do not add up to the CSV rows"

echo "Reloading the same file..."
log2=$(mktemp)
rejections_before=$(app_eval 'print(db.sensor_health.aggregate([{$group:{_id:null,n:{$sum:{$ifNull:["$rejected_readings",0]}}}]).toArray().map(x => x.n)[0] || 0)')
$DC run --rm -T ingestion > "$log2" 2>&1 || { tail -n 40 "$log2"; fail "Reload failed; log: $log2"; }
grep -E "FILE DONE" "$log2" || true
[ "$(field "$(cat "$log2")" inserted)" = "0" ] && pass "Reloading inserts nothing" || fail "Reload inserted documents"
[ "$(app_eval 'print(db.sensor_readings.countDocuments({sensor_id: {$not: /^fixture-/}}))')" = "$stored" ] \
  && pass "Document count unchanged after reload" || fail "Document count changed after reload"

rejections_after=$(app_eval 'print(db.sensor_health.aggregate([{$group:{_id:null,n:{$sum:{$ifNull:["$rejected_readings",0]}}}]).toArray().map(x => x.n)[0] || 0)')
[ "$rejections_before" = "$rejections_after" ] \
  && pass "Reload does not inflate sensor rejection counts" \
  || fail "Sensor rejection counts changed on reload"
echo "Loading the bad-rows fixture..."
log3=$(mktemp)
MSYS_NO_PATHCONV=1 $DC run --rm -T -v "$PWD/tests/fixtures:/fixtures:ro" -e DATA_DIR=/fixtures ingestion > "$log3" 2>&1 || { tail -n 40 "$log3"; fail "Fixture ingestion failed; log: $log3"; }
grep -E "batch|rejected|FILE DONE" "$log3" || true
[ "$(app_eval 'const b = db.ingestion_batches.find({file: "bad_rows.csv"}).sort({started_at: -1}).limit(1).toArray()[0]; print(b.outcome + "," + b.input_rows + "," + b.accepted_rows + "," + b.rejected_rows)')" = "completed_with_errors,20,16,4" ] \
  && pass "The bad-rows file reports completed with errors (input 20, accepted 16, rejected 4)" || fail "Batch outcome for the fixture is wrong"
grep -q "out_of_range:smoke" "$log3" && grep -q "missing_device" "$log3" \
  && pass "The log identifies the rejected rows and reasons" || fail "Rejected rows are not listed in the log"
[ "$(app_eval 'const h = db.sensor_health.findOne({sensor_id: "fixture-sensor-1"}); print(h.rejected_readings + "," + h.last_rejected_reason)')" = "2,not_a_number:humidity" ] \
  && pass "Rejections are recorded per sensor for the Degraded status" || fail "sensor_health rejection data is wrong"
[ "$(app_eval 'print(db.rejection_events.countDocuments({file:"bad_rows.csv"}))')" = 3 ] \
  && pass "Rejected rows with device IDs have unique event records" || fail "Unexpected rejection event count"

app_eval "$CLEAN_JS" >/dev/null
pass "Fixture data removed"

curl -sf "$API/api/query?metric=temperature" | grep -q '"value"' && pass "The API still serves the data" || fail "API query failed"
echo "== All Milestone 3 checks passed =="
