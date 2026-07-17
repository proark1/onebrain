#!/usr/bin/env bash
# Exchanges the first-boot/rotation credential for the raw Compose bundle.
# Any failed or held exchange preserves .env and the applied secrets epoch.
set -euo pipefail

BOX_ENV="${BOX_ENV:-/opt/onebrain/box.env}"
DOTENV_LOADER="$(dirname "$0")/onebrain_dotenv.sh"
if [ ! -r "$DOTENV_LOADER" ]; then
  printf '%s\n' 'OneBrain bootstrap: dotenv loader unavailable; holding' >&2
  exit 0
fi
# shellcheck disable=SC1090
. "$DOTENV_LOADER"
# shellcheck disable=SC1090
if [ -f "$BOX_ENV" ]; then
  # First boot still has unresolved ${VAR} placeholders until the exchange.
  set +u
  set -a
  . "$BOX_ENV"
  set +a
  set -u
fi

# Command indirections support the dry-run harness and box.env wrappers.
: "${CURL:=curl}"
: "${PYTHON:=python3}"
: "${DOCKER:=docker}"
: "${UPDATE_DATA_DIR:=/data}"
: "${UPDATE_COMPOSE_DIR:=/opt/onebrain}"
: "${UPDATE_COMPOSE_PROJECT:=onebrain}"
: "${UPDATE_PROFILES:=onebrain}"

ENV_FILE="${ENV_FILE:-${UPDATE_COMPOSE_DIR}/.env}"
COMPOSE="${UPDATE_COMPOSE_DIR}/docker-compose.yml"
WORK="${UPDATE_DATA_DIR}/onebrain_update"
EPOCH_FILE="${WORK}/secrets_epoch"
LOG="${WORK}/bootstrap.log"
RESP="${WORK}/bootstrap_resp.json"
NEW_ENV="${WORK}/env.new"

# Keep response and pending-bundle files owner-only from creation onward.
umask 077
mkdir -p "$WORK"
# Tighten older work directories and replace stale response scratch files.
chmod 0700 "$WORK"
rm -f "$RESP" "$NEW_ENV"
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG" 2>/dev/null || true; }

# First boot uses the single-use token; rotation uses the delivered fleet key.
if [ -f "$ENV_FILE" ]; then
  FIRST_BOOT=0
  if ! onebrain_load_dotenv "$ENV_FILE"; then
    log "existing dotenv invalid; holding"
    exit 0
  fi
  AUTH="${ONEBRAIN_FLEET_KEY:-}"
else
  FIRST_BOOT=1
  AUTH="${ONEBRAIN_BOOTSTRAP_TOKEN:-}"
fi

log "bootstrap exchange (first_boot=$FIRST_BOOT)"
if ! "$CURL" -sf -X POST \
     -H "Authorization: Bearer ${AUTH}" \
     -H "X-OneBrain-Deployment-Id: ${ONEBRAIN_DEPLOYMENT_ID:-}" \
     "${ONEBRAIN_FLEET_URL:-}/api/fleet/bootstrap" >"$RESP" 2>>"$LOG"; then
  # Non-2xx/unreachable: hold without advancing the epoch.
  log "bootstrap exchange unreachable/rejected; holding"
  exit 0
fi

# Parse a strictly newer bundle to a temporary file (exit 3 means no change).
CURRENT_EPOCH="$(cat "$EPOCH_FILE" 2>/dev/null || echo -1)"
if SERVED_EPOCH="$("$PYTHON" - "$RESP" "$NEW_ENV" "$CURRENT_EPOCH" "$FIRST_BOOT" 2>>"$LOG" <<'PYEOF'
import sys, json
resp_path, out_path, current, first_boot = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
try:
    current = int(current)
except ValueError:
    current = -1
data = json.load(open(resp_path))
epoch = int(data.get("secrets_epoch", 0))
dotenv = data.get("dotenv", "")
if not isinstance(dotenv, str) or not dotenv:
    raise SystemExit("empty dotenv")
if not first_boot and epoch <= current:
    raise SystemExit(3)   # already at (or ahead of) the served epoch -> nothing to do
with open(out_path, "w") as fh:
    fh.write(dotenv)
print(epoch)
PYEOF
)"; then
  :   # success: SERVED_EPOCH holds the served epoch; $NEW_ENV holds the new dotenv
else
  rc=$?
  [ "$rc" = "3" ] && { log "no newer secrets epoch (have $CURRENT_EPOCH); nothing to do"; exit 0; }
  log "bootstrap response invalid; holding"
  exit 0
fi

# Install the new .env at 0600, THEN record the applied epoch — the epoch is written
# A malformed pending bundle keeps the prior file and epoch intact.
if ! (onebrain_load_dotenv "$NEW_ENV"); then
  rm -f "$NEW_ENV"
  log "bootstrap dotenv invalid; holding"
  exit 0
fi

chmod 0600 "$NEW_ENV"
mv -f "$NEW_ENV" "$ENV_FILE"
chmod 0600 "$ENV_FILE"
printf '%s\n' "$SERVED_EPOCH" >"$EPOCH_FILE"
chmod 0600 "$EPOCH_FILE"
log "wrote $ENV_FILE at secrets_epoch $SERVED_EPOCH"

# On rotation, re-apply so running containers receive the new bundle.
if [ "$FIRST_BOOT" = "0" ]; then
  PROFILE_ARGS=()
  for _p in $UPDATE_PROFILES; do PROFILE_ARGS+=(--profile "$_p"); done
  "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" "${PROFILE_ARGS[@]}" up -d >>"$LOG" 2>&1 \
    || log "compose up after rotation warned"
fi
exit 0
