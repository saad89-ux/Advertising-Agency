"""Transactional outboxes for ad and Meta sync jobs waiting to reach Redis."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from apps.api.database import get_session
from apps.api.queue import (
    AdJob,
    MetaSyncJob,
    enqueue_ad_jobs,
    enqueue_meta_sync_jobs,
)


CREATE_AD_JOB_OUTBOX_TABLE = """
CREATE TABLE IF NOT EXISTS ad_job_outbox (
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    queued_at TIMESTAMPTZ,
    PRIMARY KEY (batch_id, ad_id)
)
"""

INSERT_AD_JOB_OUTBOX = """
INSERT INTO ad_job_outbox (batch_id, ad_id)
VALUES (%s, %s)
ON CONFLICT (batch_id, ad_id) DO NOTHING
"""


def create_ad_job_outbox_table() -> None:
    with get_session() as connection:
        connection.execute(CREATE_AD_JOB_OUTBOX_TABLE)


def persist_ad_jobs_on_connection(connection, jobs: Sequence[AdJob]) -> None:
    """Save dispatch intents in the caller's ads transaction."""
    for job in jobs:
        connection.execute(INSERT_AD_JOB_OUTBOX, (job.batch_id, job.ad_id))


def persist_meta_sync_job_on_connection(connection, job: MetaSyncJob) -> None:
    """Save a Meta sync dispatch intent in the sync-log transaction."""
    connection.execute(
        """INSERT INTO meta_sync_outbox (sync_id, workspace_id, sync_type, lookback_days)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (sync_id) DO NOTHING""",
        (job.sync_id, job.workspace_id, job.sync_type, job.lookback_days),
    )


def dispatch_pending_jobs(
    *,
    enqueue_fn: Callable[[Sequence[AdJob]], int] = enqueue_ad_jobs,
    batch_size: int = 100,
) -> int:
    """Push pending jobs to Redis, then stamp their queued_at time.

    A failed Redis operation raises and leaves the transaction uncommitted, so the
    same jobs remain pending for a later dispatch attempt.
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")

    with get_session() as connection:
        rows = connection.execute(
            """SELECT batch_id, ad_id FROM ad_job_outbox
               WHERE queued_at IS NULL
               ORDER BY created_at, batch_id, ad_id
               LIMIT %s
               FOR UPDATE SKIP LOCKED""",
            (batch_size,),
        ).fetchall()
        jobs = [AdJob(batch_id=row[0], ad_id=row[1]) for row in rows]
        if not jobs:
            return 0

        enqueue_fn(jobs)
        placeholders = ", ".join(["(%s, %s)"] * len(jobs))
        parameters = tuple(value for job in jobs for value in (job.batch_id, job.ad_id))
        connection.execute(
            f"""UPDATE ad_job_outbox SET queued_at = CURRENT_TIMESTAMP
                WHERE queued_at IS NULL AND (batch_id, ad_id) IN ({placeholders})""",
            parameters,
        )
        return len(jobs)


def dispatch_pending_meta_sync_jobs(
    *,
    enqueue_fn: Callable[[Sequence[MetaSyncJob]], int] = enqueue_meta_sync_jobs,
    batch_size: int = 100,
) -> int:
    """Dispatch persisted Meta sync intents, leaving failed Redis writes pending."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")

    with get_session() as connection:
        rows = connection.execute(
            """SELECT sync_id, workspace_id, sync_type, lookback_days
               FROM meta_sync_outbox
               WHERE queued_at IS NULL
               ORDER BY created_at, sync_id
               LIMIT %s
               FOR UPDATE SKIP LOCKED""",
            (batch_size,),
        ).fetchall()
        jobs = [
            MetaSyncJob(
                sync_id=str(row[0]),
                workspace_id=row[1],
                sync_type=row[2],
                lookback_days=row[3],
            )
            for row in rows
        ]
        if not jobs:
            return 0

        queued_count = enqueue_fn(jobs)
        if queued_count != len(jobs):
            raise RuntimeError("Redis did not accept every pending Meta sync job.")
        placeholders = ", ".join(["%s"] * len(jobs))
        connection.execute(
            f"""UPDATE meta_sync_outbox SET queued_at = CURRENT_TIMESTAMP
                WHERE queued_at IS NULL AND sync_id IN ({placeholders})""",
            tuple(job.sync_id for job in jobs),
        )
        return len(jobs)
