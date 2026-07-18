#!/usr/bin/env bash
# Root-only companion for customer-shaped boxes. It keeps the existing signed
# desired-state updater and adds a blind metadata heartbeat after every tick.
# No Compose service sources this file or its root-owned credential environment.
set -euo pipefail

ROOT=$(dirname "$0")
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
BOX_ENV="${BOX_ENV:-$ROOT/box.env}"
GATE_REPORTER="${GATE_REPORTER:-$ROOT/onebrain_gate_report.py}"
UPDATE_SCRIPT="${UPDATE_SCRIPT:-$ROOT/update.sh}"
BOOTSTRAP_SCRIPT="${ONEBRAIN_BOOTSTRAP_SCRIPT:-$ROOT/onebrain_bootstrap.sh}"
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
# Refresh the re-readable bundle before evaluating any desired state. A refresh
# failure never starts a candidate; update.sh receives the failure marker solely
# to verify/report the offered attempt as a failed prerequisite.
customer_bundle_refresh_required=false
if [ "${ONEBRAIN_GATE_AGENT_ENABLED:-false}" = "true" ] \
   || [ -n "${ONEBRAIN_BOOTSTRAP_TOKEN:-}" ]; then
  customer_bundle_refresh_required=true
fi
if [ "$customer_bundle_refresh_required" = "true" ] && [ ! -x "$BOOTSTRAP_SCRIPT" ]; then
  UPDATE_BUNDLE_REFRESH_FAILED=true "$UPDATE_SCRIPT" || update_status=$?
elif [ "$customer_bundle_refresh_required" = "true" ] && ! "$BOOTSTRAP_SCRIPT" --refresh; then
  UPDATE_BUNDLE_REFRESH_FAILED=true "$UPDATE_SCRIPT" || update_status=$?
else
  "$UPDATE_SCRIPT" || update_status=$?
fi

if [ "${ONEBRAIN_GATE_AGENT_ENABLED:-false}" = "true" ]; then
  "$GATE_REPORTER" || true
  USER_AGENT="${ONEBRAIN_USER_MANAGEMENT_AGENT:-$ROOT/onebrain_user_management_agent.py}"
  if [ ! -x "$USER_AGENT" ]; then
    "${DOCKER:-docker}" compose --project-name "${UPDATE_COMPOSE_PROJECT:-onebrain}" \
      -f "${UPDATE_COMPOSE_DIR:-$ROOT}/docker-compose.yml" cp \
      onebrain-api:/app/deploy/box/onebrain_user_management_agent.py "$USER_AGENT" >/dev/null 2>&1 || true
    chmod 0700 "$USER_AGENT" 2>/dev/null || true
  fi
  [ ! -x "$USER_AGENT" ] || "$USER_AGENT" || true
fi

exit "$update_status"
