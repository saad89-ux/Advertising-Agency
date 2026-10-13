"""Launch Gate worker entry point."""

from __future__ import annotations

import logging
from typing import Any

from apps.worker.job_runner import run_once
from apps.api.notifications import process_pending_notifications

logger = logging.getLogger(__name__)


def main(*, process_jobs: bool = False, run_until_empty: bool = False, **kwargs: Any) -> Any:
    """Entry point for the worker.

    Defaults to the safe stub behavior to maintain compatibility with skeleton tests,
    delegating to job_runner.run_once() when process_jobs=True or when invoked as a runner.
    """
    if not process_jobs:
        print("Launch Gate worker stub; no jobs processed.")
        return None
    if run_until_empty:
        processed_jobs = []
        while True:
            processed_notifications = process_pending_notifications(batch_size=10)
            job = run_once(**kwargs)
            if job is not None:
                processed_jobs.append(job)
            elif not processed_notifications:
                break
        return processed_jobs
    process_pending_notifications(batch_size=10)
    return run_once(**kwargs)


if __name__ == "__main__":
    import sys
    run_all = "--all" in sys.argv or "--run-until-empty" in sys.argv
    main(process_jobs=True, run_until_empty=run_all)
