#!/usr/bin/env bash
# Root-only companion for customer-shaped boxes. It keeps the existing signed
# desired-state updater and adds a blind metadata heartbeat after every tick.
# No Compose service sources this file or its root-owned credential environment.
set -euo pipefail

ROOT=$(dirname "$0")
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
BOX_ENV="${BOX_ENV:-$ROOT/box.env}"
GATE_REPORTER="${GATE_REPORTER:-$ROOT/onebrain_gate_report.py}"
DOTENV_LOADER="$ROOT/onebrain_dotenv.sh"

if [ ! -r "$DOTENV_LOADER" ]; then
  printf '%s\n' 'OneBrain gate agent: dotenv loader unavailable; holding' >&2
  exit 0
fi
# shellcheck disable=SC1090
. "$DOTENV_LOADER"
if [ -f "$ENV_FILE" ] && ! onebrain_load_dotenv "$ENV_FILE"; then
  printf '%s\n' 'OneBrain gate agent: invalid dotenv; holding' >&2
  exit 0
fi

# box.env can contain unresolved ${VAR} references before bootstrap completes;
# source it with nounset relaxed so an unready box holds harmlessly.
set +u
set -a
# shellcheck disable=SC1090
[ -f "$BOX_ENV" ] && . "$BOX_ENV"
set +a
set -u

if [ "${1:-}" = "--provision-callback" ]; then
  CALLBACK_URL="${ONEBRAIN_CALLBACK_URL:-}"
  if [ -z "$CALLBACK_URL" ]; then
    CALLBACK_URL="${ONEBRAIN_FLEET_URL:-}/api/provisioning/runs/${ONEBRAIN_RUN_ID:-}/callback"
  fi
  ONEBRAIN_CALLBACK_INSTANCE="$(cat "$ROOT/box.instance" 2>/dev/null || true)" \
  "$GATE_REPORTER" --provision-callback | curl -sf -X POST \
    -H "Authorization: Bearer ${ONEBRAIN_PROVISIONING_CALLBACK_TOKEN:-}" \
    -H "Content-Type: application/json" \
    --data-binary @- \
    "$CALLBACK_URL"
  exit $?
fi

update_status=0
/opt/onebrain/update.sh || update_status=$?

if [ "${ONEBRAIN_GATE_AGENT_ENABLED:-false}" = "true" ]; then
  "$GATE_REPORTER" || true
fi

exit "$update_status"
