"""The on-box update-outcome channel. P3's host update.sh writes
<data_dir>/update_state.json with exactly the UpdateReport fields
({"last_target_version","outcome","migration_reached","attempt_id","ts"});
the reporter reads it every beat. Absent/invalid file -> the default report
(outcome='none') — on Railway the file never exists, so this is dormant."""

from __future__ import annotations

import json
import os

from app.fleet.heartbeat import UpdateReport

UPDATE_STATE_FILENAME = "update_state.json"
# G1-3 (P5-03): the box's onebrain_bootstrap.sh records the secrets_epoch it last
# SUCCESSFULLY applied (wrote /opt/onebrain/.env for) to this file, a sibling of
# update_state.json in the box's work dir. The reporter reads it every beat and emits
# it as UpdateReport.applied_secrets_epoch so the operator can watch rotation
# convergence. Absent (never exchanged, or Railway) -> 0, the inert default.
SECRETS_EPOCH_FILENAME = "secrets_epoch"


def update_state_path(data_dir: str) -> str:
    return os.path.join(data_dir or ".", UPDATE_STATE_FILENAME)


def secrets_epoch_path(data_dir: str) -> str:
    return os.path.join(data_dir or ".", SECRETS_EPOCH_FILENAME)


def read_update_report(path: str) -> UpdateReport:
    """Never raises: missing file, bad JSON, wrong shape, or out-of-vocabulary
    values all return UpdateReport() (outcome='none')."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return UpdateReport.model_validate(data)
    except Exception:
        return UpdateReport()


def read_applied_secrets_epoch(path: str) -> int:
    """The secrets_epoch the box last applied (G1-3). Never raises: a missing/garbled
    file or a negative value returns 0 (no claim). Clamped to >= 0 to match the
    UpdateReport field's ge=0 bound."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return max(0, int(fh.read().strip()))
    except Exception:
        return 0
