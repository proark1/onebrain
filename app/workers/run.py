"""Run OneBrain background workers."""

from __future__ import annotations

import logging
import signal
import threading

from app.config import get_settings
from app.deps import get_job_store
from app.workers.health import start_worker_health_server_if_configured
from app.workers.service import Worker


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    start_worker_health_server_if_configured()
    settings = get_settings()
    worker = Worker(get_job_store())
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
            processed = worker.run_once()
        except Exception:
            logging.getLogger("onebrain.workers").exception("worker loop failed")
            processed = 0
        if not processed and not stopping:
            stop_event.wait(max(0.1, settings.worker_poll_seconds))


if __name__ == "__main__":
    main()
