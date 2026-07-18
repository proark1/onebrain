#!/usr/bin/env bash
# Scheduled customer-local backup and explicit restore contract for the OneBrain
# database plus Drive originals. API/workers are quiesced so both sides share a
# consistency boundary. Archives use streaming authenticated encryption, and an
# external append-only erasure ledger prevents an old snapshot from reviving a
# deletion that happened after that snapshot.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/onebrain/.env}"
BOX_ENV="${BOX_ENV:-/opt/onebrain/box.env}"
DOTENV_LOADER="${DOTENV_LOADER:-$(dirname "$0")/onebrain_dotenv.sh}"

[ -r "$DOTENV_LOADER" ] || { printf '%s\n' 'OneBrain Drive backup: dotenv loader unavailable' >&2; exit 1; }
# shellcheck disable=SC1090
. "$DOTENV_LOADER"
if [ -f "$ENV_FILE" ]; then
  onebrain_load_dotenv "$ENV_FILE" \
    || { printf '%s\n' 'OneBrain Drive backup: invalid dotenv' >&2; exit 1; }
fi
if [ -f "$BOX_ENV" ]; then
  # box.env is renderer-owned and may expand secret references loaded above.
  set +u
  set -a
  # shellcheck disable=SC1090
  . "$BOX_ENV"
  set +a
  set -u
fi

: "${DOCKER:=docker}"
: "${PYTHON:=python3}"
: "${TAR:=tar}"
: "${SHA256SUM:=sha256sum}"
: "${UPDATE_COMPOSE_DIR:=/opt/onebrain}"
: "${UPDATE_COMPOSE_PROJECT:=onebrain}"
: "${UPDATE_DATA_DIR:=/data}"
: "${ONEBRAIN_DATA_MOUNT:=/mnt/onebrain-data}"
: "${ONEBRAIN_DRIVE_BACKUP_DIR:=/var/lib/onebrain/drive-backups}"
: "${ONEBRAIN_DRIVE_BACKUP_RETENTION_DAYS:=7}"
: "${ONEBRAIN_ERASURE_LEDGER_DIR:=${ONEBRAIN_DATA_MOUNT}/.onebrain-erasure-ledger}"

COMPOSE="${UPDATE_COMPOSE_DIR}/docker-compose.yml"
DRIVE_DIR="${ONEBRAIN_DATA_MOUNT}/drive"
LOCK_DIR="/run/onebrain-drive-backup.lock"
UPDATE_LOCK="${UPDATE_DATA_DIR}/onebrain_update/update.lock"
VOLUME_TOOL="${UPDATE_COMPOSE_DIR}/onebrain-data-volume.sh"
CRYPTO_HELPER="${CRYPTO_HELPER:-$(dirname "$0")/onebrain_backup_crypto.py}"
LEDGER_HELPER="${LEDGER_HELPER:-$(dirname "$0")/onebrain_erasure_ledger.py}"
LEDGER_FILE="${ONEBRAIN_ERASURE_LEDGER_DIR}/ledger.ndjson"
LEDGER_INIT_MARKER="${ONEBRAIN_DATA_MOUNT}/.onebrain-erasure-ledger-uninitialized"
WORK=""
RESTORE_STAGE=""
LEDGER_ROWS=""
APP_QUIESCED=false
UPDATE_LOCK_ACQUIRED=false
RESTORE_OLD_DRIVE=""
PARTIAL_ARCHIVE=""

die() {
  printf 'OneBrain Drive backup: %s\n' "$*" >&2
  exit 1
}

dc() {
  "$DOCKER" compose --project-name "$UPDATE_COMPOSE_PROJECT" -f "$COMPOSE" "$@"
}

resume_application() {
  if [ "$APP_QUIESCED" = true ]; then
    dc --profile onebrain start onebrain-api onebrain-workers >/dev/null
    APP_QUIESCED=false
  fi
}

safe_remove_work() {
  local path="${1:-}"
  [ -n "$path" ] || return 0
  case "$path" in
    "$ONEBRAIN_DRIVE_BACKUP_DIR"/.work.*|"$ONEBRAIN_DATA_MOUNT"/.drive-restore.*)
      rm -rf -- "$path"
      ;;
    *) printf '%s\n' 'OneBrain Drive backup: refusing to remove unexpected temporary path' >&2; return 1 ;;
  esac
}

safe_remove_backup_file() {
  local path="${1:-}"
  [ -n "$path" ] || return 0
  case "$path" in
    "$ONEBRAIN_DRIVE_BACKUP_DIR"/onebrain-drive-*.obk|\
    "$ONEBRAIN_DRIVE_BACKUP_DIR"/onebrain-drive-*.obk.partial)
      rm -f -- "$path"
      ;;
    *) printf '%s\n' 'OneBrain Drive backup: refusing to remove unexpected backup path' >&2; return 1 ;;
  esac
}

cleanup() {
  local status=$?
  if [ -n "$RESTORE_OLD_DRIVE" ] && [ -d "$RESTORE_OLD_DRIVE" ]; then
    if [ -e "$DRIVE_DIR" ]; then
      [ -n "$RESTORE_STAGE" ] && mv "$DRIVE_DIR" "$RESTORE_STAGE/failed-drive" 2>/dev/null || true
    fi
    mv "$RESTORE_OLD_DRIVE" "$DRIVE_DIR" 2>/dev/null || true
  fi
  resume_application || true
  safe_remove_work "$WORK" || true
  safe_remove_work "$RESTORE_STAGE" || true
  if [ -n "$LEDGER_ROWS" ]; then
    case "$LEDGER_ROWS" in
      "$ONEBRAIN_ERASURE_LEDGER_DIR"/.rows.*) rm -f -- "$LEDGER_ROWS" ;;
    esac
  fi
  safe_remove_backup_file "$PARTIAL_ARCHIVE" || true
  [ "$UPDATE_LOCK_ACQUIRED" = false ] || rmdir "$UPDATE_LOCK" 2>/dev/null || true
  rmdir "$LOCK_DIR" 2>/dev/null || true
  exit "$status"
}

require_foundation() {
  [ -n "${UPDATE_BACKUP_KEY:-}" ] || die "UPDATE_BACKUP_KEY is required"
  [ -x "$VOLUME_TOOL" ] || die "data-volume verifier is unavailable"
  "$VOLUME_TOOL" verify
  [ -d "$DRIVE_DIR" ] || die "Drive directory is unavailable"
  [ -n "${ONEBRAIN_DEPLOYMENT_ID:-}" ] || die "ONEBRAIN_DEPLOYMENT_ID is required"
  [ -r "$LEDGER_HELPER" ] || die "erasure-ledger helper is unavailable"
  install -d -o root -g root -m 0700 /var/lib/onebrain
  install -d -o root -g root -m 0700 "$ONEBRAIN_DRIVE_BACKUP_DIR"
  install -d -o root -g root -m 0700 "$ONEBRAIN_ERASURE_LEDGER_DIR"
}

quiesce_application() {
  dc --profile onebrain stop onebrain-api onebrain-workers >/dev/null
  APP_QUIESCED=true
}

require_crypto() {
  [ -r "$CRYPTO_HELPER" ] || die "authenticated-backup helper is unavailable"
}

crypto_decrypt() {
  "$PYTHON" "$CRYPTO_HELPER" decrypt --input "$1"
}

ensure_erasure_ledger() {
  if [ ! -f "$LEDGER_FILE" ]; then
    [ -e "$LEDGER_INIT_MARKER" ] \
      || die "external erasure ledger is missing; refusing to initialize implicitly"
    "$PYTHON" "$LEDGER_HELPER" init --path "$LEDGER_FILE" \
      --deployment-id "$ONEBRAIN_DEPLOYMENT_ID" >/dev/null
  fi
  "$PYTHON" "$LEDGER_HELPER" verify --path "$LEDGER_FILE" \
    --deployment-id "$ONEBRAIN_DEPLOYMENT_ID" >/dev/null
  if [ -e "$LEDGER_INIT_MARKER" ]; then
    rm -f -- "$LEDGER_INIT_MARKER"
    [ ! -e "$LEDGER_INIT_MARKER" ] || die "erasure-ledger initialization marker could not be retired"
  fi
}

sync_erasure_ledger() {
  local last_seq new_seq
  ensure_erasure_ledger
  last_seq="$("$PYTHON" "$LEDGER_HELPER" verify --path "$LEDGER_FILE" \
    --deployment-id "$ONEBRAIN_DEPLOYMENT_ID")"
  [[ "$last_seq" =~ ^[0-9]+$ ]] || die "external erasure-ledger cursor is invalid"
  LEDGER_ROWS="$(mktemp "$ONEBRAIN_ERASURE_LEDGER_DIR/.rows.XXXXXX")"
  dc exec -T postgres psql -U onebrain -d onebrain -Atqc \
    "SELECT json_build_object(
       'seq', seq, 'id', id, 'account_id', account_id,
       'space_id', COALESCE(space_id, ''), 'target_type', target_type,
       'target_ref', COALESCE(target_ref, ''), 'created_at', created_at
     )::text
     FROM platform_tombstones WHERE seq > ${last_seq} ORDER BY seq" >"$LEDGER_ROWS"
  new_seq="$("$PYTHON" "$LEDGER_HELPER" append --path "$LEDGER_FILE" \
    --deployment-id "$ONEBRAIN_DEPLOYMENT_ID" <"$LEDGER_ROWS")"
  rm -f -- "$LEDGER_ROWS"
  LEDGER_ROWS=""
  [[ "$new_seq" =~ ^[0-9]+$ ]] || die "external erasure-ledger append failed"
  printf '%s\n' "$new_seq"
}

preflight_backup_space() {
  local drive_bytes database_bytes free_bytes required_bytes
  drive_bytes="$(du -sb "$DRIVE_DIR" | awk '{print $1}')"
  database_bytes="$(dc exec -T postgres psql -U onebrain -d onebrain -Atqc \
    "SELECT pg_database_size(current_database())" | tr -d '[:space:]')"
  [[ "$database_bytes" =~ ^[0-9]+$ ]] || die "database size preflight failed"
  free_bytes="$(df -PB1 "$ONEBRAIN_DRIVE_BACKUP_DIR" | awk 'NR == 2 {print $4}')"
  required_bytes=$((drive_bytes + database_bytes + 268435456))
  [ "$free_bytes" -ge "$required_bytes" ] \
    || die "insufficient local space for a Drive backup"
}

do_backup() {
  local stamp partial archive archive_name ledger_seq
  require_foundation
  require_crypto
  preflight_backup_space
  WORK="$(mktemp -d "$ONEBRAIN_DRIVE_BACKUP_DIR/.work.XXXXXX")"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive_name="onebrain-drive-${stamp}.obk"
  partial="${ONEBRAIN_DRIVE_BACKUP_DIR}/${archive_name}.partial"
  archive="${ONEBRAIN_DRIVE_BACKUP_DIR}/${archive_name}"
  PARTIAL_ARCHIVE="$partial"

  quiesce_application
  ledger_seq="$(sync_erasure_ledger)"
  dc exec -T postgres pg_dump -U onebrain -Fc -d onebrain >"$WORK/onebrain.dump"
  printf 'format=onebrain-drive-backup-v2\ndeployment_id=%s\nerasure_ledger_seq=%s\ncreated_at=%s\n' \
    "$ONEBRAIN_DEPLOYMENT_ID" "$ledger_seq" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$WORK/backup.meta"
  (cd "$WORK" && "$SHA256SUM" onebrain.dump >manifest.sha256)
  (cd "$ONEBRAIN_DATA_MOUNT" \
    && find drive -type f -print0 | sort -z | xargs -0 -r "$SHA256SUM") \
    >>"$WORK/manifest.sha256"

  "$TAR" -C "$WORK" -cf - onebrain.dump backup.meta manifest.sha256 \
    -C "$ONEBRAIN_DATA_MOUNT" drive \
    | "$PYTHON" "$CRYPTO_HELPER" encrypt --output "$partial"
  chmod 0600 "$partial"
  mv "$partial" "$archive"
  PARTIAL_ARCHIVE=""

  resume_application
  safe_remove_work "$WORK"
  WORK=""
  find "$ONEBRAIN_DRIVE_BACKUP_DIR" -maxdepth 1 -type f \
    -name 'onebrain-drive-*.obk' \
    -mtime "+$ONEBRAIN_DRIVE_BACKUP_RETENTION_DAYS" -delete
  printf '%s\n' "$archive"
}

validate_archive_entries() {
  local archive="$1" entry listing
  listing="$(crypto_decrypt "$archive" | "$TAR" -tf -)" \
    || die "backup archive cannot be decrypted or listed"
  while IFS= read -r entry; do
    case "$entry" in
      /*|../*|*/../*|*/..|*\\*) die "backup contains an unsafe archive path" ;;
    esac
    case "$entry" in
      onebrain.dump|backup.meta|manifest.sha256|drive|drive/|drive/*) ;;
      *) die "backup contains an unexpected archive path" ;;
    esac
  done <<<"$listing"
}

do_restore() {
  local archive="${1:-}" stamp old_drive archive_bytes free_bytes backup_seq current_seq backup_deployment
  [ -n "$archive" ] || die "restore requires an encrypted archive path"
  [ -f "$archive" ] || die "restore archive does not exist"
  require_foundation
  require_crypto

  archive="$(readlink -f "$archive")"
  # crypto_decrypt performs a full MAC pass with a constant-time comparison
  # before it creates a decryptor or emits plaintext.
  validate_archive_entries "$archive"

  archive_bytes="$(stat -c '%s' "$archive")"
  free_bytes="$(df -PB1 "$ONEBRAIN_DATA_MOUNT" | awk 'NR == 2 {print $4}')"
  [ "$free_bytes" -ge $((archive_bytes + 268435456)) ] \
    || die "insufficient attached-volume space to stage this restore"

  stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  RESTORE_STAGE="${ONEBRAIN_DATA_MOUNT}/.drive-restore.${stamp}"
  install -d -o root -g root -m 0700 "$RESTORE_STAGE"
  crypto_decrypt "$archive" | "$TAR" -xf - -C "$RESTORE_STAGE"
  if find "$RESTORE_STAGE" -type l -o \( ! -type d ! -type f \) | grep -q .; then
    die "backup contains unsupported filesystem entries"
  fi
  grep -Fxq 'format=onebrain-drive-backup-v2' "$RESTORE_STAGE/backup.meta" \
    || die "unsupported Drive backup format"
  backup_deployment="$(sed -n 's/^deployment_id=//p' "$RESTORE_STAGE/backup.meta")"
  [ "$backup_deployment" = "$ONEBRAIN_DEPLOYMENT_ID" ] \
    || die "backup belongs to a different deployment"
  backup_seq="$(sed -n 's/^erasure_ledger_seq=//p' "$RESTORE_STAGE/backup.meta")"
  [[ "$backup_seq" =~ ^[0-9]+$ ]] || die "backup erasure-ledger cursor is invalid"
  (cd "$RESTORE_STAGE" && "$SHA256SUM" -c manifest.sha256)

  quiesce_application
  # Capture every tombstone committed before the quiesce, then require exact
  # equality with the snapshot boundary. Any post-backup erasure makes this old
  # snapshot ineligible rather than allowing deleted data to reappear.
  current_seq="$(sync_erasure_ledger)"
  if [ "$current_seq" -gt "$backup_seq" ]; then
    die "restore refused: post-backup erasures exist in the external ledger"
  fi
  [ "$current_seq" -eq "$backup_seq" ] \
    || die "restore refused: external erasure ledger is behind the backup boundary"
  old_drive="${ONEBRAIN_DATA_MOUNT}/.drive-before-restore.${stamp}"
  mv "$DRIVE_DIR" "$old_drive"
  RESTORE_OLD_DRIVE="$old_drive"
  if ! mv "$RESTORE_STAGE/drive" "$DRIVE_DIR"; then
    die "Drive directory swap failed; the previous Drive will be restored"
  fi
  chown -R 10001:10001 "$DRIVE_DIR"
  chmod 0750 "$DRIVE_DIR"

  if ! dc exec -T postgres pg_restore -U onebrain --clean --if-exists \
      --exit-on-error --single-transaction -d onebrain <"$RESTORE_STAGE/onebrain.dump"; then
    die "database restore failed; the previous Drive directory was restored"
  fi

  # The restored database and Drive now form the committed pair. Do not roll the
  # Drive back if cleaning the obsolete directory alone encounters an error.
  RESTORE_OLD_DRIVE=""
  case "$old_drive" in
    "$ONEBRAIN_DATA_MOUNT"/.drive-before-restore.*) rm -rf -- "$old_drive" ;;
    *) die "refusing to remove unexpected pre-restore directory" ;;
  esac
  resume_application
  safe_remove_work "$RESTORE_STAGE"
  RESTORE_STAGE=""
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  die "another Drive backup or restore is running"
fi
trap cleanup EXIT
mkdir -p "$(dirname "$UPDATE_LOCK")"
if ! mkdir "$UPDATE_LOCK" 2>/dev/null; then
  die "the box update agent is active; retrying later"
fi
UPDATE_LOCK_ACQUIRED=true

case "${1:-backup}" in
  backup) do_backup ;;
  restore) do_restore "${2:-}" ;;
  ledger-sync) require_foundation; sync_erasure_ledger >/dev/null ;;
  *) die "usage: $0 [backup|restore <authenticated-archive>|ledger-sync]" ;;
esac
