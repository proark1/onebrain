"""Run OneBrain background workers."""

from __future__ import annotations

import logging
import signal
import time

from app.config import get_settings
from app.deps import get_job_store
from app.workers.service import Worker


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    worker = Worker(get_job_store())
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    logging.getLogger("onebrain.workers").info("worker started id=%s", worker.worker_id)
    while not stopping:
        processed = worker.run_once()
        if not processed:
            time.sleep(max(0.1, settings.worker_poll_seconds))


if __name__ == "__main__":
    main()
