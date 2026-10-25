"""Redis queue adapter for per-ad worker jobs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from typing import Sequence

from dotenv import load_dotenv
from redis import Redis


AD_JOB_QUEUE = "launch_gate:ad_jobs"
META_SYNC_QUEUE = "launch_gate:meta_sync_jobs"
META_SYNC_PROCESSING_QUEUE = "launch_gate:meta_sync_jobs:processing"


@dataclass(frozen=True)
class AdJob:
    """The IDs needed by the worker to load a persisted ad and its batch."""

    batch_id: str
    ad_id: str


@dataclass(frozen=True)
class MetaSyncJob:
    """Workspace-scoped Meta sync request stored in Redis by ID only."""

    sync_id: str
    workspace_id: str
    sync_type: str
    lookback_days: int


def _redis_client() -> Redis:
    load_dotenv()
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL must be set in the environment or .env file")
    return Redis.from_url(redis_url, decode_responses=True)


def enqueue_ad_jobs(
    jobs: Sequence[AdJob], *, redis_client: Redis | None = None
) -> int:
    """Append one compact ID-only message for every newly saved ad."""
    if not jobs:
        return 0
    if any(not job.batch_id.strip() or not job.ad_id.strip() for job in jobs):
        raise ValueError("ad jobs require non-empty batch_id and ad_id")

    client = redis_client or _redis_client()
    try:
        messages = [
            json.dumps(asdict(job), separators=(",", ":"), sort_keys=True)
            for job in jobs
        ]
        return int(client.rpush(AD_JOB_QUEUE, *messages))
    finally:
        if redis_client is None:
            client.close()


def enqueue_meta_sync_job(
    job: MetaSyncJob, *, redis_client: Redis | None = None
) -> int:
    """Persist a compact Meta sync request for the background worker."""
    return enqueue_meta_sync_jobs((job,), redis_client=redis_client)


def enqueue_meta_sync_jobs(
    jobs: Sequence[MetaSyncJob], *, redis_client: Redis | None = None
) -> int:
    """Append validated Meta sync requests to Redis in one operation."""
    if not jobs:
        return 0
    if any(
        not job.sync_id.strip()
        or not job.workspace_id.strip()
        or job.sync_type not in {"INITIAL_IMPORT", "METRIC_REFRESH"}
        or isinstance(job.lookback_days, bool)
        or not isinstance(job.lookback_days, int)
        or not 1 <= job.lookback_days <= 90
        for job in jobs
    ):
        raise ValueError("invalid Meta sync job")
    client = redis_client or _redis_client()
    try:
        messages = [
            json.dumps(asdict(job), separators=(",", ":"), sort_keys=True)
            for job in jobs
        ]
        return int(client.rpush(META_SYNC_QUEUE, *messages))
    finally:
        if redis_client is None:
            client.close()


def claim_meta_sync_job(*, redis_client: Redis | None = None) -> str | None:
    """Atomically move the oldest pending sync request to the processing list."""
    client = redis_client or _redis_client()
    try:
        return client.lmove(META_SYNC_QUEUE, META_SYNC_PROCESSING_QUEUE, "LEFT", "RIGHT")
    finally:
        if redis_client is None:
            client.close()


def acknowledge_meta_sync_job(message: str, *, redis_client: Redis | None = None) -> int:
    """Remove a completed sync request from the processing list."""
    client = redis_client or _redis_client()
    try:
        return int(client.lrem(META_SYNC_PROCESSING_QUEUE, 1, message))
    finally:
        if redis_client is None:
            client.close()


def retry_meta_sync_job(message: str, *, redis_client: Redis | None = None) -> None:
    """Atomically return a failed-to-execute sync request to the pending list."""
    client = redis_client or _redis_client()
    try:
        moved = client.eval(
            """if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 1 then
                   return redis.call('LPUSH', KEYS[2], ARGV[1])
               end
               return 0""",
            2,
            META_SYNC_PROCESSING_QUEUE,
            META_SYNC_QUEUE,
            message,
        )
        if not moved:
            raise RuntimeError("could not return the claimed Meta sync job to its queue")
    finally:
        if redis_client is None:
            client.close()


def recover_meta_sync_jobs(*, redis_client: Redis | None = None) -> int:
    """Restore jobs left in processing by a worker that stopped before acknowledging."""
    client = redis_client or _redis_client()
    recovered = 0
    try:
        while client.lmove(META_SYNC_PROCESSING_QUEUE, META_SYNC_QUEUE, "RIGHT", "LEFT") is not None:
            recovered += 1
        return recovered
    finally:
        if redis_client is None:
            client.close()


def dequeue_ad_job(*, redis_client: Redis | None = None) -> AdJob | None:
    """Pop the oldest queued job, returning None when the queue is empty."""
    client = redis_client or _redis_client()
    try:
        message = client.lpop(AD_JOB_QUEUE)
        if message is None:
            return None
        try:
            payload = json.loads(message)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"batch_id", "ad_id"}
                or not isinstance(payload["batch_id"], str)
                or not isinstance(payload["ad_id"], str)
                or not payload["batch_id"].strip()
                or not payload["ad_id"].strip()
            ):
                raise ValueError
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("invalid ad job in Redis queue") from exc
        return AdJob(batch_id=payload["batch_id"], ad_id=payload["ad_id"])
    finally:
        if redis_client is None:
            client.close()
