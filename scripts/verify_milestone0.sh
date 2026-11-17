#!/usr/bin/env bash
# Verifies the Milestone 0 foundation: healthy MongoDB, working authentication,
# data surviving a restart (volume), and the restart policy.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "Missing .env. Run: cp .env.example .env"; exit 1; }
set -a; source .env; set +a

if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
DB="${MONGO_DB:-sensor_db}"

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; exit 1; }
cid()  { $DC ps -q mongodb; }
mongo_eval() {
  $DC exec -T mongodb mongosh -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASSWORD" \
    --authenticationDatabase admin --quiet "$DB" --eval "$1"
}
wait_healthy() {
  for _ in $(seq 1 40); do
    state=$(docker inspect --format '{{.State.Health.Status}}' "$(cid)" 2>/dev/null || echo starting)
    [ "$state" = "healthy" ] && return 0
    sleep 3
  done
  return 1
}

echo "== Milestone 0 checks =="
$DC up -d mongodb >/dev/null

wait_healthy && pass "MongoDB container reports healthy" || fail "MongoDB did not become healthy"

[ "$(mongo_eval "db.adminCommand('ping').ok" | tr -d '[:space:]')" = "1" ] \
  && pass "Authenticated connection works" || fail "Authenticated ping failed"

mongo_eval "db.getCollection('_foundation_check').insertOne({check: 'persistence'})" >/dev/null
$DC restart mongodb >/dev/null
wait_healthy || fail "MongoDB did not recover after restart"
[ "$(mongo_eval "db.getCollection('_foundation_check').countDocuments({})" | tr -d '[:space:]')" = "1" ] \
  && pass "Data survives a container restart (volume works)" || fail "Data was lost on restart"
mongo_eval "db.getCollection('_foundation_check').drop()" >/dev/null

[ "$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$(cid)")" = "unless-stopped" ] \
  && pass "Restart policy is unless-stopped" || fail "Restart policy is not unless-stopped"

echo "== All Milestone 0 checks passed =="
