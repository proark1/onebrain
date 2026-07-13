#!/usr/bin/env bash
# OneBrain box secret-bootstrap agent (architecture §5, P5-03). Exchanges the box's
# single-use FIRST-BOOT token (or, on a rotation tick, its deployment FLEET KEY) for
# this box's secret bundle over TLS and writes /opt/onebrain/.env (0600). Run in the
# first-boot cloud-init runcmd BEFORE `compose up`, and on every update-timer tick for
# a wrapper-key/secret rotation re-fetch.
#
# The box holds ONLY the bootstrap token at first boot; the fleet key itself arrives
# INSIDE the bundle. Testable WITHOUT network via the update.sh dry-run harness
# (CURL/PYTHON/DOCKER overridable). NON-DESTRUCTIVE on failure: a lost / failed / held
# exchange NEVER advances the recorded secrets_epoch, so the reporter's G1-3
# convergence signal stays truthful. LF-only (A1, pinned by .gitattributes).
set -euo pipefail

BOX_ENV="${BOX_ENV:-/opt/onebrain/box.env}"
# shellcheck disable=SC1090
if [ -f "$BOX_ENV" ]; then
  # First boot still has unresolved ${VAR} placeholders until the exchange.
  set +u
  set -a
  . "$BOX_ENV"
  set +a
  set -u
fi

# Command indirections: bare defaults for the dry-run harness; a real box may point
# these at wrappers via box.env (same discipline as update.sh).
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

mkdir -p "$WORK"
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG" 2>/dev/null || true; }

# First boot (no .env yet) authenticates with the single-use bootstrap token; a
# rotation re-fetch (.env present) authenticates with the deployment fleet key that
# the FIRST exchange delivered inside the bundle.
if [ -f "$ENV_FILE" ]; then
  FIRST_BOOT=0
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
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
  # Unreachable / non-2xx -> HOLD. No destructive action; on first boot the smoke
  # callback then fails so the operator sees it. The epoch is NOT advanced.
  log "bootstrap exchange unreachable/rejected; holding"
  exit 0
fi

# Parse {secrets_epoch, dotenv}: write the new .env to a temp and echo the served epoch,
# but ONLY on first boot OR when the served epoch exceeds the last applied one.
# SystemExit(3) => nothing newer to apply; any other non-zero => malformed response.
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
# ONLY after a successful .env write, so a lost/failed exchange never advances the
# reporter's convergence signal (G1-2 / G1-3).
umask 077
mv -f "$NEW_ENV" "$ENV_FILE"
chmod 0600 "$ENV_FILE"
printf '%s\n' "$SERVED_EPOCH" >"$EPOCH_FILE"
log "wrote $ENV_FILE at secrets_epoch $SERVED_EPOCH"

# Rotation re-fetch (not first boot): re-apply so running containers pick up the new
# secrets. First boot leaves `compose up` to the cloud-init runcmd that follows.
if [ "$FIRST_BOOT" = "0" ]; then
  PROFILE_ARGS=()
  for _p in $UPDATE_PROFILES; do PROFILE_ARGS+=(--profile "$_p"); done
  "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" "${PROFILE_ARGS[@]}" up -d >>"$LOG" 2>&1 \
    || log "compose up after rotation warned"
fi
exit 0
