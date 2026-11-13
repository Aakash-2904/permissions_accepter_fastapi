#!/usr/bin/env bash
# Manual end-to-end test: pipe a sample PermissionRequest payload into the hook.
# Requires RELAY_URL and RELAY_API_KEY in the environment (or in .env at the repo root).
# Tap Approve/Deny on your phone; the decision JSON is printed to stdout.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$ROOT/.env" ]]; then
  # Load .env without overriding variables already set in the shell.
  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    [[ -z "${!key:-}" ]] && export "$key=$value"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ROOT/.env" | tr -d '\r')
fi

: "${RELAY_URL:?set RELAY_URL}"
: "${RELAY_API_KEY:?set RELAY_API_KEY}"
export RELAY_URL RELAY_API_KEY WAIT_SECONDS="${WAIT_SECONDS:-120}" PHONE_APPROVE_DEBUG="${PHONE_APPROVE_DEBUG:-1}"

if [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  PY="$ROOT/.venv/Scripts/python.exe"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python3"
fi

PAYLOAD=$(cat <<'JSON'
{
  "session_id": "simulated-session",
  "transcript_path": "/tmp/simulated.jsonl",
  "cwd": "/home/me/project",
  "permission_mode": "default",
  "hook_event_name": "PermissionRequest",
  "tool_name": "Bash",
  "tool_input": {
    "command": "API_TOKEN=should-be-redacted npm run deploy",
    "description": "Simulated deploy command"
  }
}
JSON
)

echo ">> sending simulated request to $RELAY_URL (waiting up to ${WAIT_SECONDS}s for your tap)" >&2
set +e
OUTPUT=$(printf '%s' "$PAYLOAD" | "$PY" "$ROOT/hook/phone_approve.py")
STATUS=$?
set -e

echo ">> hook exit code: $STATUS" >&2
if [[ -z "$OUTPUT" ]]; then
  echo ">> no stdout (timeout / relay unreachable): Claude Code would show its terminal prompt" >&2
else
  echo "$OUTPUT"
fi
