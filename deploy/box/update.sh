#!/usr/bin/env bash
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

BOX_ENV="${BOX_ENV:-/opt/onebrain/box.env}"
# shellcheck disable=SC1090
if [ -f "$BOX_ENV" ]; then
  set -a
  . "$BOX_ENV"
  set +a
fi

# Command indirections: bare defaults for the dry-run harness; a real box may
# point these at `docker compose exec ...` wrappers via box.env.
: "${DOCKER:=docker}"
: "${CURL:=curl}"
: "${ALEMBIC:=alembic}"
: "${PG_DUMP:=pg_dump}"
: "${PG_RESTORE:=pg_restore}"
: "${OPENSSL:=openssl}"
: "${PYTHON:=python3}"

: "${UPDATE_DATA_DIR:=/data}"
: "${UPDATE_COMPOSE_DIR:=/opt/onebrain}"
: "${UPDATE_COMPOSE_PROJECT:=onebrain}"
: "${UPDATE_PROFILES:=onebrain}"
: "${UPDATE_LOCAL_MODULES:=}"
: "${UPDATE_HEALTH_URL:=http://127.0.0.1/health}"
: "${UPDATE_VERIFY_BIN:=${UPDATE_COMPOSE_DIR}/onebrain_box_verify.py}"

WORK="${UPDATE_DATA_DIR}/onebrain_update"
COMPOSE="${UPDATE_COMPOSE_DIR}/docker-compose.yml"
OVERRIDE="${UPDATE_COMPOSE_DIR}/images.override.yml"
OVERRIDE_PREV="${UPDATE_COMPOSE_DIR}/images.override.prev.yml"
STATE_FILE="${WORK}/update_state.json"
LAST_APPLIED="${WORK}/last_applied.json"
LOG="${WORK}/update.log"
LOCK="${WORK}/update.lock"

mkdir -p "$WORK"

# Singleton: mkdir is atomic; a second run exits cleanly (A2 — no flock).
if ! mkdir "$LOCK" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG" 2>/dev/null || true; }

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

ATTEMPT_ID=""
TARGET_VERSION=""

# write_state <outcome> <migration_reached> <backup_status> <backup_ts>
# Writes the metadata-only UpdateReport (NO free-text reason — that stays in $LOG,
# off the fleet edge; ground rule 3).
write_state() {
  "$PYTHON" - "$STATE_FILE" "$1" "$2" "$TARGET_VERSION" "$ATTEMPT_ID" "$3" "$4" <<'PYEOF'
import sys, json, datetime
path, outcome, migration_reached, version, attempt_id, backup_status, backup_ts = sys.argv[1:8]
json.dump({
    "last_target_version": version,
    "outcome": outcome,
    "migration_reached": migration_reached,
    "attempt_id": attempt_id,
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "backup_status": backup_status,
    "backup_ts": backup_ts,
}, open(path, "w"))
PYEOF
}

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
  write_state "failed" "" "" ""
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

# Select this box's images from the verified target (H-5: the release pins ALL
# module images fleet-wide; a box pulls only its enabled modules). Writes the
# pin override and prints the refs to pull — A7: derived ONLY from verifier stdout.
select_images() {
  UPDATE_LOCAL_MODULES="$UPDATE_LOCAL_MODULES" "$PYTHON" - "$TARGET" "$OVERRIDE" <<'PYEOF'
import sys, json, os
target = json.load(open(sys.argv[1]))
override_path = sys.argv[2]
images = target.get("images", {})
local = [m.strip() for m in os.environ.get("UPDATE_LOCAL_MODULES", "").split(",") if m.strip()]
selected = {m: images[m] for m in local if m in images} if local else dict(images)
lines = ["services:"]
for m, ref in sorted(selected.items()):
    lines.append("  %s:" % m)
    lines.append("    image: %s" % ref)
with open(override_path, "w") as fh:
    fh.write("\n".join(lines) + "\n")
for _, ref in sorted(selected.items()):
    print(ref)
PYEOF
}

BACKUP_STATUS=""
BACKUP_TS=""
PRE_MIGRATION_REV=""
ENC=""   # encrypted backup path (set in step 4); recover decrypts THIS, never the shredded plaintext

# --- 4. QUIESCE + BACKUP before a schema change -----------------------------
crosses_migration() { [ "$MIG_FROM" != "$MIG_TO" ] || [ "$ROLLBACK_KIND" = "restore_required" ]; }
if crosses_migration; then
  log "schema change -> quiesce + backup"
  dc_over stop >>"$LOG" 2>&1 || true
  PRE_MIGRATION_REV="$("$ALEMBIC" current 2>/dev/null | tr -d '[:space:]' || true)"
  printf '%s\n' "$PRE_MIGRATION_REV" >"$WORK/pre_migration_revision"
  DUMP="$WORK/backup.sql"
  ENC="${UPDATE_DATA_DIR}/backups/backup-$(date -u +%Y%m%dT%H%M%SZ).sql.enc"
  mkdir -p "${UPDATE_DATA_DIR}/backups"
  if "$PG_DUMP" >"$DUMP" 2>>"$LOG" \
     && "$OPENSSL" enc -aes-256-cbc -pbkdf2 -salt -pass "pass:${UPDATE_BACKUP_KEY:-}" -in "$DUMP" -out "$ENC" 2>>"$LOG"; then
    BACKUP_STATUS="success"
    BACKUP_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    rm -f "$DUMP"
    log "backup ok -> $ENC"
  else
    BACKUP_STATUS="failed"
    log "backup FAILED; holding (no destructive apply)"
    write_state "failed" "$PRE_MIGRATION_REV" "$BACKUP_STATUS" "$BACKUP_TS"
    exit 0
  fi
fi

# --- 5. PULL + UP by digest (verifier map ONLY, A7) -------------------------
[ -f "$OVERRIDE" ] && cp -f "$OVERRIDE" "$OVERRIDE_PREV"
log "pull + up (verified digests)"
REFS="$(select_images)"
for ref in $REFS; do
  "$DOCKER" pull "$ref" >>"$LOG" 2>&1
done
dc_over "${PROFILE_ARGS[@]}" pull >>"$LOG" 2>&1 || true
dc_over "${PROFILE_ARGS[@]}" up -d >>"$LOG" 2>&1

# --- 6. FENCE, don't flap ---------------------------------------------------
MIGRATION_REACHED="$MIG_TO"
if [ "$MIG_FROM" != "$MIG_TO" ]; then
  CURRENT="$("$ALEMBIC" current 2>/dev/null | tr -d '[:space:]' || true)"
  MIGRATION_REACHED="$CURRENT"
  if [ "$CURRENT" != "$MIG_TO" ]; then
    log "fence FAILED: alembic current=$CURRENT != target=$MIG_TO; holding DEGRADED"
    write_state "failed" "$CURRENT" "$BACKUP_STATUS" "$BACKUP_TS"
    exit 0
  fi
fi

# --- 7. SMOKE + recover by rollback_kind ------------------------------------
recover_code_only() {
  log "recover code_only: re-up previous digest set"
  if [ -f "$OVERRIDE_PREV" ]; then cp -f "$OVERRIDE_PREV" "$OVERRIDE"; else rm -f "$OVERRIDE"; fi
  dc_over "${PROFILE_ARGS[@]}" up -d >>"$LOG" 2>&1 || true
}
recover_restore_required() {
  log "recover restore_required: decrypt backup + pg_restore at $PRE_MIGRATION_REV then revert digests"
  # The plaintext dump was shredded right after encryption (step 4), so the ONLY
  # surviving copy is the encrypted $ENC. Decrypt it with the SAME cipher+key used to
  # encrypt (openssl enc -d) into a temp plaintext, restore from THAT, then shred the
  # temp so customer data never lingers on disk. NEVER pg_restore the deleted
  # $WORK/backup.sql (that path no longer exists).
  RESTORE="$WORK/restore.sql"
  if "$OPENSSL" enc -d -aes-256-cbc -pbkdf2 -pass "pass:${UPDATE_BACKUP_KEY:-}" \
       -in "$ENC" -out "$RESTORE" 2>>"$LOG"; then
    "$PG_RESTORE" "$RESTORE" >>"$LOG" 2>&1 || log "pg_restore warned"
    shred -u "$RESTORE" 2>/dev/null || rm -f "$RESTORE"
  else
    log "backup decrypt FAILED; cannot restore DB"
  fi
  recover_code_only
}
if ! "$CURL" -sf "$UPDATE_HEALTH_URL" >/dev/null 2>>"$LOG"; then
  log "smoke FAILED (rollback_kind=$ROLLBACK_KIND)"
  if [ "$ROLLBACK_KIND" = "restore_required" ]; then
    recover_restore_required
  else
    recover_code_only
  fi
  write_state "rolled_back" "$MIGRATION_REACHED" "$BACKUP_STATUS" "$BACKUP_TS"
  exit 0
fi

# --- 8/9. SUCCESS: record floor+nonce, persist last-applied + UpdateReport ---
"$PYTHON" "$UPDATE_VERIFY_BIN" record-apply <"$ENVELOPE" >>"$LOG" 2>&1 || log "record-apply warned"
cp -f "$TARGET" "$LAST_APPLIED"
write_state "succeeded" "$MIGRATION_REACHED" "$BACKUP_STATUS" "$BACKUP_TS"
log "update SUCCEEDED -> $TARGET_VERSION"
exit 0
