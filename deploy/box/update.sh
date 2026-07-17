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
# .env (the exchanged secret bundle, P5-03) is sourced FIRST so box.env's ${VAR} refs
# (ONEBRAIN_FLEET_KEY, UPDATE_BACKUP_KEY, UPDATE_DESIRED_STATE_PUBLIC_KEYS, ...) re-
# expand to the delivered real values. Absent on a not-yet-exchanged box -> the refs
# stay empty and the run holds harmlessly (the fetch below fails auth, non-destructive).
# shellcheck disable=SC1090
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi
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
# 7c / G2-2: the custom-format dump (-Fc) and pg_restore MUST resolve to the SAME
# database, or a restore on a failed migration-crossing update could load the dump into
# a different/empty DB than was dumped. Both connect through this ONE shared target
# (overridable in box.env alongside the PG_DUMP/PG_RESTORE exec wrappers a live box may
# point these at) — the dump is never left implicit while the restore is explicit.
: "${PG_CONN:=postgresql://onebrain@postgres:5432/onebrain}"
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

# Preserve the non-root app identity across image upgrades; dry-run harnesses are not root.
[ "$EUID" -ne 0 ] || { chown -Rh 10001:10001 "$UPDATE_DATA_DIR"; chmod 750 "$UPDATE_DATA_DIR"; }

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

BACKUP_STATUS=""
BACKUP_TS=""
BACKUP_MANIFEST=""   # 7d/A17: sha256:<hex>:<bytes> of $ENC; "" until a backup is taken
PRE_MIGRATION_REV=""
ENC=""   # encrypted backup path (set in step 4); recover decrypts THIS, never the shredded plaintext

# --- 4. QUIESCE + BACKUP before a schema change -----------------------------
crosses_migration() { [ "$MIG_FROM" != "$MIG_TO" ] || [ "$ROLLBACK_KIND" = "restore_required" ]; }
if crosses_migration; then
  log "schema change -> quiesce + backup"
  PRE_MIGRATION_REV="$(alembic_current 2>/dev/null | tr -d '[:space:]' || true)"
  quiesce_application_services >>"$LOG" 2>&1 || true
  printf '%s\n' "$PRE_MIGRATION_REV" >"$WORK/pre_migration_revision"
  DUMP="$WORK/backup.dump"
  ENC="${UPDATE_DATA_DIR}/backups/backup-$(date -u +%Y%m%dT%H%M%SZ).dump.enc"
  mkdir -p "${UPDATE_DATA_DIR}/backups"
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
  else
    BACKUP_STATUS="failed"
    log "backup FAILED; restoring current stack and holding (no destructive apply)"
    resume_current_stack >>"$LOG" 2>&1 || log "current stack restore after backup failure warned"
    write_state "failed" "$PRE_MIGRATION_REV" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
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
  CURRENT="$(alembic_current 2>/dev/null | tr -d '[:space:]' || true)"
  MIGRATION_REACHED="$CURRENT"
  if [ "$CURRENT" != "$MIG_TO" ]; then
    log "fence FAILED: alembic current=$CURRENT != target=$MIG_TO; holding DEGRADED"
    write_state "failed" "$CURRENT" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
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
  # $WORK/backup.dump (that path no longer exists).
  RESTORE="$WORK/restore.dump"
  if "$OPENSSL" enc -d -aes-256-cbc -pbkdf2 -pass "pass:${UPDATE_BACKUP_KEY:-}" \
       -in "$ENC" -out "$RESTORE" 2>>"$LOG"; then
    restore_onebrain_db "$RESTORE" >>"$LOG" 2>&1 || log "pg_restore warned"
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
  write_state "rolled_back" "$MIGRATION_REACHED" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
  exit 0
fi

# --- 8/9. SUCCESS: record floor+nonce, persist last-applied + UpdateReport ---
"$PYTHON" "$UPDATE_VERIFY_BIN" record-apply <"$ENVELOPE" >>"$LOG" 2>&1 || log "record-apply warned"
cp -f "$TARGET" "$LAST_APPLIED"
write_state "succeeded" "$MIGRATION_REACHED" "$BACKUP_STATUS" "$BACKUP_TS" "$BACKUP_MANIFEST"
log "update SUCCEEDED -> $TARGET_VERSION"
exit 0
