"""Start the background worker after the Postgres schema is ready."""

from __future__ import annotations

from app.deploy.runtime import exec_process, wait_for_schema_if_needed, worker_command


def main() -> None:
    wait_for_schema_if_needed()
    exec_process(worker_command())


if __name__ == "__main__":
    main()
