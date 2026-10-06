#!/usr/bin/env bash
# Post-deploy smoke (INFRA-120), same interface as TBD's scripts/smoke-test.sh. Run by aws-infra's
# post-deploy-smoke.sh against the live URL at the deployed tag. Read-only: two GETs, no writes.
#
#   1. GET /api/healthz             liveness, no dependencies
#   2. GET /api/health/dependencies 200 only when the database answers SELECT 1
#
# Env: SMOKE_BASE_URL (required, e.g. https://dev.ziftbook.com). SMOKE_USERNAME / SMOKE_PASSWORD are
# accepted for interface parity and unused: there is no login check yet.
# Exit: 0 all passed, 1 a check failed, 2 SMOKE_BASE_URL missing.
set -uo pipefail

BASE_URL="${SMOKE_BASE_URL:-}"
[[ -n "$BASE_URL" ]] || { echo "SMOKE_BASE_URL is not set"; exit 2; }
BASE_URL="${BASE_URL%/}"
failed=0

check() { # <path>: expects 200
  local body status
  body="$(mktemp)"
  status="$(curl --silent --show-error --max-time 15 --connect-timeout 5 -o "$body" -w '%{http_code}' \
    "$BASE_URL$1" 2>/dev/null)" || status=000
  if [[ "$status" == 200 ]]; then echo "ok   GET $1 (200)"
  else echo "FAIL GET $1: expected 200, got $status; body: $(head -c 200 "$body" | tr -d '\n')"; failed=1; fi
  rm -f "$body"
}

echo "Smoke testing $BASE_URL"
check /api/healthz
check /api/health/dependencies
(( failed == 0 )) && { echo "All smoke checks passed."; exit 0; }
echo "Smoke checks FAILED."
exit 1
