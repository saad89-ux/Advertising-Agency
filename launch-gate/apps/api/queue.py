"""Redis queue adapter for per-ad worker jobs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from typing import Sequence

from dotenv import load_dotenv
from redis import Redis


AD_JOB_QUEUE = "launch_gate:ad_jobs"


@dataclass(frozen=True)
class AdJob:
    """The IDs needed by the worker to load a persisted ad and its batch."""

    batch_id: str
    ad_id: str


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
