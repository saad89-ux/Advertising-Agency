from contextlib import contextmanager
from copy import deepcopy

import pytest

from apps.api import outbox
from apps.api.queue import AD_JOB_QUEUE, AdJob, dequeue_ad_job, enqueue_ad_jobs


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class FakeOutboxDatabase:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def session(self):
        pending = deepcopy(self.rows)

        class Connection:
            def execute(inner, statement, params=None):
                if statement.lstrip().startswith("SELECT batch_id, ad_id"):
                    limit = params[0]
                    return FakeResult(
                        (row["batch_id"], row["ad_id"])
                        for row in pending
                        if row["queued_at"] is None
                    )
                if statement.lstrip().startswith("UPDATE ad_job_outbox"):
                    pairs = set(zip(params[0::2], params[1::2]))
                    for row in pending:
                        if (row["batch_id"], row["ad_id"]) in pairs:
                            row["queued_at"] = "synthetic timestamp"
                return FakeResult()

        try:
            yield Connection()
        except Exception:
            raise
        else:
            self.rows = pending


class FakeRedis:
    def __init__(self):
        self.items = []

    def rpush(self, key, *values):
        assert key == AD_JOB_QUEUE
        self.items.extend(values)
        return len(self.items)

    def lpop(self, key):
        assert key == AD_JOB_QUEUE
        return self.items.pop(0) if self.items else None


def test_redis_down_then_pending_outbox_job_is_reenqueued(monkeypatch):
    job = AdJob(batch_id="batch-1", ad_id="ad-1")
    database = FakeOutboxDatabase(
        [{"batch_id": job.batch_id, "ad_id": job.ad_id, "queued_at": None}]
    )
    monkeypatch.setattr(outbox, "get_session", database.session)

    def redis_down(jobs):
        raise ConnectionError("synthetic Redis outage")

    with pytest.raises(ConnectionError, match="Redis outage"):
        outbox.dispatch_pending_jobs(enqueue_fn=redis_down)
    assert database.rows[0]["queued_at"] is None

    redis_client = FakeRedis()
    assert outbox.dispatch_pending_jobs(
        enqueue_fn=lambda jobs: enqueue_ad_jobs(jobs, redis_client=redis_client)
    ) == 1

    assert database.rows[0]["queued_at"] == "synthetic timestamp"
    assert dequeue_ad_job(redis_client=redis_client) == job
    assert outbox.dispatch_pending_jobs(
        enqueue_fn=lambda jobs: enqueue_ad_jobs(jobs, redis_client=redis_client)
    ) == 0
