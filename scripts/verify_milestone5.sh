#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${API_PORT:-8000}"
if [ -f .env ]; then set -a; source .env; set +a; PORT="${API_PORT:-8000}"; fi
BASE="http://localhost:$PORT"
python - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
def check(path, keys):
    with urllib.request.urlopen(base + path, timeout=45) as r:
        assert r.status == 200, (path, r.status)
        obj = json.load(r)
    assert all(k in obj for k in keys), (path, obj)
    print('PASS', path)
    return obj
check('/api/health', ['status'])
check('/api/overview', ['sensor_count', 'active_alerts', 'historical'])
check('/api/sensors', ['count', 'sensors'])
check('/api/sensor-status', ['reference_time', 'counts', 'coverage_warning'])
check('/api/alerts?limit=5', ['alerts'])
check('/api/alerts/summary', ['total', 'active', 'resolved'])
check('/api/trends?metric=temperature&limit=5', ['points', 'resolution'])
print('Milestone 5 API smoke checks passed')
PY
