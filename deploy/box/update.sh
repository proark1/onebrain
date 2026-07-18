#!/usr/bin/env bash
# shellcheck disable=SC2317
# ^ File-wide (must follow the shebang, clean directive line): helper functions
#   (dc/dc_over/recover_*/…) run via the main flow and traps; shellcheck's static
#   call graph misreads them as unreachable. The dry-run harness
#   (tests/test_box_update_sh.py) proves every one is actually invoked.
# OneBrain box update agent (architecture §3e, P1-F/P3). The box VERIFIES, never
# trusts MC (D2): it fetches the signed desired-state, verifies it with the
# app-free verifier, and derives EVERY pulled image SOLELY from the verifier's
# validated stdout target (A7) — the raw envelope/ack and the provision-time
# compose are opaque after verification. Decoupled from the app container so a
# broken app can still be recovered.
#
# Testable WITHOUT Docker/Postgres via the python dry-run harness
# (tests/test_box_update_sh.py): docker/curl/alembic/pg_dump/pg_restore/openssl are
# overridable (defaults below) and stubbed on a temp PATH. LF-only (A1, pinned by
# .gitattributes). Singleton via a mkdir lock (A2 — no flock dependency).
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/onebrain/.env}"
BOX_ENV="${BOX_ENV:-/opt/onebrain/box.env}"
DOTENV_LOADER="$(dirname "$0")/onebrain_dotenv.sh"
# Load the exchanged bundle as data before trusted box.env expands its ${VAR} refs.
if [ ! -r "$DOTENV_LOADER" ]; then
  printf '%s\n' 'OneBrain updater: dotenv loader unavailable; holding' >&2
  exit 0
fi
# shellcheck disable=SC1090
. "$DOTENV_LOADER"
if [ -f "$ENV_FILE" ]; then
  if ! onebrain_load_dotenv "$ENV_FILE"; then
    printf '%s\n' 'OneBrain updater: invalid dotenv; holding' >&2
    exit 0
  fi
fi
# shellcheck disable=SC1090
if [ -f "$BOX_ENV" ]; then
  # box.env is renderer-owned; relax nounset for unresolved first-boot refs.
  set +u
  set -a
  . "$BOX_ENV"
  set +a
  set -u
fi

# Command indirections: bare defaults for the dry-run harness; a real box may
# point these at `docker compose exec ...` wrappers via box.env.
: "${DOCKER:=docker}"
: "${CURL:=curl}"
: "${ALEMBIC:=alembic}"
: "${PG_DUMP:=pg_dump}"
: "${PG_RESTORE:=pg_restore}"
: "${DATE:=date}"
# 7c / G2-2: the custom-format dump (-Fc) and pg_restore MUST resolve to the SAME
# database, or a restore on a failed migration-crossing update could load the dump into
# a different/empty DB than was dumped. Both connect through this ONE shared target
# (overridable in box.env alongside the PG_DUMP/PG_RESTORE exec wrappers a live box may
# point these at) — the dump is never left implicit while the restore is explicit.
: "${PG_CONN:=postgresql://onebrain@postgres:5432/onebrain}"
: "${OPENSSL:=openssl}"
: "${PYTHON:=python3}"

# `/data` remains application-owned runtime storage. Root-only updater state and
# rollback archives deliberately live on the verified attached data volume.
: "${UPDATE_DATA_DIR:=/data}"
: "${ONEBRAIN_DATA_MOUNT:=/mnt/onebrain-data}"
: "${ONEBRAIN_MAINTENANCE_DIR:=${ONEBRAIN_DATA_MOUNT}/onebrain-maintenance}"
: "${ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT:=$(dirname "$0")/onebrain-data-volume.sh}"
: "${UPDATE_COMPOSE_DIR:=/opt/onebrain}"
: "${UPDATE_COMPOSE_PROJECT:=onebrain}"
: "${UPDATE_PROFILES:=onebrain}"
: "${UPDATE_LOCAL_MODULES:=}"
: "${UPDATE_HEALTH_URL:=http://127.0.0.1/health}"
: "${UPDATE_VERIFY_BIN:=${UPDATE_COMPOSE_DIR}/onebrain_box_verify.py}"
# A previous stack can need a little time to become ready after its images are
# restored. Keep this bounded and renderer-overridable for slow hosts/tests.
: "${UPDATE_RECOVERY_HEALTH_ATTEMPTS:=12}"
: "${UPDATE_RECOVERY_HEALTH_INTERVAL_SECONDS:=5}"
# A crash/reboot must not leave the periodic updater held forever. A lock is
# reclaimed only after this age AND when its recorded owner is no longer live.
: "${UPDATE_LOCK_STALE_SECONDS:=3600}"
# Keep local encrypted rollback archives conservatively by default. Zero means
# retain all archives; pruning always excludes the newest archive.
: "${UPDATE_BACKUP_RETENTION_DAYS:=30}"
# The first role-split migration is known by revision. A later release plan may
# force the same host-asset check for successors by setting this trusted box.env
# switch to true.
: "${UPDATE_ROLE_SPLIT_REQUIRED:=true}"

case "$UPDATE_RECOVERY_HEALTH_ATTEMPTS" in
  ''|*[!0-9]*) UPDATE_RECOVERY_HEALTH_ATTEMPTS=12 ;;
esac
[ "$UPDATE_RECOVERY_HEALTH_ATTEMPTS" -ge 1 ] || UPDATE_RECOVERY_HEALTH_ATTEMPTS=1
case "$UPDATE_RECOVERY_HEALTH_INTERVAL_SECONDS" in
  ''|*[!0-9]*) UPDATE_RECOVERY_HEALTH_INTERVAL_SECONDS=5 ;;
esac
case "$UPDATE_LOCK_STALE_SECONDS" in
  ''|*[!0-9]*) UPDATE_LOCK_STALE_SECONDS=3600 ;;
esac
case "$UPDATE_BACKUP_RETENTION_DAYS" in
  ''|*[!0-9]*) UPDATE_BACKUP_RETENTION_DAYS=30 ;;
esac

MAINTENANCE_EXPECTED_DIR="${ONEBRAIN_DATA_MOUNT%/}/onebrain-maintenance"
WORK="${ONEBRAIN_MAINTENANCE_DIR}/onebrain_update"
BACKUP_DIR="${ONEBRAIN_MAINTENANCE_DIR}/backups"
COMPOSE="${UPDATE_COMPOSE_DIR}/docker-compose.yml"
OVERRIDE="${UPDATE_COMPOSE_DIR}/images.override.yml"
OVERRIDE_PREV="${UPDATE_COMPOSE_DIR}/images.override.prev.yml"
OVERRIDE_NEXT="${UPDATE_COMPOSE_DIR}/images.override.next.yml"
MIGRATE_ENV="${UPDATE_COMPOSE_DIR}/env/onebrain-migrate.env"
POSTGRES_ENV="${UPDATE_COMPOSE_DIR}/env/postgres.env"
POSTGRES_INIT="${UPDATE_COMPOSE_DIR}/postgres-init.sh"
STATE_FILE="${WORK}/update_state.json"
LAST_APPLIED="${WORK}/last_applied.json"
LOG="${WORK}/update.log"
LOCK="${WORK}/update.lock"
LOCK_PID_FILE="${LOCK}/pid"
LOCK_STARTED_FILE="${LOCK}/started_at"

# The data-volume verifier proves that the expected UUID-backed mount is present
# before this root-only directory is touched. A missing or mismatched mount must
# never fall back to the root-disk mountpoint directory after a reboot.
require_maintenance_dir() {
  local mount_real maintenance_real mount_source maintenance_source maintenance_target
  if [ "$ONEBRAIN_MAINTENANCE_DIR" != "$MAINTENANCE_EXPECTED_DIR" ]; then
    printf '%s\n' 'OneBrain updater: maintenance path is not the dedicated data-volume directory; holding' >&2
    return 1
  fi
  if ! command -v mountpoint >/dev/null 2>&1 || ! command -v findmnt >/dev/null 2>&1 \
     || ! command -v readlink >/dev/null 2>&1; then
    printf '%s\n' 'OneBrain updater: mount verification tools unavailable; holding' >&2
    return 1
  fi
  if ! mountpoint -q "$ONEBRAIN_DATA_MOUNT"; then
    printf '%s\n' 'OneBrain updater: persistent data volume is not mounted; holding' >&2
    return 1
  fi
  if [ ! -x "$ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT" ] \
     || ! ONEBRAIN_DATA_MOUNT="$ONEBRAIN_DATA_MOUNT" "$ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT" verify >/dev/null 2>&1; then
    printf '%s\n' 'OneBrain updater: persistent data volume is unavailable or mismatched; holding' >&2
    return 1
  fi
  if [ -L "$ONEBRAIN_MAINTENANCE_DIR" ]; then
    printf '%s\n' 'OneBrain updater: maintenance path must not be a symlink; holding' >&2
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
    printf '%s\n' 'OneBrain updater: could not canonicalize maintenance directory; holding' >&2
    return 1
  fi
  case "$maintenance_real" in
    "$mount_real"/*) ;;
    *)
      printf '%s\n' 'OneBrain updater: maintenance directory escaped the data volume; holding' >&2
      return 1
      ;;
  esac
  if ! mount_source="$(findmnt -nr -o SOURCE --target "$mount_real" 2>/dev/null)" \
     || ! maintenance_source="$(findmnt -nr -o SOURCE --target "$maintenance_real" 2>/dev/null)" \
     || ! maintenance_target="$(findmnt -nr -o TARGET --target "$maintenance_real" 2>/dev/null)" \
     || [ -z "$mount_source" ] || [ "$maintenance_source" != "$mount_source" ] \
     || [ "$maintenance_target" != "$mount_real" ]; then
    printf '%s\n' 'OneBrain updater: maintenance directory is not on the verified data volume; holding' >&2
    return 1
  fi
}

# Desired-state envelopes and plaintext database archives live below $WORK.
# Make both new files and existing scratch private before any are created.
umask 077
if ! require_maintenance_dir; then
  exit 0
fi
mkdir -p "$WORK"

# `/data` ownership is established once by first boot. Do not recursively chown
# a host path here: Postgres data is mounted separately and must remain untouched.
if ! chmod 0700 "$WORK"; then
  printf '%s\n' 'OneBrain updater: cannot secure update scratch; holding' >&2
  exit 0
fi

# Singleton: mkdir is atomic; a second run exits cleanly (A2 — no flock).
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG" 2>/dev/null || true; }

DUMP=""
RESTORE=""
LOCK_HELD=0

remove_plaintext_archive() {
  local archive="${1:-}"
  [ -n "$archive" ] || return 0
  [ -e "$archive" ] || return 0
  shred -u "$archive" 2>/dev/null || rm -f -- "$archive"
}

cleanup_update_scratch() {
  remove_plaintext_archive "$DUMP"
  remove_plaintext_archive "$RESTORE"
  if [ "$LOCK_HELD" = "1" ]; then
    rm -f -- "$LOCK_PID_FILE" "$LOCK_STARTED_FILE"
    rmdir "$LOCK" 2>/dev/null || true
  fi
}

lock_started_epoch() {
  local lock_dir="$1" started
  started="$(tr -d '[:space:]' <"${lock_dir}/started_at" 2>/dev/null || true)"
  case "$started" in
    ''|*[!0-9]*)
      started="$(stat -c %Y "$lock_dir" 2>/dev/null || stat -f %m "$lock_dir" 2>/dev/null || true)"
      ;;
  esac
  case "$started" in
    ''|*[!0-9]*) return 1 ;;
  esac
  printf '%s\n' "$started"
}

lock_is_stale() {
  local lock_dir="$1" started now age pid
  started="$(lock_started_epoch "$lock_dir" || true)"
  now="$("$DATE" +%s 2>/dev/null || true)"
  case "$started:$now" in
    *[!0-9:]*|:*) return 1 ;;
  esac
  age=$((now - started))
  [ "$age" -ge 0 ] && [ "$age" -ge "$UPDATE_LOCK_STALE_SECONDS" ] || return 1
  pid="$(tr -d '[:space:]' <"${lock_dir}/pid" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) return 0 ;;
  esac
  [ "$pid" -gt 1 ] || return 0
  kill -0 "$pid" 2>/dev/null && return 1
  return 0
}

reclaim_stale_lock() {
  local stale_lock="${LOCK}.stale.$$"
  if ! mv "$LOCK" "$stale_lock" 2>/dev/null; then
    return 1
  fi
  # Recheck after the atomic rename. A newly-created/active lock is restored
  # instead of being removed if the first observation raced its owner.
  if ! lock_is_stale "$stale_lock"; then
    [ -e "$LOCK" ] || mv "$stale_lock" "$LOCK" 2>/dev/null || true
    return 1
  fi
  rm -f -- "${stale_lock}/pid" "${stale_lock}/started_at"
  rmdir "$stale_lock" 2>/dev/null || log "stale update lock moved aside for manual inspection"
  log "reclaimed stale update lock"
  return 0
}

acquire_update_lock() {
  while true; do
    if mkdir "$LOCK" 2>/dev/null; then
      LOCK_HELD=1
      printf '%s\n' "$$" >"$LOCK_PID_FILE"
      "$DATE" +%s >"$LOCK_STARTED_FILE"
      chmod 0600 "$LOCK_PID_FILE" "$LOCK_STARTED_FILE"
      return 0
    fi
    if ! lock_is_stale "$LOCK"; then
      log "active or recent update lock present; holding"
      return 1
    fi
    if ! reclaim_stale_lock; then
      log "stale update lock could not be reclaimed; holding"
      return 1
    fi
  done
}

trap cleanup_update_scratch EXIT
trap 'exit 1' HUP INT TERM
if ! acquire_update_lock; then
  exit 0
fi

# Assemble --profile args from the space-separated product list.
PROFILE_ARGS=()
for _p in $UPDATE_PROFILES; do
  PROFILE_ARGS+=(--profile "$_p")
done
dc() { "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" "$@"; }
dc_over() {
  if [ -f "$OVERRIDE" ]; then
    "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" -f "$OVERRIDE" "$@"
  else
    "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" "$@"
  fi
}

alembic_current() {
  if command -v "$ALEMBIC" >/dev/null 2>&1; then
    "$ALEMBIC" current
  else
    dc_over exec -T onebrain-api alembic current
  fi | awk 'NF { print $1; exit }'
}

dump_onebrain_db() {
  if command -v "$PG_DUMP" >/dev/null 2>&1; then
    "$PG_DUMP" -Fc -d "$PG_CONN"
  else
    dc exec -T postgres pg_dump -U onebrain -Fc -d onebrain
  fi
}

restore_onebrain_db() {
  local archive="$1"
  if command -v "$PG_RESTORE" >/dev/null 2>&1; then
    "$PG_RESTORE" --clean --if-exists -d "$PG_CONN" "$archive"
  else
    dc exec -T postgres pg_restore -U onebrain --clean --if-exists -d onebrain <"$archive"
  fi
}

prune_encrypted_backups() {
  local backup_dir="$1" now cutoff newest="" newest_mtime=-1 archive mtime
  [ "$UPDATE_BACKUP_RETENTION_DAYS" -gt 0 ] || return 0
  [ -d "$backup_dir" ] || return 0
  now="$("$DATE" +%s 2>/dev/null || true)"
  case "$now" in
    ''|*[!0-9]*) return 0 ;;
  esac
  cutoff=$((now - UPDATE_BACKUP_RETENTION_DAYS * 86400))

  # Discover the newest archive first, then explicitly exclude it from every
  # deletion pass even if its timestamp is unexpectedly old.
  while IFS= read -r -d '' archive; do
    mtime="$(stat -c %Y "$archive" 2>/dev/null || stat -f %m "$archive" 2>/dev/null || true)"
    case "$mtime" in
      ''|*[!0-9]*) continue ;;
    esac
    if [ "$mtime" -gt "$newest_mtime" ]; then
      newest="$archive"
      newest_mtime="$mtime"
    fi
  done < <(find "$backup_dir" -maxdepth 1 -type f -name 'backup-*.dump.enc' -print0 2>/dev/null)
  [ -n "$newest" ] || return 0

  while IFS= read -r -d '' archive; do
    [ "$archive" -ef "$newest" ] && continue
    mtime="$(stat -c %Y "$archive" 2>/dev/null || stat -f %m "$archive" 2>/dev/null || true)"
    case "$mtime" in
      ''|*[!0-9]*) continue ;;
    esac
    if [ "$mtime" -lt "$cutoff" ]; then
      rm -f -- "$archive" || log "encrypted backup retention cleanup warned"
    fi
  done < <(find "$backup_dir" -maxdepth 1 -type f -name 'backup-*.dump.enc' -print0 2>/dev/null)
}

quiesce_application_services() {
  local services=(caddy)
  local service
  for service in ${UPDATE_LOCAL_MODULES//,/ }; do
    [ -n "$service" ] && services+=("$service")
  done
  dc_over "${PROFILE_ARGS[@]}" stop "${services[@]}"
}

resume_current_stack() {
  dc_over "${PROFILE_ARGS[@]}" up -d
}

wait_for_health() {
  local attempt
  for ((attempt = 1; attempt <= UPDATE_RECOVERY_HEALTH_ATTEMPTS; attempt++)); do
    if "$CURL" -sf "$UPDATE_HEALTH_URL" >/dev/null 2>>"$LOG"; then
      return 0
    fi
    if [ "$attempt" -lt "$UPDATE_RECOVERY_HEALTH_ATTEMPTS" ]; then
      sleep "$UPDATE_RECOVERY_HEALTH_INTERVAL_SECONDS"
    fi
  done
  return 1
}

restore_previous_override() {
  rm -f "$OVERRIDE_NEXT"
  if [ -f "$OVERRIDE_PREV" ]; then
    cp -f "$OVERRIDE_PREV" "$OVERRIDE"
  else
    rm -f "$OVERRIDE"
  fi
}

recover_current_stack() {
  if ! resume_current_stack >>"$LOG" 2>&1; then
    log "current stack restart FAILED"
    return 1
  fi
  if ! wait_for_health; then
    log "current stack health check FAILED"
    return 1
  fi
  return 0
}

recover_code_only() {
  log "recover code_only: restore previous digest set"
  restore_previous_override
  recover_current_stack
}

recover_restore_required() {
  log "recover restore_required: restore database + previous digest set"
  # Stop candidate application services before restoring the database. Postgres
  # itself remains running so pg_restore can repair the pre-migration schema.
  if ! quiesce_application_services >>"$LOG" 2>&1; then
    log "candidate quiesce FAILED; cannot restore database"
    return 1
  fi
  if [ -z "$ENC" ] || [ ! -r "$ENC" ]; then
    log "backup archive unavailable; cannot restore database"
    return 1
  fi
  # The plaintext dump was shredded after encryption (step 4), so decrypt the
  # encrypted archive into a short-lived file and never restore the old path.
  RESTORE="$WORK/restore.dump"
  if ! "$OPENSSL" enc -d -aes-256-cbc -pbkdf2 -pass "pass:${UPDATE_BACKUP_KEY:-}" \
       -in "$ENC" -out "$RESTORE" 2>>"$LOG"; then
    log "backup decrypt FAILED; cannot restore database"
    rm -f "$RESTORE"
    return 1
  fi
  if ! restore_onebrain_db "$RESTORE" >>"$LOG" 2>&1; then
    log "pg_restore FAILED; cannot restore database"
    shred -u "$RESTORE" 2>/dev/null || rm -f "$RESTORE"
    return 1
  fi
  shred -u "$RESTORE" 2>/dev/null || rm -f "$RESTORE"
  recover_code_only
}

recover_failed_candidate() {
  local migration_reached="$1"
  local reason="$2"
  log "$reason; restoring previous healthy stack"
  if [ "$ROLLBACK_KIND" = "restore_required" ]; then
    if recover_restore_required; then
      write_state "rolled_back" "$migration_reached" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
    else
      write_state "failed" "$migration_reached" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
    fi
  elif recover_code_only; then
    write_state "rolled_back" "$migration_reached" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
  else
    write_state "failed" "$migration_reached" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
  fi
}

read_env_assignment() {
  local env_path="$1"
  local key="$2"
  awk -v key="$key" '
    index($0, key "=") == 1 {
      count += 1
      value = substr($0, length(key) + 2)
    }
    END {
      if (count == 1 && value != "") {
        print value
        exit 0
      }
      exit 1
    }
  ' "$env_path"
}

role_split_preflight() {
  local app_role worker_role postgres_app_role postgres_worker_role
  local owner_password app_password worker_password assistant_password communication_password rate_limit_secret
  local role_name_re='^[A-Za-z_][A-Za-z0-9_$]{0,62}$'

  if [ ! -r "$MIGRATE_ENV" ] || [ ! -r "$POSTGRES_ENV" ] || [ ! -x "$POSTGRES_INIT" ]; then
    log "role-split preflight failed: required host assets missing"
    return 1
  fi
  if ! grep -Eq '^[[:space:]]+postgres-roles:' "$COMPOSE" \
     || ! grep -q 'postgres-init.sh' "$COMPOSE"; then
    log "role-split preflight failed: role normalizer is not in compose"
    return 1
  fi
  if ! app_role="$(read_env_assignment "$MIGRATE_ENV" ONEBRAIN_POSTGRES_APP_ROLE)" \
     || ! worker_role="$(read_env_assignment "$MIGRATE_ENV" ONEBRAIN_POSTGRES_WORKER_ROLE)" \
     || ! postgres_app_role="$(read_env_assignment "$POSTGRES_ENV" POSTGRES_APP_ROLE)" \
     || ! postgres_worker_role="$(read_env_assignment "$POSTGRES_ENV" POSTGRES_WORKER_ROLE)"; then
    log "role-split preflight failed: role names missing from rendered assets"
    return 1
  fi
  if ! [[ "$app_role" =~ $role_name_re ]] || ! [[ "$worker_role" =~ $role_name_re ]] \
     || [ "$app_role" = "$worker_role" ] \
     || [ "$app_role" != "$postgres_app_role" ] \
     || [ "$worker_role" != "$postgres_worker_role" ]; then
    log "role-split preflight failed: role names are unsafe or inconsistent"
    return 1
  fi
  owner_password=${POSTGRES_PASSWORD:-}
  app_password=${POSTGRES_APP_PASSWORD:-}
  worker_password=${POSTGRES_WORKER_PASSWORD:-}
  assistant_password=${POSTGRES_ASSISTANT_PASSWORD:-}
  communication_password=${POSTGRES_COMMUNICATION_PASSWORD:-}
  rate_limit_secret=${ONEBRAIN_LOGIN_RATE_LIMIT_SECRET:-}
  if [ "${#owner_password}" -lt 32 ] || [ "${#app_password}" -lt 32 ] \
     || [ "${#worker_password}" -lt 32 ] \
     || [ "${#assistant_password}" -lt 32 ] \
     || [ "${#communication_password}" -lt 32 ] \
     || [ "${#rate_limit_secret}" -lt 32 ]; then
    log "role-split preflight failed: role credentials are unavailable"
    return 1
  fi
  return 0
}

requires_role_split_preflight() {
  [ "$UPDATE_ROLE_SPLIT_REQUIRED" = "true" ] \
    || [ "$MIG_TO" = "0030_job_queue_rls_roles" ]
}

ATTEMPT_ID=""
TARGET_VERSION=""

# write_state <outcome> <migration_reached> <backup_status> <backup_ts> <backup_manifest>
# Writes the metadata-only UpdateReport (NO free-text reason — that stays in $LOG,
# off the fleet edge; ground rule 3). backup_manifest (7d/A17) is "sha256:<hex>:<bytes>"
# of the encrypted backup, or "" when no backup was taken.
write_state() {
  "$PYTHON" - "$STATE_FILE" "$1" "$2" "$TARGET_VERSION" "$ATTEMPT_ID" "$3" "$4" "$5" <<'PYEOF'
import sys, json, datetime
path, outcome, migration_reached, version, attempt_id, backup_status, backup_ts, backup_manifest = sys.argv[1:9]
json.dump({
    "last_target_version": version,
    "outcome": outcome,
    "migration_reached": migration_reached,
    "attempt_id": attempt_id,
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "backup_status": backup_status,
    "backup_ts": backup_ts,
    "backup_manifest": backup_manifest,
}, open(path, "w"))
PYEOF
}

# --- 0. FETCH + APPLY served floor bump (revocation kill-switch) ------------
# Runs BEFORE the desired-state fetch so a revoked box raises its local floor even
# if it would otherwise pull. Non-fatal / hold-on-unreachable (same discipline as
# the desired-state GET): a missing MC, no served bump, or a rejected bump never
# aborts the run. The box verifier re-checks the OFFLINE release signature
# (apply-floor-bump -> verify_floor_bump) against this box's OWN deployment id, so a
# compromised MC serving a forged/mis-scoped bump is rejected here.
log "fetch floor bump"
BUMP="$WORK/floor_bump.json"
BUMP_INNER="$WORK/bump_inner.json"
if "$CURL" -sf \
     -H "Authorization: Bearer ${ONEBRAIN_FLEET_KEY:-}" \
     -H "X-OneBrain-Deployment-Id: ${ONEBRAIN_DEPLOYMENT_ID:-}" \
     "${ONEBRAIN_FLEET_URL:-}/api/fleet/floor-bump" >"$BUMP" 2>>"$LOG" \
   && [ -s "$BUMP" ] && ! grep -qx 'null' "$BUMP"; then
  # Unwrap {"floor_bump": {...}} -> the signed bump; SystemExit(1) (skips apply) when
  # the body carries no bump object. Guarded by `if` so set -e never aborts here.
  if "$PYTHON" - "$BUMP" "$BUMP_INNER" 2>>"$LOG" <<'PYEOF'
import sys, json
outer = json.load(open(sys.argv[1]))
inner = outer.get("floor_bump")
if not isinstance(inner, dict):
    raise SystemExit(1)
json.dump(inner, open(sys.argv[2], "w"))
PYEOF
  then
    "$PYTHON" "$UPDATE_VERIFY_BIN" apply-floor-bump <"$BUMP_INNER" >>"$LOG" 2>&1 \
      || log "floor bump apply rejected (held local floor)"
  fi
fi

# --- 1. FETCH desired-state -------------------------------------------------
log "fetch desired-state"
SERVE="$WORK/serve.json"
if ! "$CURL" -sf \
     -H "Authorization: Bearer ${ONEBRAIN_FLEET_KEY:-}" \
     -H "X-OneBrain-Deployment-Id: ${ONEBRAIN_DEPLOYMENT_ID:-}" \
     "${ONEBRAIN_FLEET_URL:-}/api/fleet/desired-state" >"$SERVE" 2>>"$LOG"; then
  # MC unreachable -> HOLD (keep last-known-good; nothing destructive). Do not
  # write a failure outcome: this is not a rejection, just a missed poll.
  log "mc unreachable; holding last-known-good"
  exit 0
fi
# Empty body => emission disabled / no target for this box. Nothing to do.
if [ ! -s "$SERVE" ] || ! grep -q '[^[:space:]]' "$SERVE" || grep -qx 'null' "$SERVE"; then
  log "no desired-state served; nothing to do"
  exit 0
fi

# Split the serve envelope from the out-of-band attempt_id hint (P4-05 shape).
ENVELOPE="$WORK/envelope.json"
if ! "$PYTHON" - "$SERVE" "$ENVELOPE" >"$WORK/attempt_id" 2>>"$LOG" <<'PYEOF'
import sys, json
serve = json.load(open(sys.argv[1]))
env = serve.get("envelope")
if not isinstance(env, dict):
    raise SystemExit("no envelope")
json.dump(env, open(sys.argv[2], "w"))
print(serve.get("attempt_id", ""))
PYEOF
then
  log "serve payload had no envelope; holding"
  exit 0
fi
ATTEMPT_ID="$(cat "$WORK/attempt_id" 2>/dev/null || true)"

# --- 2. VERIFY (verify-don't-trust; A7 stdout is the ONLY image source) -----
log "verify envelope"
TARGET="$WORK/target.json"
if ! "$PYTHON" "$UPDATE_VERIFY_BIN" verify <"$ENVELOPE" >"$TARGET" 2>"$WORK/verify.err"; then
  REASON="$(head -n1 "$WORK/verify.err" 2>/dev/null || echo verify_failed)"
  log "verify REJECTED: $REASON"
  # A rejection IS a converged-negative for this offer: stamp outcome=failed so
  # the reconcile tick sees it (attempt_id gates it). The reason stays local.
  write_state "failed" "" "" "" ""
  exit 0
fi

read_target() { "$PYTHON" -c 'import sys,json;print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$TARGET" "$1"; }
TARGET_VERSION="$(read_target version)"
MIG_FROM="$(read_target migration_from)"
MIG_TO="$(read_target migration_to)"
ROLLBACK_KIND="$(read_target rollback_kind)"

# --- 3. NO-OP: compare the verified digest set to the last applied ----------
new_digest_set() { "$PYTHON" -c 'import sys,json;d=json.load(open(sys.argv[1]))["images"];print("\n".join(sorted(d.values())))' "$1"; }
NEW_DIGESTS="$(new_digest_set "$TARGET")"
OLD_DIGESTS=""
if [ -f "$LAST_APPLIED" ]; then
  OLD_DIGESTS="$(new_digest_set "$LAST_APPLIED")"
fi
if [ -n "$NEW_DIGESTS" ] && [ "$NEW_DIGESTS" = "$OLD_DIGESTS" ]; then
  log "no-op: already at verified digest set"
  exit 0
fi

# The gate agent refreshes the re-readable bundle before launching this script.
# If that refresh failed, still verify the offered target and report this exact
# attempt as failed, but never stop services or change image pins using stale
# credentials.
if [ "${UPDATE_BUNDLE_REFRESH_FAILED:-false}" = "true" ]; then
  log "bundle refresh prerequisite FAILED; holding current stack"
  write_state "failed" "$MIG_FROM" "" "" ""
  exit 0
fi

# The role split first enforced by 0030 needs host assets (the idempotent
# postgres-roles service plus rendered env files) and fresh credentials before
# the migration container is allowed to run. This deliberately happens before
# Caddy or application services are quiesced.
if requires_role_split_preflight && ! role_split_preflight; then
  log "migration prerequisite FAILED; holding current stack"
  write_state "failed" "$MIG_FROM" "" "" ""
  exit 0
fi

# Select this box's images from the verified target (H-5: the release pins ALL
# module images fleet-wide; a box pulls only its enabled modules). Writes the
# pin override and prints the refs to pull — A7: derived ONLY from verifier stdout.
select_images() {
  local output_path="$1"
  UPDATE_LOCAL_MODULES="$UPDATE_LOCAL_MODULES" "$PYTHON" - "$TARGET" "$output_path" <<'PYEOF'
import sys, json, os
target = json.load(open(sys.argv[1]))
override_path = sys.argv[2]
images = target.get("images", {})
local = [m.strip() for m in os.environ.get("UPDATE_LOCAL_MODULES", "").split(",") if m.strip()]
selected = {m: images[m] for m in local if m in images} if local else dict(images)
service_images = dict(selected)
if "onebrain-api" in selected:
    service_images["onebrain-migrate"] = selected["onebrain-api"]
lines = ["services:"]
for m, ref in sorted(service_images.items()):
    lines.append("  %s:" % m)
    lines.append("    image: %s" % ref)
with open(override_path, "w") as fh:
    fh.write("\n".join(lines) + "\n")
for _, ref in sorted(selected.items()):
    print(ref)
PYEOF
}

install_next_override() {
  if [ -f "$OVERRIDE" ]; then
    cp -f "$OVERRIDE" "$OVERRIDE_PREV"
  else
    rm -f "$OVERRIDE_PREV"
  fi
  mv -f "$OVERRIDE_NEXT" "$OVERRIDE"
}

BACKUP_STATUS=""
BACKUP_TS=""
BACKUP_MANIFEST=""   # 7d/A17: sha256:<hex>:<bytes> of $ENC; "" until a backup is taken
PRE_MIGRATION_REV=""
ENC=""   # encrypted backup path (set in step 4); recover decrypts THIS, never the shredded plaintext

# --- 4. QUIESCE + BACKUP before a schema change -----------------------------
crosses_migration() { [ "$MIG_FROM" != "$MIG_TO" ] || [ "$ROLLBACK_KIND" = "restore_required" ]; }
if crosses_migration; then
  log "schema change -> quiesce + backup"
  backup_key="${UPDATE_BACKUP_KEY:-}"
  if [ "${#backup_key}" -lt 32 ]; then
    log "migration backup key is unavailable or too short; holding current stack"
    write_state "failed" "$MIG_FROM" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
    exit 0
  fi
  PRE_MIGRATION_REV="$(alembic_current 2>/dev/null | tr -d '[:space:]' || true)"
  if ! quiesce_application_services >>"$LOG" 2>&1; then
    log "quiesce FAILED; holding current stack before backup"
    recover_current_stack || log "current stack recovery after quiesce failure FAILED"
    write_state "failed" "$PRE_MIGRATION_REV" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
    exit 0
  fi
  printf '%s\n' "$PRE_MIGRATION_REV" >"$WORK/pre_migration_revision"
  DUMP="$WORK/backup.dump"
  ENC="${BACKUP_DIR}/backup-$(date -u +%Y%m%dT%H%M%SZ).dump.enc"
  mkdir -p "$BACKUP_DIR"
  chmod 0700 "$BACKUP_DIR"
  if dump_onebrain_db >"$DUMP" 2>>"$LOG" \
     && "$OPENSSL" enc -aes-256-cbc -pbkdf2 -salt -pass "pass:${UPDATE_BACKUP_KEY:-}" -in "$DUMP" -out "$ENC" 2>>"$LOG"; then
    BACKUP_STATUS="success"
    BACKUP_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # 7d/A17: a metadata-only manifest (digest+size of the ENCRYPTED backup object, never
    # a path or content) so MC can gate the migration-crossing rollout above a bare
    # self-reported "success". Recorded in update_state.json + emitted in the heartbeat.
    BACKUP_MANIFEST="sha256:$(sha256sum "$ENC" 2>>"$LOG" | cut -d' ' -f1):$(wc -c <"$ENC" 2>>"$LOG" | tr -d '[:space:]')"
    rm -f "$DUMP"
    log "backup ok -> $ENC"
    prune_encrypted_backups "$BACKUP_DIR"
  else
    BACKUP_STATUS="failed"
    log "backup FAILED; restoring current stack and holding (no destructive apply)"
    recover_current_stack || log "current stack recovery after backup failure FAILED"
    write_state "failed" "$PRE_MIGRATION_REV" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
    exit 0
  fi
fi

# --- 5. PULL + UP by digest (verifier map ONLY, A7) -------------------------
log "pull + up (verified digests)"
rm -f "$OVERRIDE_NEXT"
if ! REFS="$(select_images "$OVERRIDE_NEXT")"; then
  log "verified image selection FAILED; restoring current stack"
  rm -f "$OVERRIDE_NEXT"
  recover_current_stack || log "current stack recovery after image selection failure FAILED"
  write_state "failed" "${PRE_MIGRATION_REV:-$MIG_FROM}" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
  exit 0
fi
for ref in $REFS; do
  if ! "$DOCKER" pull "$ref" >>"$LOG" 2>&1; then
    log "verified image pull FAILED; restoring current stack"
    rm -f "$OVERRIDE_NEXT"
    recover_current_stack || log "current stack recovery after image pull failure FAILED"
    write_state "failed" "${PRE_MIGRATION_REV:-$MIG_FROM}" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
    exit 0
  fi
done
if ! install_next_override; then
  log "override install FAILED; restoring current stack"
  restore_previous_override
  recover_current_stack || log "current stack recovery after override failure FAILED"
  write_state "failed" "${PRE_MIGRATION_REV:-$MIG_FROM}" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
  exit 0
fi
MIGRATION_REACHED="${PRE_MIGRATION_REV:-$MIG_FROM}"
if ! dc_over "${PROFILE_ARGS[@]}" pull >>"$LOG" 2>&1; then
  recover_failed_candidate "$MIGRATION_REACHED" "compose pull FAILED"
  exit 0
fi
if ! dc_over "${PROFILE_ARGS[@]}" up -d >>"$LOG" 2>&1; then
  recover_failed_candidate "$MIGRATION_REACHED" "candidate startup/migration FAILED"
  exit 0
fi

# --- 6. FENCE, don't flap ---------------------------------------------------
MIGRATION_REACHED="$MIG_TO"
if [ "$MIG_FROM" != "$MIG_TO" ]; then
  CURRENT="$(alembic_current 2>/dev/null | tr -d '[:space:]' || true)"
  MIGRATION_REACHED="$CURRENT"
  if [ "$CURRENT" != "$MIG_TO" ]; then
    recover_failed_candidate "$CURRENT" "migration fence FAILED: alembic current=$CURRENT != target=$MIG_TO"
    exit 0
  fi
fi

# --- 7. SMOKE + recover by rollback_kind ------------------------------------
if ! "$CURL" -sf "$UPDATE_HEALTH_URL" >/dev/null 2>>"$LOG"; then
  recover_failed_candidate "$MIGRATION_REACHED" "smoke FAILED (rollback_kind=$ROLLBACK_KIND)"
  exit 0
fi

# --- 8/9. SUCCESS: record floor+nonce, persist last-applied + UpdateReport ---
"$PYTHON" "$UPDATE_VERIFY_BIN" record-apply <"$ENVELOPE" >>"$LOG" 2>&1 || log "record-apply warned"
cp -f "$TARGET" "$LAST_APPLIED"
write_state "succeeded" "$MIGRATION_REACHED" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
log "update SUCCEEDED -> $TARGET_VERSION"
exit 0
