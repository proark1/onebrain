# Drive storage, backup, and restore

Drive is part of `onebrain_core` on every customer deployment. It is not a
Mission Control capability and is not mounted into Mission Control containers.

## Persistent storage contract

Customer provisioning identifies exactly one Hetzner attached volume, creates
an ext4 filesystem only when the volume is blank, records its filesystem UUID
in `/etc/fstab`, and mounts it at `/mnt/onebrain-data`. The expected UUID is
stored at `/etc/onebrain-data-volume.uuid`.

`onebrain-data-volume.service` is required by and ordered before Docker. It
fails when the mount is missing or its UUID differs, so Docker cannot start
customer containers over an empty root-disk directory. The service creates and
verifies `/mnt/onebrain-data/drive` as `10001:10001` with mode `0750`.

Only the customer `onebrain-api` and `onebrain-workers` containers receive:

```text
/mnt/onebrain-data/drive:/data/drive
```

## Scheduled local backup

`onebrain-drive-erasure-ledger.timer` exports new content-free
`platform_tombstones` every minute to
`/mnt/onebrain-data/.onebrain-erasure-ledger/ledger.ndjson`. This root-only,
persistent-volume path is outside the backed-up `drive/` subtree,
is not mounted into application containers, and is outside both the
database and Drive snapshots. It is append-only, chained, HMAC-authenticated,
fsynced per record, root-only, and bound to the deployment ID. A one-use marker
allows genesis creation during first provisioning; if the ledger later goes
missing, synchronization and restore fail rather than silently creating an
empty deletion history.

`onebrain-drive-backup.timer` runs daily at 02:30 UTC with up to 30 minutes of
random delay. Both services copy their engines from the running, digest-pinned
API image, ensuring host backup behavior matches the installed OneBrain
version.

The engine serializes with the box updater, verifies the attached-volume UUID,
checks local capacity, quiesces API/workers, and captures one consistency
boundary containing:

- a PostgreSQL custom-format dump of the `onebrain` database;
- all Drive originals and staging data under the Drive root;
- a format marker and content checksums.

The backup records the verified external-ledger sequence at its consistency
boundary. It then writes a streaming authenticated container (`.obk`) using
AES-256-CTR plus HMAC-SHA256 in encrypt-then-MAC order. A PBKDF2-derived master
key is separated into independent encryption and authentication keys using
domain-separated HMAC derivation. The deployment key comes from
`UPDATE_BACKUP_KEY` through the environment, never the process command line.
Restore completes a full HMAC pass and constant-time tag comparison before it
creates a decryptor or emits any plaintext. There is no unauthenticated checksum
sidecar.

Completed archives are mode `0600` under `/var/lib/onebrain/drive-backups`, a
root-only host path not mounted into application containers; incomplete
output is removed, and the default local retention is seven days.

Start an additional backup and inspect the timer with:

```sh
sudo systemctl start onebrain-drive-backup.service
sudo systemctl status onebrain-drive-backup.service
sudo systemctl list-timers onebrain-drive-backup.timer
sudo systemctl status onebrain-drive-erasure-ledger.service
sudo systemctl list-timers onebrain-drive-erasure-ledger.timer
sudo ls -l /var/lib/onebrain/drive-backups/
```

## Manual restore

A restore is deliberately an explicit root operation. It authenticates the
archive before plaintext, then validates archive paths, entry types, content
checksums, format, deployment identity, volume identity, and staging capacity.
After quiescing API/workers it performs one final tombstone export. The external
ledger sequence must exactly equal the backup's recorded sequence. If any GDPR
or permanent deletion happened after that backup, the restore is refused before
the database or Drive is changed. Operators must choose a backup created after
the deletion; deleted data is never replayed into service.

The Drive swap remains reversible until `pg_restore` commits with
`--single-transaction`; a database failure restores the previous Drive tree.

On the customer box:

```sh
sudo systemctl stop onebrain-drive-backup.timer

project="$(sudo awk -F= '$1 == "UPDATE_COMPOSE_PROJECT" { print $2 }' /opt/onebrain/box.env)"
sudo docker compose --project-name "$project" -f /opt/onebrain/docker-compose.yml \
  cp onebrain-api:/app/deploy/box/onebrain-drive-backup.sh /run/onebrain-drive-backup.sh
sudo docker compose --project-name "$project" -f /opt/onebrain/docker-compose.yml \
  cp onebrain-api:/app/deploy/box/onebrain_backup_crypto.py /run/onebrain_backup_crypto.py
sudo docker compose --project-name "$project" -f /opt/onebrain/docker-compose.yml \
  cp onebrain-api:/app/deploy/box/onebrain_erasure_ledger.py /run/onebrain_erasure_ledger.py
sudo chmod 0700 /run/onebrain-drive-backup.sh /run/onebrain_backup_crypto.py \
  /run/onebrain_erasure_ledger.py
sudo env DOTENV_LOADER=/opt/onebrain/onebrain_dotenv.sh \
  /run/onebrain-drive-backup.sh restore \
  /var/lib/onebrain/drive-backups/onebrain-drive-YYYYMMDDTHHMMSSZ.obk
sudo rm -f /run/onebrain-drive-backup.sh /run/onebrain_backup_crypto.py \
  /run/onebrain_erasure_ledger.py

sudo systemctl start onebrain-drive-backup.timer
```

After restoring, run the normal schema/Drive-malware activation gate, confirm a
fresh scanner heartbeat and current definitions, then verify login, Drive
listing, a clean-file download, and an indexed-file query before returning the
deployment to users. Pending or inconclusive revisions remain quarantined and
must be reconciled; restore is never permission to bypass their scan state.
The exact activation, definition-freshness, EICAR, and recovery procedure is in
the [Drive malware operations runbook](drive-malware-operations.md).

These authenticated local archives protect against logical deletion and bad
application updates. Because both backups and the external erasure ledger remain
on the same server, they are not a substitute for offsite backup plus offsite
ledger replication when protecting against whole-server or root-disk loss. A
missing external ledger is intentionally a hard restore blocker.

## Download and malware posture

Every completed Drive revision enters mandatory malware quarantine, including
files that are not indexed for AI. Ordinary download, indexing, and original-byte
export require a current-policy `clean` attestation over the exact stored hash and
size. Pending, scanning, infected, inconclusive, and rescan-required revisions
remain locked; approval cannot override that boundary.

OneBrain still never describes a clean verdict as proof that a file is safe.
Originals are not executed or rendered by the API and are served only as
attachments with `application/octet-stream`, `X-Content-Type-Options: nosniff`,
and `Cache-Control: private, no-store`; active Office/PDF previews remain outside
this release. Host and endpoint protection therefore remain required defense in
depth.
