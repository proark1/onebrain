#!/usr/bin/env bash
# Exchanges the first-boot/rotation credential for the raw Compose bundle.
# Any failed or held exchange preserves .env and the applied secrets epoch.
set -euo pipefail

REFRESH_MODE=0
case "${1:-}" in
  "") ;;
  --refresh) REFRESH_MODE=1 ;;
  *)
    printf '%s\n' 'usage: onebrain_bootstrap.sh [--refresh]' >&2
    exit 64
    ;;
esac

# First boot deliberately treats a rejected exchange as a harmless hold. The
# gate agent uses --refresh and needs a distinct status so it can report a
# candidate prerequisite failure without starting that candidate.
refresh_hold() {
  if [ "$REFRESH_MODE" = "1" ]; then
    exit 75
  fi
  exit 0
}

BOX_ENV="${BOX_ENV:-/opt/onebrain/box.env}"
DOTENV_LOADER="$(dirname "$0")/onebrain_dotenv.sh"
if [ ! -r "$DOTENV_LOADER" ]; then
  printf '%s\n' 'OneBrain bootstrap: dotenv loader unavailable; holding' >&2
  refresh_hold
fi
# shellcheck disable=SC1090
. "$DOTENV_LOADER"

load_box_env() {
  # First boot still has unresolved ${VAR} placeholders until the exchange.
  set +u
  set -a
  # shellcheck disable=SC1090
  [ -f "$BOX_ENV" ] && . "$BOX_ENV"
  set +a
  set -u
}

load_box_env

# Command indirections support the dry-run harness and box.env wrappers.
: "${CURL:=curl}"
: "${PYTHON:=python3}"
: "${DOCKER:=docker}"
: "${ONEBRAIN_DATA_MOUNT:=/mnt/onebrain-data}"
: "${ONEBRAIN_MAINTENANCE_DIR:=${ONEBRAIN_DATA_MOUNT}/onebrain-maintenance}"
: "${ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT:=$(dirname "$0")/onebrain-data-volume.sh}"
: "${UPDATE_COMPOSE_DIR:=/opt/onebrain}"
: "${UPDATE_COMPOSE_PROJECT:=onebrain}"
: "${UPDATE_PROFILES:=onebrain}"

ENV_FILE="${ENV_FILE:-${UPDATE_COMPOSE_DIR}/.env}"
COMPOSE="${UPDATE_COMPOSE_DIR}/docker-compose.yml"
OVERRIDE="${UPDATE_COMPOSE_DIR}/images.override.yml"
MAINTENANCE_EXPECTED_DIR="${ONEBRAIN_DATA_MOUNT%/}/onebrain-maintenance"
WORK="${ONEBRAIN_MAINTENANCE_DIR}/onebrain_update"
EPOCH_FILE="${WORK}/secrets_epoch"
LOG="${WORK}/bootstrap.log"
RESP="${WORK}/bootstrap_resp.json"
NEW_ENV="${WORK}/env.new"
# One-shot marker so a terminal first-boot failure is reported to Mission Control
# once, not on every timer tick.
REPORTED_MARKER="${WORK}/provision_failure_reported"
GATE_AGENT="${ONEBRAIN_GATE_AGENT:-$(dirname "$0")/onebrain-gate-agent.sh}"

# The exchange carries secrets, so its state belongs in the root-owned
# maintenance subtree of the UUID-verified attached volume, never `/data`.
require_maintenance_dir() {
  local mount_real maintenance_real mount_source maintenance_source maintenance_target
  if [ "$ONEBRAIN_MAINTENANCE_DIR" != "$MAINTENANCE_EXPECTED_DIR" ]; then
    printf '%s\n' 'OneBrain bootstrap: maintenance path is not the dedicated data-volume directory; holding' >&2
    return 1
  fi
  if ! command -v mountpoint >/dev/null 2>&1 || ! command -v findmnt >/dev/null 2>&1 \
     || ! command -v readlink >/dev/null 2>&1; then
    printf '%s\n' 'OneBrain bootstrap: mount verification tools unavailable; holding' >&2
    return 1
  fi
  if ! mountpoint -q "$ONEBRAIN_DATA_MOUNT"; then
    printf '%s\n' 'OneBrain bootstrap: persistent data volume is not mounted; holding' >&2
    return 1
  fi
  if [ ! -x "$ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT" ] \
     || ! ONEBRAIN_DATA_MOUNT="$ONEBRAIN_DATA_MOUNT" "$ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT" verify >/dev/null 2>&1; then
    printf '%s\n' 'OneBrain bootstrap: persistent data volume is unavailable or mismatched; holding' >&2
    return 1
  fi
  if [ -L "$ONEBRAIN_MAINTENANCE_DIR" ]; then
    printf '%s\n' 'OneBrain bootstrap: maintenance path must not be a symlink; holding' >&2
    return 1
  fi
  if [ "$EUID" -eq 0 ]; then
    install -d -o root -g root -m 0700 "$ONEBRAIN_MAINTENANCE_DIR"
  else
    # The functional harness supplies an explicit temporary mount + verifier.
    mkdir -p "$ONEBRAIN_MAINTENANCE_DIR" && chmod 0700 "$ONEBRAIN_MAINTENANCE_DIR"
  fi
  if ! mount_real="$(readlink -f -- "$ONEBRAIN_DATA_MOUNT" 2>/dev/null)" \
     || ! maintenance_real="$(readlink -f -- "$ONEBRAIN_MAINTENANCE_DIR" 2>/dev/null)"; then
    printf '%s\n' 'OneBrain bootstrap: could not canonicalize maintenance directory; holding' >&2
    return 1
  fi
  case "$maintenance_real" in
    "$mount_real"/*) ;;
    *)
      printf '%s\n' 'OneBrain bootstrap: maintenance directory escaped the data volume; holding' >&2
      return 1
      ;;
  esac
  if ! mount_source="$(findmnt -nr -o SOURCE --target "$mount_real" 2>/dev/null)" \
     || ! maintenance_source="$(findmnt -nr -o SOURCE --target "$maintenance_real" 2>/dev/null)" \
     || ! maintenance_target="$(findmnt -nr -o TARGET --target "$maintenance_real" 2>/dev/null)" \
     || [ -z "$mount_source" ] || [ "$maintenance_source" != "$mount_source" ] \
     || [ "$maintenance_target" != "$mount_real" ]; then
    printf '%s\n' 'OneBrain bootstrap: maintenance directory is not on the verified data volume; holding' >&2
    return 1
  fi
}

# Keep response and pending-bundle files owner-only from creation onward.
umask 077
if ! require_maintenance_dir; then
  refresh_hold
fi
mkdir -p "$WORK"
# Tighten older work directories and replace stale response scratch files.
chmod 0700 "$WORK"
rm -f "$RESP" "$NEW_ENV"
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG" 2>/dev/null || true; }

dc_over() {
  if [ -f "$OVERRIDE" ]; then
    "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" -f "$OVERRIDE" "$@"
  else
    "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" "$@"
  fi
}

# First boot uses the single-use token; rotation uses the delivered fleet key.
if [ -f "$ENV_FILE" ]; then
  FIRST_BOOT=0
  if ! onebrain_load_dotenv "$ENV_FILE"; then
    log "existing dotenv invalid; holding"
    refresh_hold
  fi
  AUTH="${ONEBRAIN_FLEET_KEY:-}"
else
  FIRST_BOOT=1
  AUTH="${ONEBRAIN_BOOTSTRAP_TOKEN:-}"
fi

# A first-boot token is single-use, short-lived, and cannot be reissued, so a 401
# is TERMINAL: this box will never obtain its bundle. Holding silently is what
# left a dead box reporting `dispatched` with every module `active` while it
# served 502 (2026-07-20). Report it once so the run fails with a reason. Only a
# never-provisioned box reports; a rotation failure on a working box is a
# transient the timer retries, not a provisioning outcome.
report_unrecoverable_first_boot() {
  [ "$FIRST_BOOT" = "1" ] || return 0
  [ -n "${ONEBRAIN_PROVISIONING_CALLBACK_TOKEN:-}" ] || return 0
  [ -x "$GATE_AGENT" ] || return 0
  # `[ ... ] && return 0` would abort the script under `set -e` on the common
  # path where the marker does not exist yet.
  if [ -e "$REPORTED_MARKER" ]; then return 0; fi
  # Marker first: a callback that half-succeeds must not be retried every tick.
  : >"$REPORTED_MARKER" 2>/dev/null || true
  if ONEBRAIN_CALLBACK_STATUS="failed" \
     ONEBRAIN_CALLBACK_SMOKE="failed" \
     ONEBRAIN_CALLBACK_KIND="failure" \
     "$GATE_AGENT" --provision-callback >>"$LOG" 2>&1; then
    log "reported unrecoverable bootstrap to Mission Control"
  else
    log "unrecoverable bootstrap report failed; run stays dispatched"
  fi
}

log "bootstrap exchange (first_boot=$FIRST_BOOT)"
# -sf hid the status code, so a rejected token and an unreachable control plane
# were indistinguishable in the log. Capture the code and treat them differently.
HTTP_CODE="$("$CURL" -s -o "$RESP" -w '%{http_code}' -X POST \
     -H "Authorization: Bearer ${AUTH}" \
     -H "X-OneBrain-Deployment-Id: ${ONEBRAIN_DEPLOYMENT_ID:-}" \
     "${ONEBRAIN_FLEET_URL:-}/api/fleet/bootstrap" 2>>"$LOG")" || HTTP_CODE="000"
case "$HTTP_CODE" in
  2??) ;;
  401)
    log "bootstrap exchange rejected (401: invalid, expired, or consumed credential)"
    report_unrecoverable_first_boot
    refresh_hold
    ;;
  *)
    # Unreachable, rate-limited, or a control-plane error: retryable, so hold
    # without advancing the epoch and without failing the run.
    log "bootstrap exchange unreachable/rejected (http=$HTTP_CODE); holding"
    refresh_hold
    ;;
esac

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
  refresh_hold
fi

# Install the new .env at 0600, THEN record the applied epoch — the epoch is written
# A malformed pending bundle keeps the prior file and epoch intact.
if ! (onebrain_load_dotenv "$NEW_ENV"); then
  rm -f "$NEW_ENV"
  log "bootstrap dotenv invalid; holding"
  refresh_hold
fi

chmod 0600 "$NEW_ENV"
mv -f "$NEW_ENV" "$ENV_FILE"
chmod 0600 "$ENV_FILE"
log "installed pending bundle for secrets_epoch $SERVED_EPOCH"

# Compose gives shell values precedence over .env. Reload the literal bundle
# and renderer-owned references before reapplying so a rotation cannot start
# containers with the previous secret values.
if ! onebrain_load_dotenv "$ENV_FILE"; then
  log "installed dotenv could not be reloaded; holding"
  refresh_hold
fi
load_box_env

# On rotation, re-apply so running containers receive the new bundle. Keep the
# digest-pinned image override: a secret rotation must never roll images back
# to the base compose defaults.
if [ "$FIRST_BOOT" = "0" ]; then
  PROFILE_ARGS=()
  for _p in $UPDATE_PROFILES; do PROFILE_ARGS+=(--profile "$_p"); done
  if ! dc_over "${PROFILE_ARGS[@]}" up -d >>"$LOG" 2>&1; then
    # Keep the previous applied epoch so the next timer tick retries this same
    # served bundle instead of treating a failed container reapply as complete.
    log "compose up after rotation FAILED; bundle remains pending"
    refresh_hold
  fi
fi
printf '%s\n' "$SERVED_EPOCH" >"$EPOCH_FILE"
chmod 0600 "$EPOCH_FILE"
log "applied $ENV_FILE at secrets_epoch $SERVED_EPOCH"
exit 0
