#!/usr/bin/env bash
# Render Cron Job entry point. This invokes only the invitation-generation
# endpoint; it has no database, research, or delivery credentials.
set -euo pipefail

: "${OUTREACH_WORKER_URL:?OUTREACH_WORKER_URL is required}"
: "${OUTREACH_WORKER_TOKEN:?OUTREACH_WORKER_TOKEN is required}"

case "$OUTREACH_WORKER_URL" in
  https://geo-research-outreach-staging.onrender.com) ;;
  *) echo 'Worker URL must be the approved Outreach staging host.' >&2; exit 2 ;;
esac

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT

status_code="$(curl --silent --show-error --output "$response_file" \
  --write-out '%{http_code}' --max-time 180 --request POST \
  --header "Authorization: Bearer ${OUTREACH_WORKER_TOKEN}" \
  --header 'Content-Type: application/json' \
  --data '{}' \
  "${OUTREACH_WORKER_URL}/api/internal/invitation-generation-tick")"

if [[ "$status_code" != '200' ]]; then
  echo "Invitation-generation tick returned HTTP ${status_code}." >&2
  exit 1
fi

python3 - "$response_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as response:
    result = json.load(response)

status = str(result.get("status", "unknown"))
processed_chunks = int(result.get("processed_chunks", 0))
print(f"status={status}")
print(f"processed_chunks={processed_chunks}")
if status == "ATTENTION_REQUIRED":
    raise SystemExit("Invitation generation requires recovery; inspect the Admin Invitations page.")
PY
