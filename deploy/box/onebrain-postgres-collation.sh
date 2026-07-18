#!/usr/bin/env bash
# Check or repair PostgreSQL collation-version drift on a OneBrain host.
#
# `check` is read-only. `apply` creates an encrypted recoverable pg_dump for
# every affected database, stops verified application services, reindexes,
# refreshes the recorded collation version, then restores and health-checks
# the stack.
set -euo pipefail

MODE="${1:-check}"
case "$MODE" in
  check|apply) ;;
  *) printf '%s\n' "usage: $0 [check|apply]" >&2; exit 2 ;;
esac

ROOT="$(dirname "$0")"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
BOX_ENV="${BOX_ENV:-$ROOT/box.env}"
DOTENV_LOADER="$ROOT/onebrain_dotenv.sh"

if [ ! -r "$DOTENV_LOADER" ]; then
  printf '%s\n' 'OneBrain collation maintenance: dotenv loader unavailable; holding' >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$DOTENV_LOADER"
if [ -f "$ENV_FILE" ] && ! onebrain_load_dotenv "$ENV_FILE"; then
  printf '%s\n' 'OneBrain collation maintenance: invalid dotenv; holding' >&2
  exit 1
fi
if [ -f "$BOX_ENV" ]; then
  set +u
  set -a
  # shellcheck disable=SC1090
  . "$BOX_ENV"
  set +a
  set -u
fi

: "${DOCKER:=docker}"
: "${CURL:=curl}"
: "${OPENSSL:=openssl}"
: "${FLOCK:=flock}"
: "${UPDATE_COMPOSE_DIR:=/opt/onebrain}"
: "${UPDATE_COMPOSE_PROJECT:=onebrain}"
: "${UPDATE_PROFILES:=onebrain}"
: "${UPDATE_HEALTH_URL:=http://127.0.0.1/health}"
# The rendered host config names the real persistent volume explicitly.  Keep
# the older reporter name only as a compatibility fallback; never use the
# container's root-disk update path (/data) for maintenance artifacts.
: "${ONEBRAIN_DATA_MOUNT:=${ONEBRAIN_DATA_VOLUME_PATH:-/mnt/onebrain-data}}"
: "${ONEBRAIN_MAINTENANCE_DIR:=${ONEBRAIN_DATA_MOUNT}/maintenance}"
: "${ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT:=$ROOT/onebrain-data-volume.sh}"
# Each backup is conservatively budgeted from the live database size, plus
# this margin.  The floor covers catalog/WAL churn and encryption overhead for
# small databases; the percentage covers the larger installations.
: "${ONEBRAIN_COLLATION_BACKUP_MARGIN_PERCENT:=25}"
: "${ONEBRAIN_COLLATION_BACKUP_MIN_MARGIN_BYTES:=268435456}"
: "${ONEBRAIN_COLLATION_BACKUP_RETENTION_DAYS:=30}"

COMPOSE="$UPDATE_COMPOSE_DIR/docker-compose.yml"
OVERRIDE="$UPDATE_COMPOSE_DIR/images.override.yml"
BACKUP_DIR="$ONEBRAIN_MAINTENANCE_DIR/collation-backups"
LOCK_FILE="$ONEBRAIN_MAINTENANCE_DIR/onebrain-postgres-collation.lock"
OWNER_ROLE="${POSTGRES_USER:-onebrain}"
PROFILE_ARGS=()
for profile in $UPDATE_PROFILES; do PROFILE_ARGS+=(--profile "$profile"); done
APP_SERVICES=()
quiesced=0
MAINTENANCE_LOCK_HELD=0
MAINTENANCE_LOCK_FD=""

dc() { "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" "$@"; }
dc_over() {
  if [ -f "$OVERRIDE" ]; then
    "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" -f "$OVERRIDE" "$@"
  else
    "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" "$@"
  fi
}

list_mismatched_databases() {
  dc_over exec -T postgres psql -v ON_ERROR_STOP=1 -U "$OWNER_ROLE" -d postgres -At -c "
    SELECT datname
    FROM pg_database
    WHERE datallowconn
      AND datname <> 'template0'
      AND datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid)
    ORDER BY datname;
  "
}

assert_safe_database_name() {
  case "$1" in
    [A-Za-z_]* ) ;;
    *) return 1 ;;
  esac
  case "$1" in
    *[!A-Za-z0-9_]* ) return 1 ;;
  esac
}

assert_no_explicit_collation_drift() {
  local database="$1" count
  count="$(dc_over exec -T postgres psql -v ON_ERROR_STOP=1 -U "$OWNER_ROLE" -d "$database" -At -c "
    SELECT count(*)
    FROM pg_collation c
    JOIN pg_namespace n ON n.oid = c.collnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND c.collversion IS DISTINCT FROM pg_collation_actual_version(c.oid);
  ")"
  if [ "$count" != "0" ]; then
    printf 'OneBrain collation maintenance: explicit collation drift in %s; manual dependency rebuild required\n' "$database" >&2
    return 1
  fi
}

is_uint() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

assert_safe_service_name() {
  case "${1:-}" in
    ''|-*|*[!A-Za-z0-9_.-]*) return 1 ;;
    *) return 0 ;;
  esac
}

verify_compose_config() {
  # Do not trust a partial/invalid config when deciding which writers to stop.
  # Suppress compose's expansion diagnostics: they can contain environment
  # values and are not useful to a human running this explicit maintenance job.
  if ! dc_over "${PROFILE_ARGS[@]}" config -q >/dev/null 2>&1; then
    printf '%s\n' 'OneBrain collation maintenance: compose configuration could not be verified; holding' >&2
    return 1
  fi
}

discover_application_services() {
  local services service
  APP_SERVICES=()
  if ! services="$(dc_over "${PROFILE_ARGS[@]}" config --services 2>/dev/null)"; then
    printf '%s\n' 'OneBrain collation maintenance: application service discovery failed; holding' >&2
    return 1
  fi
  while IFS= read -r service; do
    [ -n "$service" ] || continue
    if ! assert_safe_service_name "$service"; then
      printf '%s\n' 'OneBrain collation maintenance: invalid service name from compose configuration; holding' >&2
      return 1
    fi
    case "$service" in
      postgres|postgres-roles|redis|caddy|*-migrate) continue ;;
    esac
    APP_SERVICES+=("$service")
  done <<<"$services"
}

validate_backup_key() {
  local backup_key="${UPDATE_BACKUP_KEY:-}"
  if [ "${#backup_key}" -lt 32 ]; then
    printf '%s\n' 'OneBrain collation maintenance: backup encryption key unavailable or too short; holding' >&2
    return 1
  fi
}

database_backup_bytes() {
  local database bytes total=0
  for database in "$@"; do
    if ! bytes="$(dc_over exec -T postgres psql -v ON_ERROR_STOP=1 -U "$OWNER_ROLE" -d postgres -At -c "
      SELECT pg_database_size(datname)
      FROM pg_database
      WHERE datname = '$database';
    " 2>/dev/null)"; then
      printf '%s\n' 'OneBrain collation maintenance: could not measure database backup size; holding' >&2
      return 1
    fi
    bytes="$(printf '%s' "$bytes" | tr -d '[:space:]')"
    if ! is_uint "$bytes"; then
      printf '%s\n' 'OneBrain collation maintenance: database backup size was invalid; holding' >&2
      return 1
    fi
    total=$((total + 10#$bytes))
  done
  printf '%s\n' "$total"
}

required_backup_bytes() {
  local database_bytes="$1" margin_percent="$ONEBRAIN_COLLATION_BACKUP_MARGIN_PERCENT"
  local minimum_margin="$ONEBRAIN_COLLATION_BACKUP_MIN_MARGIN_BYTES" margin
  if ! is_uint "$database_bytes" || ! is_uint "$margin_percent" || ! is_uint "$minimum_margin" \
     || [ "$margin_percent" -lt 10 ] || [ "$margin_percent" -gt 100 ] || [ "$minimum_margin" -lt 1 ]; then
    printf '%s\n' 'OneBrain collation maintenance: backup-capacity policy is invalid; holding' >&2
    return 1
  fi
  margin=$((10#$database_bytes * 10#$margin_percent / 100))
  if [ "$margin" -lt "$minimum_margin" ]; then
    margin="$minimum_margin"
  fi
  printf '%s\n' "$((10#$database_bytes + margin))"
}

available_bytes() {
  local bytes
  if ! bytes="$(df -Pk "$ONEBRAIN_MAINTENANCE_DIR" 2>/dev/null | awk 'NR == 2 { print $4 * 1024 }')"; then
    return 1
  fi
  bytes="$(printf '%s' "$bytes" | tr -d '[:space:]')"
  is_uint "$bytes" || return 1
  printf '%s\n' "$bytes"
}

verify_maintenance_directory() {
  local relative mount_real maintenance_real mounted_target
  case "$ONEBRAIN_DATA_MOUNT" in
    /*) ;;
    *)
      printf '%s\n' 'OneBrain collation maintenance: data-volume mount path is invalid; holding' >&2
      return 1
      ;;
  esac
  case "$ONEBRAIN_MAINTENANCE_DIR" in
    "$ONEBRAIN_DATA_MOUNT"/*) relative="${ONEBRAIN_MAINTENANCE_DIR#"$ONEBRAIN_DATA_MOUNT"/}" ;;
    *)
      printf '%s\n' 'OneBrain collation maintenance: maintenance directory must be below the data volume; holding' >&2
      return 1
      ;;
  esac
  case "/$relative/" in
    *'//'*|*'/./'*|*'/../'*)
      printf '%s\n' 'OneBrain collation maintenance: maintenance directory path is unsafe; holding' >&2
      return 1
      ;;
  esac
  if ! command -v mountpoint >/dev/null 2>&1 || ! command -v findmnt >/dev/null 2>&1 \
     || ! command -v readlink >/dev/null 2>&1; then
    printf '%s\n' 'OneBrain collation maintenance: mount verification tools unavailable; holding' >&2
    return 1
  fi
  if ! mountpoint -q "$ONEBRAIN_DATA_MOUNT"; then
    printf '%s\n' 'OneBrain collation maintenance: required data volume is not mounted; holding' >&2
    return 1
  fi
  if [ ! -x "$ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT" ] \
     || ! ONEBRAIN_DATA_MOUNT="$ONEBRAIN_DATA_MOUNT" "$ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT" verify >/dev/null 2>&1; then
    printf '%s\n' 'OneBrain collation maintenance: required data volume could not be verified; holding' >&2
    return 1
  fi

  # Create the only permitted host-side maintenance root with strict access,
  # then prove that canonicalization cannot escape or enter a nested mount.
  umask 077
  if ! install -d -o root -g root -m 0700 "$ONEBRAIN_MAINTENANCE_DIR"; then
    printf '%s\n' 'OneBrain collation maintenance: could not prepare maintenance directory; holding' >&2
    return 1
  fi
  if ! mount_real="$(readlink -f -- "$ONEBRAIN_DATA_MOUNT" 2>/dev/null)" \
     || ! maintenance_real="$(readlink -f -- "$ONEBRAIN_MAINTENANCE_DIR" 2>/dev/null)"; then
    printf '%s\n' 'OneBrain collation maintenance: could not canonicalize maintenance directory; holding' >&2
    return 1
  fi
  case "$maintenance_real" in
    "$mount_real"/*) ;;
    *)
      printf '%s\n' 'OneBrain collation maintenance: maintenance directory escaped the data volume; holding' >&2
      return 1
      ;;
  esac
  if ! mounted_target="$(findmnt -nr -o TARGET --target "$maintenance_real" 2>/dev/null)" \
     || [ "$mounted_target" != "$mount_real" ]; then
    printf '%s\n' 'OneBrain collation maintenance: maintenance directory is not on the verified data volume; holding' >&2
    return 1
  fi
}

acquire_maintenance_lock() {
  if ! command -v "$FLOCK" >/dev/null 2>&1; then
    printf '%s\n' 'OneBrain collation maintenance: host lock utility unavailable; holding' >&2
    return 1
  fi
  if ! exec {MAINTENANCE_LOCK_FD}>"$LOCK_FILE"; then
    printf '%s\n' 'OneBrain collation maintenance: could not open host maintenance lock; holding' >&2
    return 1
  fi
  if ! "$FLOCK" -n "$MAINTENANCE_LOCK_FD"; then
    exec {MAINTENANCE_LOCK_FD}>&-
    MAINTENANCE_LOCK_FD=""
    printf '%s\n' 'OneBrain collation maintenance: another maintenance run is active; holding' >&2
    return 1
  fi
  MAINTENANCE_LOCK_HELD=1
}

release_maintenance_lock() {
  [ "$MAINTENANCE_LOCK_HELD" = "1" ] || return 0
  "$FLOCK" -u "$MAINTENANCE_LOCK_FD" >/dev/null 2>&1 || true
  exec {MAINTENANCE_LOCK_FD}>&-
  MAINTENANCE_LOCK_FD=""
  MAINTENANCE_LOCK_HELD=0
}

resume_stack() {
  if [ "$quiesced" != "1" ]; then
    return 0
  fi
  # Only re-start the writers that were deliberately selected from a verified
  # compose config.  This also recovers a partial `compose stop` failure.
  if [ "${#APP_SERVICES[@]}" -gt 0 ] \
     && ! dc_over "${PROFILE_ARGS[@]}" up -d "${APP_SERVICES[@]}" >/dev/null 2>&1; then
    printf '%s\n' 'OneBrain collation maintenance: application recovery failed' >&2
    return 1
  fi
  quiesced=0
}
cleanup() {
  status=$?
  if ! resume_stack && [ "$status" -eq 0 ]; then
    status=1
  fi
  release_maintenance_lock
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT

quiesce_application_services() {
  [ "${#APP_SERVICES[@]}" -gt 0 ] || return 0
  # Arm the EXIT trap before stopping anything.  Docker Compose can stop a
  # prefix of the service list before returning a failure.
  quiesced=1
  dc_over "${PROFILE_ARGS[@]}" stop "${APP_SERVICES[@]}"
}

backup_database() {
  local database="$1" timestamp="$2" backup
  if ! backup="$(mktemp "$BACKUP_DIR/${database}-${timestamp}-XXXXXXXX.dump.enc" 2>/dev/null)"; then
    printf '%s\n' 'OneBrain collation maintenance: could not allocate encrypted backup path; holding' >&2
    return 1
  fi
  # Stream the custom-format dump straight into OpenSSL: a plaintext archive
  # never reaches disk.  `env:` avoids exposing the key in a process argument.
  if ! dc_over exec -T postgres pg_dump -U "$OWNER_ROLE" -Fc -d "$database" 2>/dev/null \
      | "$OPENSSL" enc -aes-256-cbc -pbkdf2 -salt -pass env:UPDATE_BACKUP_KEY -out "$backup" 2>/dev/null; then
    rm -f -- "$backup"
    printf '%s\n' 'OneBrain collation maintenance: encrypted backup failed; holding' >&2
    return 1
  fi
  if [ ! -s "$backup" ]; then
    rm -f -- "$backup"
    printf '%s\n' 'OneBrain collation maintenance: encrypted backup was empty; holding' >&2
    return 1
  fi
  chmod 0600 "$backup"
}

is_safe_backup_name() {
  [[ "${1:-}" =~ ^[A-Za-z_][A-Za-z0-9_]*-[0-9]{8}T[0-9]{6}Z(-[A-Za-z0-9]{6,})?\.dump\.enc$ ]]
}

retain_encrypted_backups() {
  local retention_days="$ONEBRAIN_COLLATION_BACKUP_RETENTION_DAYS"
  local now backup newest='' mtime retention_seconds base
  local -a backups=()
  if ! is_uint "$retention_days"; then
    printf '%s\n' 'OneBrain collation maintenance: backup-retention policy is invalid; keeping all backups' >&2
    return 1
  fi
  while IFS= read -r -d '' backup; do
    base="${backup##*/}"
    is_safe_backup_name "$base" || continue
    backups+=("$backup")
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump.enc' -print0 2>/dev/null)
  [ "${#backups[@]}" -gt 0 ] || return 0

  newest="${backups[0]}"
  for backup in "${backups[@]}"; do
    [ "$backup" -nt "$newest" ] && newest="$backup"
  done
  now="$(date +%s)"
  if ! is_uint "$now"; then
    printf '%s\n' 'OneBrain collation maintenance: could not evaluate backup retention; keeping all backups' >&2
    return 1
  fi
  retention_seconds=$((10#$retention_days * 86400))
  for backup in "${backups[@]}"; do
    [ "$backup" = "$newest" ] && continue
    if ! mtime="$(stat -c %Y -- "$backup" 2>/dev/null)" || ! is_uint "$mtime"; then
      printf '%s\n' 'OneBrain collation maintenance: could not evaluate a backup age; keeping remaining backups' >&2
      return 1
    fi
    if [ $((10#$now - 10#$mtime)) -ge "$retention_seconds" ]; then
      rm -f -- "$backup" || {
        printf '%s\n' 'OneBrain collation maintenance: backup retention could not remove an expired archive' >&2
        return 1
      }
    fi
  done
}

# Check mode remains read-only.  Apply creates/acquires the host lock before
# its first SQL query, so no two destructive runs can overlap.
if [ "$MODE" = "apply" ]; then
  validate_backup_key
  verify_maintenance_directory
  acquire_maintenance_lock
fi

if ! MISMATCHED_DATABASES="$(list_mismatched_databases)"; then
  printf '%s\n' 'OneBrain collation maintenance: could not query PostgreSQL collation state' >&2
  exit 1
fi
if [ -z "$MISMATCHED_DATABASES" ]; then
  printf '%s\n' 'OneBrain collation maintenance: no mismatch detected'
  exit 0
fi
mapfile -t DATABASES <<<"$MISMATCHED_DATABASES"

printf 'OneBrain collation maintenance: affected databases: %s\n' "${DATABASES[*]}"
for database in "${DATABASES[@]}"; do
  assert_safe_database_name "$database" || {
    printf 'OneBrain collation maintenance: unsafe database name from PostgreSQL catalog\n' >&2
    exit 1
  }
  assert_no_explicit_collation_drift "$database"
done

if [ "$MODE" = "check" ]; then
  printf '%s\n' 'OneBrain collation maintenance: check complete; rerun with apply during a maintenance window'
  exit 0
fi

verify_compose_config
discover_application_services

if ! database_bytes="$(database_backup_bytes "${DATABASES[@]}")"; then
  exit 1
fi
if ! required_bytes="$(required_backup_bytes "$database_bytes")"; then
  exit 1
fi
if ! free_bytes="$(available_bytes)" || [ "$free_bytes" -lt "$required_bytes" ]; then
  printf 'OneBrain collation maintenance: insufficient free space for encrypted backups (need %s bytes)\n' "$required_bytes" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
for database in "${DATABASES[@]}"; do
  backup_database "$database" "$timestamp"
done
if ! retain_encrypted_backups; then
  # A retention failure does not make a newly encrypted backup unsafe and must
  # not leave a successful reindex incorrectly reported as failed.
  printf '%s\n' 'OneBrain collation maintenance: backup retention deferred' >&2
fi

quiesce_application_services

for database in "${DATABASES[@]}"; do
  dc_over exec -T postgres psql -v ON_ERROR_STOP=1 -U "$OWNER_ROLE" -d "$database" \
    -c "REINDEX (VERBOSE) DATABASE \"$database\""
  dc_over exec -T postgres psql -v ON_ERROR_STOP=1 -U "$OWNER_ROLE" -d "$database" \
    -c "ALTER DATABASE \"$database\" REFRESH COLLATION VERSION"
done

if ! REMAINING_MISMATCHES="$(list_mismatched_databases)"; then
  printf '%s\n' 'OneBrain collation maintenance: could not verify PostgreSQL collation state' >&2
  exit 1
fi
if [ -n "$REMAINING_MISMATCHES" ]; then
  printf '%s\n' 'OneBrain collation maintenance: mismatch remained after reindex' >&2
  exit 1
fi

if ! resume_stack; then
  exit 1
fi
if ! "$CURL" -fsS "$UPDATE_HEALTH_URL" >/dev/null; then
  printf '%s\n' 'OneBrain collation maintenance: stack did not become healthy after maintenance' >&2
  exit 1
fi
printf '%s\n' 'OneBrain collation maintenance: applied and healthy'
