#!/usr/bin/env bash
# Prepare and verify OneBrain's attached persistent-data volume.
#
# The UUID recorded on first boot is authoritative thereafter. Docker is ordered
# behind the verifier so a missing or different filesystem cannot make Postgres
# initialize on the root disk after a reboot.
set -euo pipefail

DATA_MOUNT="${ONEBRAIN_DATA_MOUNT:-/mnt/onebrain-data}"
UUID_FILE="${ONEBRAIN_DATA_UUID_FILE:-/etc/onebrain-data-volume.uuid}"
DRIVE_MARKER="${ONEBRAIN_DRIVE_MARKER:-/etc/onebrain-drive-enabled}"
DRIVE_DIR="${DATA_MOUNT}/drive"
LEDGER_INIT_MARKER="${ONEBRAIN_ERASURE_LEDGER_INIT_MARKER:-${DATA_MOUNT}/.onebrain-erasure-ledger-uninitialized}"

die() {
  printf 'OneBrain data volume: %s\n' "$*" >&2
  exit 1
}

mounted_uuid() {
  local source
  source="$(findmnt -nr -o SOURCE --target "$DATA_MOUNT")" || return 1
  blkid -s UUID -o value "$source"
}

fstab_entry_matches_volume() {
  # Existing boxes can legitimately have different mount options (for example
  # ``defaults,nofail`` from an earlier bootstrap). The filesystem identity,
  # mount point, and type are the safety boundary; options do not identify a
  # different disk and are therefore intentionally not rewritten during setup.
  local line="$1" expected_uuid="$2" source target fstype
  read -r source target fstype _ <<<"$line"
  [ "$source" = "UUID=$expected_uuid" ] \
    && [ "$target" = "$DATA_MOUNT" ] \
    && [ "$fstype" = "ext4" ]
}

verify_volume() {
  [ -s "$UUID_FILE" ] || die "expected UUID marker is missing"
  mountpoint -q "$DATA_MOUNT" || die "$DATA_MOUNT is not a mount point"

  local expected actual
  expected="$(tr -d '[:space:]' <"$UUID_FILE")"
  [ -n "$expected" ] || die "expected UUID marker is empty"
  actual="$(mounted_uuid)" || die "mounted filesystem UUID cannot be read"
  [ "$actual" = "$expected" ] || die "mounted filesystem UUID does not match the provisioned volume"

  if [ -e "$DRIVE_MARKER" ]; then
    install -d -o 10001 -g 10001 -m 0750 "$DRIVE_DIR"
    [ "$(stat -c '%u:%g:%a' "$DRIVE_DIR")" = "10001:10001:750" ] \
      || die "Drive directory ownership or mode is unsafe"
  fi
}

setup_volume() {
  local candidates=() dev fstype uuid fstab_line created_filesystem=false
  local current_entries=()
  shopt -s nullglob
  for dev in /dev/disk/by-id/scsi-0HC_Volume_*; do
    [ -b "$dev" ] && candidates+=("$dev")
  done
  shopt -u nullglob

  [ "${#candidates[@]}" -eq 1 ] \
    || die "expected exactly one attached Hetzner data volume, found ${#candidates[@]}"
  dev="${candidates[0]}"

  fstype="$(blkid -s TYPE -o value "$dev" 2>/dev/null || true)"
  if [ -z "$fstype" ]; then
    # A recorded UUID means this is an established deployment. Never format a
    # volume in that state: a lost filesystem signature is a recovery incident.
    [ ! -e "$UUID_FILE" ] || die "recorded volume lost its filesystem signature"
    mkfs.ext4 -F "$dev"
    fstype="ext4"
    created_filesystem=true
  fi
  [ "$fstype" = "ext4" ] || die "attached volume must use ext4, found $fstype"

  uuid="$(blkid -s UUID -o value "$dev")"
  [ -n "$uuid" ] || die "attached volume has no UUID"
  if [ -e "$UUID_FILE" ]; then
    [ "$(tr -d '[:space:]' <"$UUID_FILE")" = "$uuid" ] \
      || die "attached volume UUID differs from the provisioned UUID"
  else
    install -d -o root -g root -m 0755 "$(dirname "$UUID_FILE")"
    printf '%s\n' "$uuid" >"$UUID_FILE"
    chmod 0600 "$UUID_FILE"
  fi

  install -d -o root -g root -m 0755 "$DATA_MOUNT"
  fstab_line="UUID=$uuid $DATA_MOUNT ext4 defaults,x-systemd.device-timeout=90s 0 2"
  mapfile -t current_entries < <(
    awk -v target="$DATA_MOUNT" '$1 !~ /^#/ && $2 == target {print}' /etc/fstab
  )
  if [ "${#current_entries[@]}" -gt 1 ]; then
    die "multiple fstab entries already own $DATA_MOUNT"
  elif [ "${#current_entries[@]}" -eq 1 ]; then
    fstab_entry_matches_volume "${current_entries[0]}" "$uuid" \
      || die "an incompatible fstab entry already owns $DATA_MOUNT"
  else
    printf '%s\n' "$fstab_line" >>/etc/fstab
  fi

  mountpoint -q "$DATA_MOUNT" || mount "$DATA_MOUNT"
  # The erasure ledger sits outside the backed-up Drive subtree. Its one-time
  # initialization authority is created only when this script itself formats a
  # genuinely new filesystem. A rebuilt host attached to an existing volume
  # therefore cannot silently replace a missing ledger with an empty one.
  if [ "$created_filesystem" = true ] && [ -e "$DRIVE_MARKER" ]; then
    printf '%s\n' 'initialize-once' >"$LEDGER_INIT_MARKER"
    chown root:root "$LEDGER_INIT_MARKER"
    chmod 0600 "$LEDGER_INIT_MARKER"
  fi
  verify_volume
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-verify}" in
    setup) setup_volume ;;
    verify) verify_volume ;;
    *) die "usage: $0 [setup|verify]" ;;
  esac
fi
