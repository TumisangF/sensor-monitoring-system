#!/usr/bin/env bash
set -euo pipefail

printf 'Checking running API...\n'
curl --fail --silent --show-error 'http://localhost:8000/api/health'
echo
printf '\nRunning isolated API fault tests...\n'
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T \
  -v "$(pwd -W)/tests:/tests:ro" \
  -e PYTHONPATH=/app --entrypoint python api /tests/test_api_faults.py
printf '\nRunning isolated ingestion fault tests...\n'
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T \
  -v "$(pwd -W)/tests:/tests:ro" \
  -e PYTHONPATH=/app --entrypoint python ingestion /tests/test_ingestion_faults.py
printf '\nMilestone 7 isolated checks passed.\n'
