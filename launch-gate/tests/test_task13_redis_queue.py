import json

import pytest

from apps.api.queue import AD_JOB_QUEUE, AdJob, dequeue_ad_job, enqueue_ad_jobs


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


def test_enqueue_adds_one_id_only_message_per_ad_in_fifo_order():
    redis_client = FakeRedis()
    jobs = [AdJob("batch-1", "ad-1"), AdJob("batch-1", "ad-2")]

    assert enqueue_ad_jobs(jobs, redis_client=redis_client) == 2
    assert [json.loads(item) for item in redis_client.items] == [
        {"ad_id": "ad-1", "batch_id": "batch-1"},
        {"ad_id": "ad-2", "batch_id": "batch-1"},
    ]
    assert dequeue_ad_job(redis_client=redis_client) == jobs[0]
    assert dequeue_ad_job(redis_client=redis_client) == jobs[1]
    assert dequeue_ad_job(redis_client=redis_client) is None


def test_empty_enqueue_does_not_require_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert enqueue_ad_jobs([]) == 0


def test_enqueue_rejects_empty_identifiers_before_writing():
    redis_client = FakeRedis()
    with pytest.raises(ValueError, match="non-empty batch_id and ad_id"):
        enqueue_ad_jobs([AdJob("batch-1", " ")], redis_client=redis_client)
    assert redis_client.items == []


def test_dequeue_rejects_malformed_queue_message():
    redis_client = FakeRedis()
    redis_client.items.append('{"batch_id":"batch-1","ad_id":"ad-1","text":"raw"}')
    with pytest.raises(RuntimeError, match="invalid ad job in Redis queue"):
        dequeue_ad_job(redis_client=redis_client)
