"""Bounded background preflight: retries poll the same content, never repeat model calls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Lock
import time

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preflight")
_jobs = {}
_lock = Lock()


def submit(user_id, payload, review):
    key = hashlib.sha256(json.dumps([user_id, payload], sort_keys=True).encode()).hexdigest()
    with _lock:
        now = time.monotonic()
        for old_key, (created, future) in list(_jobs.items()):
            if future.done() and now - created > 600:
                del _jobs[old_key]
        if key not in _jobs:
            if len(_jobs) >= 64:
                from fastapi import HTTPException
                raise HTTPException(503, "Review queue is full. Please retry shortly.")
            _jobs[key] = (now, _pool.submit(review))
        future = _jobs[key][1]
    if future.done():
        return future.result()
    return {"status": "reviewing", "message": "Checking campaign ads; no Meta launch has been attempted yet."}
