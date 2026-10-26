#!/bin/sh
# Starts the background worker (auto-restarting) and the API in ONE container.
# Why: Render's free tier has no separate "background worker" service, and the repo's
# own worker loop (apps.worker.production) must run continuously next to the API.
set -e

(
  while true; do
    python -m apps.worker.production || true
    echo "[start.sh] worker exited; restarting in 5 seconds" >&2
    sleep 5
  done
) &

# exec replaces the shell so Render's stop signal reaches uvicorn directly.
exec uvicorn apps.api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
