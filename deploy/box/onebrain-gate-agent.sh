#!/usr/bin/env bash
# Root-only companion for customer-shaped boxes. It keeps the existing signed
# desired-state updater and adds a blind metadata heartbeat after every tick.
# No Compose service sources this file or its root-owned credential environment.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/onebrain/.env}"
BOX_ENV="${BOX_ENV:-/opt/onebrain/box.env}"

# box.env can contain unresolved ${VAR} references before bootstrap completes;
# source it with nounset relaxed so an unready box holds harmlessly.
set +u
set -a
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
[ -f "$BOX_ENV" ] && . "$BOX_ENV"
set +a
set -u

update_status=0
/opt/onebrain/update.sh || update_status=$?

if [ "${ONEBRAIN_GATE_AGENT_ENABLED:-false}" = "true" ]; then
  /opt/onebrain/onebrain_gate_report.py || true
fi

exit "$update_status"
