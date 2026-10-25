"""Launch Gate worker entry point."""

from __future__ import annotations

import logging
from typing import Any

from apps.worker.job_runner import run_once
from apps.api.notifications import process_pending_notifications
from apps.api.queue import recover_meta_sync_jobs
from apps.api.outbox import dispatch_pending_meta_sync_jobs
from apps.worker.meta_sync import run_meta_sync_once

logger = logging.getLogger(__name__)


def main(*, process_jobs: bool = False, run_until_empty: bool = False, **kwargs: Any) -> Any:
    """Entry point for the worker.

    Defaults to the safe stub behavior to maintain compatibility with skeleton tests,
    delegating to job_runner.run_once() when process_jobs=True or when invoked as a runner.
    """
    if not process_jobs:
        print("Launch Gate worker stub; no jobs processed.")
        return None
    recover_meta_sync_jobs()
    dispatch_pending_meta_sync_jobs()
    if run_until_empty:
        processed_jobs = []
        while True:
            processed_notifications = process_pending_notifications(batch_size=10)
            dispatch_pending_meta_sync_jobs()
            sync_job = run_meta_sync_once(redis_client=kwargs.get("meta_redis_client"))
            ad_job = run_once(**kwargs)
            if sync_job is not None:
                processed_jobs.append(sync_job)
            if ad_job is not None:
                processed_jobs.append(ad_job)
            elif sync_job is None and ad_job is None and not processed_notifications:
                break
        return processed_jobs
    process_pending_notifications(batch_size=10)
    sync_job = run_meta_sync_once(redis_client=kwargs.get("meta_redis_client"))
    return sync_job if sync_job is not None else run_once(**kwargs)


if __name__ == "__main__":
    import sys
    run_all = "--all" in sys.argv or "--run-until-empty" in sys.argv
    main(process_jobs=True, run_until_empty=run_all)
