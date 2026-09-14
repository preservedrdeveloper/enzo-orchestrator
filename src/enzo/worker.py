from __future__ import annotations

import os
import signal
import time
import uuid

from .container import build_container


def main() -> None:
    container = build_container()
    worker_id = os.environ.get("ENZO_WORKER_ID", f"worker-{uuid.uuid4()}")
    poll_seconds = float(os.environ.get("ENZO_POLL_SECONDS", "0.5"))
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while running:
        progressed = container.orchestrator.run_one_job(worker_id=worker_id)
        progressed = container.orchestrator.run_one_outbox(worker_id=f"{worker_id}-outbox") or progressed
        if not progressed:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
