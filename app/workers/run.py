"""Run OneBrain background workers."""

from __future__ import annotations

import logging
import signal
import threading
import time

from app.config import get_settings
from app.deps import get_drive_malware_scanner, get_drive_malware_scanning_service, get_job_store
from app.drive.malware.definitions import DefinitionError
from app.drive.malware.factory import assert_drive_malware_runtime_packaged
from app.workers.health import start_worker_health_server_if_configured
from app.workers.service import Worker


class _DefinitionRefreshRunner:
    """Run slow mirror I/O off the job/heartbeat loop, at most once at a time."""

    def __init__(self, malware, *, check_interval_seconds: float = 60.0):
        self.malware = malware
        self.check_interval_seconds = max(1.0, float(check_interval_seconds))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._next_check_at = 0.0

    def start_if_due(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not force and now < self._next_check_at:
                return False
            self._next_check_at = now + self.check_interval_seconds
            self._thread = threading.Thread(
                target=self._run,
                name="drive-malware-definition-refresh",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            self.malware.refresh_definitions_if_due()
        except DefinitionError:
            logging.getLogger("onebrain.workers").exception(
                "Drive malware definition refresh failed; retaining the last verified set"
            )
        except Exception:
            logging.getLogger("onebrain.workers").exception(
                "Drive malware definition maintenance failed"
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    start_worker_health_server_if_configured()
    settings = get_settings()
    scanner = get_drive_malware_scanner()
    assert_drive_malware_runtime_packaged(scanner)
    malware = get_drive_malware_scanning_service()
    worker = Worker(get_job_store())
    malware.heartbeat_if_due(force=True)
    definition_refresh = _DefinitionRefreshRunner(malware)
    definition_refresh.start_if_due(force=True)
    stopping = False
    stop_event = threading.Event()

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        worker.stop_claiming()
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    logging.getLogger("onebrain.workers").info("worker started id=%s", worker.worker_id)
    while not stopping:
        try:
            definition_refresh.start_if_due()
            try:
                malware.reconcile(limit=max(1, int(settings.worker_batch_size) * 4))
            except Exception:
                # Quarantine metadata stays fail-closed. A maintenance outage
                # must not starve unrelated retention, intake, or AI jobs.
                logging.getLogger("onebrain.workers").exception(
                    "Drive malware reconciliation failed"
                )
            try:
                malware.heartbeat_if_due()
            except Exception:
                logging.getLogger("onebrain.workers").exception(
                    "Drive malware runtime heartbeat failed"
                )
            processed = worker.run_once()
        except Exception:
            logging.getLogger("onebrain.workers").exception("worker loop failed")
            processed = 0
        if not processed and not stopping:
            stop_event.wait(max(0.1, settings.worker_poll_seconds))


if __name__ == "__main__":
    main()
