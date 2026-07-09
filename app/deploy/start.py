"""Start the configured OneBrain deployment process."""

from __future__ import annotations

from app.deploy import start_api, start_worker
from app.deploy.runtime import deployment_process


def main() -> None:
    process = deployment_process()
    if process == "api":
        start_api.main()
        return
    if process == "worker":
        start_worker.main()
        return
    raise RuntimeError(f"Unsupported ONEBRAIN_PROCESS value: {process}")


if __name__ == "__main__":
    main()
