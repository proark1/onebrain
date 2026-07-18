"""Static customer-box contracts for Drive volume persistence and local backups.

The scripts execute on Ubuntu customer boxes, while this test suite is also run
on Windows. These tests pin the security/ordering contract without pretending a
Windows host can execute mount, systemd, Docker, or pg_restore.
"""

from pathlib import Path


BOX = Path(__file__).parents[1] / "deploy" / "box"


def _read(name: str) -> str:
    return (BOX / name).read_text(encoding="utf-8")


def test_volume_setup_is_uuid_pinned_and_fails_closed():
    script = _read("onebrain-data-volume.sh")
    unit = _read("onebrain-data-volume.service")

    assert "expected exactly one attached Hetzner data volume" in script
    assert 'fstype="$(blkid -s TYPE' in script
    assert '[ ! -e "$UUID_FILE" ]' in script
    assert "mkfs.ext4 -F" in script
    assert "UUID=$uuid $DATA_MOUNT ext4 defaults,x-systemd.device-timeout=90s" in script
    assert 'mountpoint -q "$DATA_MOUNT"' in script
    assert 'actual="$(mounted_uuid)"' in script
    assert '[ "$actual" = "$expected" ]' in script

    assert "RequiresMountsFor=/mnt/onebrain-data" in unit
    assert "Before=docker.service" in unit
    assert "ExecStart=/opt/onebrain/onebrain-data-volume.sh verify" in unit
    assert "RequiredBy=docker.service" in unit


def test_backup_uses_safe_env_loading_and_an_atomic_authenticated_archive():
    script = _read("onebrain-drive-backup.sh")

    safe_load = script.index('onebrain_load_dotenv "$ENV_FILE"')
    trusted_box_env = script.index('. "$BOX_ENV"')
    assert safe_load < trusted_box_env
    assert "onebrain_backup_crypto.py" in script
    assert 'encrypt --output "$partial"' in script
    assert "aes-256-cbc" not in script
    assert "openssl" not in script.lower()
    assert ".sha256.partial" not in script
    assert "restore checksum sidecar" not in script

    quiesce = script.index("quiesce_application", script.index("do_backup()"))
    ledger = script.index('ledger_seq="$(sync_erasure_ledger)"', quiesce)
    dump = script.index("pg_dump -U onebrain -Fc -d onebrain", ledger)
    archive = script.index('-C "$ONEBRAIN_DATA_MOUNT" drive', dump)
    publish = script.index('mv "$partial" "$archive"', archive)
    assert quiesce < ledger < dump < archive < publish
    assert "onebrain.dump backup.meta manifest.sha256" in script
    assert 'find drive -type f -print0' in script
    assert "ONEBRAIN_DRIVE_BACKUP_RETENTION_DAYS:=7" in script
    assert "SELECT pg_database_size(current_database())" in script
    assert 'PARTIAL_ARCHIVE="$partial"' in script
    assert 'archive_name="onebrain-drive-${stamp}.obk"' in script


def test_backup_and_update_agents_share_an_atomic_maintenance_lock():
    script = _read("onebrain-drive-backup.sh")
    update = _read("update.sh")

    assert 'UPDATE_LOCK="${UPDATE_DATA_DIR}/onebrain_update/update.lock"' in script
    assert 'mkdir "$UPDATE_LOCK"' in script
    assert 'LOCK="${WORK}/update.lock"' in update
    assert 'mkdir "$LOCK"' in update
    assert "Restart=on-failure" in _read("onebrain-drive-backup.service")
    assert "RestartSec=15min" in _read("onebrain-drive-backup.service")


def test_restore_verifies_before_swap_and_is_transactional():
    script = _read("onebrain-drive-backup.sh")
    restore = script[script.index("do_restore() {"):]

    paths = restore.index("validate_archive_entries")
    content = restore.index('"$SHA256SUM" -c manifest.sha256')
    quiesce = restore.index("quiesce_application")
    ledger = restore.index('current_seq="$(sync_erasure_ledger)"')
    refusal = restore.index("post-backup erasures exist")
    swap = restore.index('mv "$DRIVE_DIR" "$old_drive"')
    database = restore.index("pg_restore -U onebrain")
    assert paths < content < quiesce < ledger < refusal < swap < database

    assert "--exit-on-error --single-transaction" in restore
    assert 'RESTORE_OLD_DRIVE="$old_drive"' in restore
    assert 'mv "$RESTORE_OLD_DRIVE" "$DRIVE_DIR"' in script
    assert "backup contains an unexpected archive path" in script
    assert "backup contains an unsafe archive path" in script
    assert "backup contains unsupported filesystem entries" in script
    assert "insufficient attached-volume space" in script
    assert 'format=onebrain-drive-backup-v2' in script
    assert 'erasure_ledger_seq=%s' in script
    assert '[ "$current_seq" -eq "$backup_seq" ]' in restore


def test_external_erasure_ledger_is_append_only_authenticated_and_outside_snapshot():
    script = _read("onebrain-drive-backup.sh")
    helper = _read("onebrain_erasure_ledger.py")

    assert "ONEBRAIN_ERASURE_LEDGER_DIR:=${ONEBRAIN_DATA_MOUNT}/.onebrain-erasure-ledger" in script
    assert "ONEBRAIN_DRIVE_BACKUP_DIR:=/var/lib/onebrain/drive-backups" in script
    assert '${ONEBRAIN_DATA_MOUNT}/.onebrain-erasure-ledger-uninitialized' in script
    volume_script = (BOX / "onebrain-data-volume.sh").read_text(encoding="utf-8")
    assert 'created_filesystem=true' in volume_script
    assert 'if [ "$created_filesystem" = true ]' in volume_script
    assert "refusing to initialize implicitly" in script
    assert "FROM platform_tombstones WHERE seq > ${last_seq} ORDER BY seq" in script
    assert '"$LEDGER_HELPER" append' in script
    assert "ledger.ndjson" not in script[script.index('"$TAR" -C "$WORK"'):script.index("chmod 0600")]

    assert "os.O_WRONLY | os.O_APPEND" in helper
    assert "os.fsync(descriptor)" in helper
    assert "hmac.compare_digest" in helper
    assert "prev" in helper and "ZERO_MAC" in helper
    assert "reason" not in helper.split("RECORD_FIELDS", 1)[1].split("}", 1)[0]


def test_backup_timer_is_daily_randomized_and_persistent():
    timer = _read("onebrain-drive-backup.timer")
    assert "OnCalendar=*-*-* 02:30:00 UTC" in timer
    assert "RandomizedDelaySec=30m" in timer
    assert "Persistent=true" in timer

    ledger_timer = _read("onebrain-drive-erasure-ledger.timer")
    assert "OnUnitInactiveSec=1min" in ledger_timer
    assert "Persistent=true" in ledger_timer


def test_host_service_executes_the_digest_pinned_api_images_backup_engine():
    service = _read("onebrain-drive-backup.service")
    assert "{{COMPOSE_PROJECT}}" in service
    assert "onebrain-api:/app/deploy/box/onebrain-drive-backup.sh" in service
    assert "onebrain_backup_crypto.py" in service
    assert "onebrain_erasure_ledger.py" in service
    assert "DOTENV_LOADER=/opt/onebrain/onebrain_dotenv.sh" in service
    assert "ExecStopPost=-/usr/bin/rm -f /run/onebrain-drive-backup-job.sh" in service

    ledger_service = _read("onebrain-drive-erasure-ledger.service")
    assert "onebrain-drive-ledger-sync.sh ledger-sync" in ledger_service
    assert "onebrain_erasure_ledger.py" in ledger_service
