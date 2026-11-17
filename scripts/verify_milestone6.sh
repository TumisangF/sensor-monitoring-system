#!/usr/bin/env bash
set -euo pipefail
printf 'Checking API endpoints...\n'
for path in '/api/health' '/api/overview' '/api/sensor-status' '/api/alerts?resolved=false&limit=2' '/api/trends?metric=temperature&limit=2' '/api/trends?metric=humidity&limit=2' '/api/trends?metric=smoke&limit=2' '/api/trends?metric=co&limit=2' '/api/trends?metric=lpg&limit=2'; do
  curl --fail --silent --show-error --max-time 30 "http://localhost:8000${path}" >/dev/null
  printf 'PASS %s\n' "$path"
done
printf '\nChecking Grafana HTTP...\n'
curl --fail --silent --show-error --max-time 30 'http://localhost:3000/api/health'
printf '\nOpen http://localhost:3000 and select City Environmental Sensor Monitoring.\n'
printf 'Check that each table and trend chart renders; HTTP tests do not prove panel query success.\n'
