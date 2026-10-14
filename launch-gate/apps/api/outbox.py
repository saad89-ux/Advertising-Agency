"""Transactional outbox for ad jobs waiting to reach Redis."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from apps.api.database import get_session
from apps.api.queue import AdJob, enqueue_ad_jobs


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
