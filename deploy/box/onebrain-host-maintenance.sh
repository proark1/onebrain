#!/usr/bin/env bash
# OneBrain host maintenance: retain rollback images while recovering disk space.
#
# This program is deliberately independent from the updater. It first builds a
# deletion plan, then removes only old image IDs that are not referenced by a
# running/stopped container, either image override, the last verified apply, or
# the provision-time compose file. A missing or malformed protected-state file
# therefore fails closed for image cleanup rather than risking a rollback image.
set -euo pipefail

: "${DOCKER:=docker}"
: "${PYTHON:=python3}"
: "${DATE:=date}"
: "${ONEBRAIN_DATA_MOUNT:=/mnt/onebrain-data}"
: "${ONEBRAIN_MAINTENANCE_DIR:=${ONEBRAIN_DATA_MOUNT}/onebrain-maintenance}"
: "${ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT:=$(dirname "$0")/onebrain-data-volume.sh}"
: "${UPDATE_COMPOSE_DIR:=/opt/onebrain}"
: "${UPDATE_LOCK_STALE_SECONDS:=3600}"
: "${MAINTENANCE_IMAGE_MIN_AGE_HOURS:=168}"
: "${MAINTENANCE_BUILD_CACHE_MIN_AGE_HOURS:=${MAINTENANCE_IMAGE_MIN_AGE_HOURS}}"
: "${MAINTENANCE_DRY_RUN:=0}"

if [ "${1:-}" = "--dry-run" ]; then
  MAINTENANCE_DRY_RUN=1
elif [ -n "${1:-}" ]; then
  printf '%s\n' "usage: $0 [--dry-run]" >&2
  exit 2
fi

log() {
  printf '%s onebrain-host-maintenance: %s\n' "$("$DATE" -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

if ! command -v "$DOCKER" >/dev/null 2>&1 || ! command -v "$PYTHON" >/dev/null 2>&1; then
  log "docker or python unavailable; holding"
  exit 0
fi

if ! [[ "$MAINTENANCE_IMAGE_MIN_AGE_HOURS" =~ ^[0-9]+$ ]] || \
   ! [[ "$MAINTENANCE_BUILD_CACHE_MIN_AGE_HOURS" =~ ^[0-9]+$ ]] || \
   ! [[ "$UPDATE_LOCK_STALE_SECONDS" =~ ^[0-9]+$ ]]; then
  log "maintenance retention values must be non-negative integer hours; holding"
  exit 0
fi

MAINTENANCE_EXPECTED_DIR="${ONEBRAIN_DATA_MOUNT%/}/onebrain-maintenance"
UPDATE_STATE_DIR="${ONEBRAIN_MAINTENANCE_DIR}/onebrain_update"

# Maintenance reads rollback state only after the data-volume verifier proves
# the provisioned UUID-backed mount is present. Never fall back to a root-disk
# directory at the mountpoint when a volume disappears after reboot.
require_maintenance_dir() {
  local mount_real maintenance_real mount_source maintenance_source maintenance_target
  if [ "$ONEBRAIN_MAINTENANCE_DIR" != "$MAINTENANCE_EXPECTED_DIR" ]; then
    log "maintenance path is not the dedicated data-volume directory; holding"
    return 1
  fi
  if ! command -v mountpoint >/dev/null 2>&1 || ! command -v findmnt >/dev/null 2>&1 \
     || ! command -v readlink >/dev/null 2>&1; then
    log "mount verification tools unavailable; holding"
    return 1
  fi
  if ! mountpoint -q "$ONEBRAIN_DATA_MOUNT"; then
    log "persistent data volume is not mounted; holding"
    return 1
  fi
  if [ ! -x "$ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT" ] \
     || ! ONEBRAIN_DATA_MOUNT="$ONEBRAIN_DATA_MOUNT" "$ONEBRAIN_DATA_VOLUME_VERIFY_SCRIPT" verify >/dev/null 2>&1; then
    log "persistent data volume is unavailable or mismatched; holding"
    return 1
  fi
  if [ -L "$ONEBRAIN_MAINTENANCE_DIR" ]; then
    log "maintenance path must not be a symlink; holding"
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
    log "could not canonicalize maintenance directory; holding"
    return 1
  fi
  case "$maintenance_real" in
    "$mount_real"/*) ;;
    *)
      log "maintenance directory escaped the data volume; holding"
      return 1
      ;;
  esac
  if ! mount_source="$(findmnt -nr -o SOURCE --target "$mount_real" 2>/dev/null)" \
     || ! maintenance_source="$(findmnt -nr -o SOURCE --target "$maintenance_real" 2>/dev/null)" \
     || ! maintenance_target="$(findmnt -nr -o TARGET --target "$maintenance_real" 2>/dev/null)" \
     || [ -z "$mount_source" ] || [ "$maintenance_source" != "$mount_source" ] \
     || [ "$maintenance_target" != "$mount_real" ]; then
    log "maintenance directory is not on the verified data volume; holding"
    return 1
  fi
}

if ! require_maintenance_dir; then
  exit 0
fi

# Do not race an update while it swaps image overrides. A reboot/crash can leave
# the directory behind, so only a dead/invalid owner whose lock is old enough is
# reclaimed; a live or recent lock continues to hold maintenance safely.
UPDATE_LOCK="${UPDATE_STATE_DIR}/update.lock"

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

update_lock_is_stale() {
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

reclaim_stale_update_lock() {
  local stale_lock="${UPDATE_LOCK}.stale.$$"
  if ! mv "$UPDATE_LOCK" "$stale_lock" 2>/dev/null; then
    return 1
  fi
  if ! update_lock_is_stale "$stale_lock"; then
    [ -e "$UPDATE_LOCK" ] || mv "$stale_lock" "$UPDATE_LOCK" 2>/dev/null || true
    return 1
  fi
  if [ -d "$stale_lock" ]; then
    rm -f -- "${stale_lock}/pid" "${stale_lock}/started_at"
    rmdir "$stale_lock" 2>/dev/null || log "stale update lock moved aside for manual inspection"
  else
    rm -f -- "$stale_lock" || log "stale update lock moved aside for manual inspection"
  fi
  return 0
}

if [ -e "$UPDATE_LOCK" ]; then
  if update_lock_is_stale "$UPDATE_LOCK" && reclaim_stale_update_lock; then
    log "reclaimed stale update lock; continuing"
  else
    log "active or recent update lock present; holding"
    exit 0
  fi
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/onebrain-maintenance.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PROTECTED_REFS="$WORK/protected-refs"
CANDIDATES="$WORK/image-removal-plan"
CONTAINERS="$WORK/containers"
IMAGE_IDS="$WORK/image-ids"

# Emit every release image reference that must survive cleanup. The base compose
# is an additional conservative safeguard for a first boot before the updater
# has written a verified state. Override and last-applied parsing are strict:
# if an existing source cannot be trusted, image cleanup is skipped entirely.
if ! "$PYTHON" - \
  "${UPDATE_COMPOSE_DIR}/docker-compose.yml" \
  "${UPDATE_COMPOSE_DIR}/images.override.yml" \
  "${UPDATE_COMPOSE_DIR}/images.override.prev.yml" \
  "${UPDATE_STATE_DIR}/last_applied.json" >"$PROTECTED_REFS" <<'PYEOF'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


IMAGE_LINE = re.compile(r"^\s*image:\s*(?:(['\"])(.*?)\1|([^\s#]+))\s*(?:#.*)?$")


def compose_refs(path: Path, *, required: bool = False) -> list[str]:
    if not path.exists():
        if required:
            raise ValueError(f"missing compose image source: {path}")
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        raise ValueError(f"empty compose image source: {path}")
    refs: list[str] = []
    for line in text.splitlines():
        match = IMAGE_LINE.match(line)
        if match:
            ref = (match.group(2) or match.group(3) or "").strip()
            if not ref:
                raise ValueError(f"empty image reference in {path}")
            refs.append(ref)
    if not refs:
        raise ValueError(f"no image references in {path}")
    return refs


def applied_refs(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    images = payload.get("images") if isinstance(payload, dict) else None
    if not isinstance(images, dict) or not images:
        raise ValueError(f"last applied state has no image map: {path}")
    refs: list[str] = []
    for value in images.values():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"invalid image reference in {path}")
        refs.append(value.strip())
    return refs


compose, current, previous, applied = map(Path, sys.argv[1:])
refs = [*compose_refs(compose, required=True), *compose_refs(current), *compose_refs(previous), *applied_refs(applied)]
for ref in sorted(set(refs)):
    print(ref)
PYEOF
then
  log "protected image state unreadable; skipping image cleanup"
  PROTECTED_REFS=""
fi

if [ -s "$PROTECTED_REFS" ]; then
  declare -A protected=()

  # Resolve each protected digest/tag to the local immutable image ID. Missing
  # refs are harmless: no local image exists to delete.
  while IFS= read -r ref; do
    ref="${ref%$'\r'}"
    [ -n "$ref" ] || continue
    if image_id="$("$DOCKER" image inspect --format '{{.Id}}' "$ref" 2>/dev/null)"; then
      image_id="${image_id%$'\r'}"
      [ -n "$image_id" ] && protected["$image_id"]=1
    fi
  done <"$PROTECTED_REFS"

  # A Docker inventory failure means we cannot prove whether an image is
  # unreferenced. Fail closed rather than treating an unavailable daemon as an
  # empty inventory.
  if ! "$DOCKER" ps -aq >"$CONTAINERS" 2>/dev/null || \
     ! "$DOCKER" image ls -aq --no-trunc >"$IMAGE_IDS" 2>/dev/null; then
    log "Docker image inventory unavailable; skipping image cleanup"
    PROTECTED_REFS=""
  fi

  if [ -n "$PROTECTED_REFS" ]; then
    # Running images are required by the contract. Protect stopped-container
    # images as well: that is more conservative and lets a failed stack restart
    # without a repull. `docker image rm` is deliberately never forced.
    while IFS= read -r container_id; do
      container_id="${container_id%$'\r'}"
      [ -n "$container_id" ] || continue
      if image_id="$("$DOCKER" inspect --format '{{.Image}}' "$container_id" 2>/dev/null)"; then
        image_id="${image_id%$'\r'}"
        [ -n "$image_id" ] && protected["$image_id"]=1
      fi
    done <"$CONTAINERS"

    now_epoch="$("$DATE" +%s)"
    min_age_seconds=$((MAINTENANCE_IMAGE_MIN_AGE_HOURS * 3600))
    : >"$CANDIDATES"
    declare -A seen=()
    while IFS= read -r image_id; do
      image_id="${image_id%$'\r'}"
      [ -n "$image_id" ] || continue
      [ -n "${seen[$image_id]:-}" ] && continue
      seen["$image_id"]=1
      [ -n "${protected[$image_id]:-}" ] && continue
      if ! created="$("$DOCKER" image inspect --format '{{.Created}}' "$image_id" 2>/dev/null)"; then
        continue
      fi
      created="${created%$'\r'}"
      if ! created_epoch="$("$DATE" -d "$created" +%s 2>/dev/null)"; then
        log "cannot determine image age; keeping image"
        continue
      fi
      if [ $((now_epoch - created_epoch)) -ge "$min_age_seconds" ]; then
        printf '%s\n' "$image_id" >>"$CANDIDATES"
      fi
    done <"$IMAGE_IDS"

    planned="$(wc -l <"$CANDIDATES" | tr -d '[:space:]')"
    log "planned ${planned} unprotected image removals after ${MAINTENANCE_IMAGE_MIN_AGE_HOURS}h retention"

    if [ "$MAINTENANCE_DRY_RUN" = "1" ]; then
      log "dry-run requested; no cache or image cleanup performed"
      exit 0
    fi

    # The plan is complete before the first mutation. A non-forced removal can
    # only succeed for an image Docker itself considers unreferenced.
    while IFS= read -r image_id; do
      image_id="${image_id%$'\r'}"
      [ -n "$image_id" ] || continue
      if "$DOCKER" image rm "$image_id" >/dev/null 2>&1; then
        log "removed one unprotected image"
      else
        log "kept image Docker still references"
      fi
    done <"$CANDIDATES"
  fi
fi

# Build cache contains no deployable image reference. Keep recently used cache
# for fast rebuilds and never let a cache-prune failure affect the serving stack.
if [ "$MAINTENANCE_DRY_RUN" != "1" ]; then
  "$DOCKER" builder prune --all --force \
    --filter "until=${MAINTENANCE_BUILD_CACHE_MIN_AGE_HOURS}h" >/dev/null 2>&1 \
    || log "builder cache cleanup warned"
fi

log "complete"
