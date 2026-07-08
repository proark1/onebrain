"""Start the FastAPI service with deployment-safe preflight steps."""

from __future__ import annotations

from app.deploy.runtime import api_command, exec_process, run_migrations_if_needed


def main() -> None:
    run_migrations_if_needed()
    exec_process(api_command())


if __name__ == "__main__":
    main()
