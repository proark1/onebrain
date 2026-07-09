"""Minimal HTTP health endpoint for worker-only deployments."""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOGGER = logging.getLogger("onebrain.workers")
HEALTH_ENV = "ONEBRAIN_WORKER_HEALTH_SERVER"


class WorkerHealthServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WorkerHealthHandler(BaseHTTPRequestHandler):
    server_version = "OneBrainWorkerHealth/1.0"

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/health":
            self.send_error(404)
            return

        body = json.dumps({"status": "ok", "process": "worker"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def start_worker_health_server_if_configured() -> WorkerHealthServer | None:
    mode = os.environ.get(HEALTH_ENV, "auto").strip().lower()
    if mode in {"0", "false", "no", "off", "disabled"}:
        return None

    port = os.environ.get("PORT", "").strip()
    if mode == "auto" and not port:
        return None
    if not port:
        port = "8000"

    try:
        port_number = int(port)
    except ValueError as exc:
        raise RuntimeError("PORT must be an integer when worker health server is enabled.") from exc

    server = WorkerHealthServer(("0.0.0.0", port_number), WorkerHealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="worker-health", daemon=True)
    thread.start()
    LOGGER.info("worker health server started port=%s", server.server_port)
    return server
