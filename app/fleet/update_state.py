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


def update_state_path(data_dir: str) -> str:
    return os.path.join(data_dir or ".", UPDATE_STATE_FILENAME)


def read_update_report(path: str) -> UpdateReport:
    """Never raises: missing file, bad JSON, wrong shape, or out-of-vocabulary
    values all return UpdateReport() (outcome='none')."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return UpdateReport.model_validate(data)
    except Exception:
        return UpdateReport()
