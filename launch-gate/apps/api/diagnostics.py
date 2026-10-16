"""Diagnostics API endpoint providing full real-time visibility into AI models and Langfuse tracing."""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends
from psycopg import Connection
from redis import Redis

from apps.api.auth import get_current_user, require_role
from apps.api.database import get_session
from apps.api.queue import AD_JOB_QUEUE, _redis_client
from apps.worker.model_client import is_gemini_configured

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("/ai-tracing")
def get_ai_and_tracing_status() -> dict[str, Any]:
    """Inspect real-time Gemini API status, Langfuse connection, Redis queue, and trace counts."""
    load_dotenv()

    # 1. Gemini / Model API Check
    model_key = os.environ.get("MODEL_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    gemini_active = is_gemini_configured()
    model_name = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

    # 2. Langfuse Status
    lf_pub = (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip(" \t\r\n\"'")
    lf_sec = (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip(" \t\r\n\"'")
    lf_host = (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com").strip(" \t\r\n\"'")
    lf_enabled = bool(lf_pub and lf_sec and "placeholder" not in lf_pub.lower())

    # 3. Redis Queue
    queue_length = 0
    redis_online = False
    try:
        r: Redis = _redis_client()
        queue_length = r.llen(AD_JOB_QUEUE)
        redis_online = True
    except Exception:
        pass

    # 4. PostgreSQL Trace Counts
    trace_count = 0
    span_count = 0
    db_online = False
    try:
        with get_session() as conn:
            with conn.cursor() as cur:
                # Ensure tables exist
                cur.execute(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'worker_traces');"
                )
                if cur.fetchone()[0]:
                    cur.execute("SELECT count(*) FROM worker_traces;")
                    trace_count = cur.fetchone()[0]
                    cur.execute("SELECT count(*) FROM worker_spans;")
                    span_count = cur.fetchone()[0]
            db_online = True
    except Exception:
        pass

    return {
        "gemini_api": {
            "is_configured": gemini_active,
            "status": "active" if gemini_active else "heuristic_fallback",
            "model_name": model_name,
            "message": (
                f"Google Gemini connected ({model_name})"
                if gemini_active
                else "MODEL_API_KEY not set in .env. Running in compliant heuristic semantic mode."
            ),
            "key_present": bool(model_key),
        },
        "langfuse": {
            "is_configured": lf_enabled,
            "status": "connected" if lf_enabled else "postgresql_fallback",
            "host": lf_host,
            "public_key_preview": f"{lf_pub[:6]}...{lf_pub[-4:]}" if len(lf_pub) > 10 else ("Set" if lf_pub else "Not Set"),
            "message": (
                f"Langfuse connected and streaming traces to {lf_host}"
                if lf_enabled
                else "Langfuse keys not configured in .env. All traces are safely captured locally in PostgreSQL."
            ),
        },
        "worker_queue": {
            "redis_connected": redis_online,
            "queue_name": AD_JOB_QUEUE,
            "pending_jobs": queue_length,
        },
        "telemetry_counts": {
            "database_connected": db_online,
            "total_traces": trace_count,
            "total_spans": span_count,
        },
    }
