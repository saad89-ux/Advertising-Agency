"""Tracing implementation with Langfuse and PostgreSQL fallback.

Architecture & PRD compliance:
- PRD §4.9 Explainability [B R9, D8]: Each ad is a trace with a span per step,
  tagged with ad and batch IDs (Langfuse, open source). A reviewer can open
  what was checked, which rule applied, what the page showed and what it cost.
- Tech Stack: Tracing via Langfuse configured through environment variables.
- Resilient fallback: If Langfuse credentials are not provided or Langfuse is unreachable,
  all trace and span data is safely captured in PostgreSQL tables (or in-memory buffer).
- Standalone DDL creation for worker trace tables (preserving apps/api/schema.py).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# --- DDL Statements ---

WORKER_TRACES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS worker_traces (
    trace_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    ad_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    verdict TEXT,
    total_cost_usd NUMERIC(10, 6) DEFAULT 0.0,
    step_count INT DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb,
    start_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
"""

WORKER_SPANS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS worker_spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    cost_usd NUMERIC(10, 6) DEFAULT 0.0,
    input_data JSONB DEFAULT '{}'::jsonb,
    output_data JSONB DEFAULT '{}'::jsonb,
    metadata JSONB DEFAULT '{}'::jsonb,
    start_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
"""

WORKER_TRACES_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_worker_traces_batch ON worker_traces (batch_id);
"""

WORKER_SPANS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_worker_spans_trace ON worker_spans (trace_id);
"""


def _execute_trace_ddl(conn: Any) -> None:
    """Execute table and index creation DDLs on a PostgreSQL connection."""
    with conn.cursor() as cur:
        cur.execute(WORKER_TRACES_TABLE_DDL)
        cur.execute(WORKER_SPANS_TABLE_DDL)
        cur.execute(WORKER_TRACES_INDEX_DDL)
        cur.execute(WORKER_SPANS_INDEX_DDL)
    if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
        conn.commit()


def create_trace_tables(connection: Any = None) -> None:
    """Create trace and span tables in PostgreSQL independently of API schema."""
    if connection is not None:
        _execute_trace_ddl(connection)
    else:
        from apps.api.database import get_session
        with get_session() as conn:
            _execute_trace_ddl(conn)


# --- Data Structures ---

@dataclass
class SpanRecord:
    """Single execution step span within an ad verification trace."""

    span_id: str
    trace_id: str
    step_name: str
    status: str
    cost_usd: float
    input_data: dict[str, Any]
    output_data: dict[str, Any]
    metadata: dict[str, Any]
    start_time: str
    end_time: str | None = None


@dataclass
class TraceRecord:
    """Top-level verification trace for an ad."""

    trace_id: str
    batch_id: str
    ad_id: str
    client_id: str
    status: str
    verdict: str | None
    total_cost_usd: float
    step_count: int
    metadata: dict[str, Any]
    start_time: str
    end_time: str | None
    spans: list[SpanRecord] = field(default_factory=list)


# --- PostgreSQL Fallback Tracer ---

class PostgresTracer:
    """PostgreSQL fallback tracer recording traces and spans to relational tables."""

    def __init__(
        self,
        connection: Any = None,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.connection = connection
        self.connection_factory = connection_factory

    @contextmanager
    def _get_connection(self) -> Iterator[Any]:
        if self.connection is not None:
            yield self.connection
        elif self.connection_factory is not None:
            conn = self.connection_factory()
            try:
                yield conn
                if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
                    conn.commit()
            except Exception:
                if hasattr(conn, "rollback") and not getattr(conn, "autocommit", False):
                    conn.rollback()
                raise
            finally:
                if hasattr(conn, "close"):
                    conn.close()
        else:
            from apps.api.database import get_session
            with get_session() as conn:
                yield conn

    def _commit_if_needed(self, conn: Any) -> None:
        if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
            conn.commit()

    def setup(self) -> None:
        with self._get_connection() as conn:
            _execute_trace_ddl(conn)

    def start_trace(
        self,
        trace_id: str,
        batch_id: str,
        ad_id: str,
        client_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Record the start of an ad verification trace."""
        meta_json = json.dumps(metadata or {})
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO worker_traces (
                        trace_id, batch_id, ad_id, client_id, status, metadata, start_time
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (trace_id) DO UPDATE SET
                        batch_id = EXCLUDED.batch_id,
                        ad_id = EXCLUDED.ad_id,
                        client_id = EXCLUDED.client_id,
                        metadata = EXCLUDED.metadata;
                    """,
                    (trace_id, batch_id, ad_id, client_id, "in_progress", meta_json, now_iso),
                )
            self._commit_if_needed(conn)
        return trace_id

    def start_span(
        self,
        trace_id: str,
        step_name: str,
        input_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        span_id: str | None = None,
    ) -> str:
        """Record the start of a step span."""
        effective_span_id = span_id or f"{trace_id}:{step_name}:{uuid4().hex[:8]}"
        inp_json = json.dumps(input_data or {})
        meta_json = json.dumps(metadata or {})
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO worker_spans (
                        span_id, trace_id, step_name, status, input_data, metadata, start_time
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                    ON CONFLICT (span_id) DO NOTHING;
                    """,
                    (effective_span_id, trace_id, step_name, "in_progress", inp_json, meta_json, now_iso),
                )
            self._commit_if_needed(conn)
        return effective_span_id

    def end_span(
        self,
        span_id: str,
        trace_id: str,
        output_data: dict[str, Any] | None = None,
        status: str = "completed",
        cost_usd: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record the completion of a step span."""
        out_json = json.dumps(output_data or {})
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                if metadata:
                    meta_json = json.dumps(metadata)
                    cur.execute(
                        """
                        UPDATE worker_spans
                        SET status = %s,
                            cost_usd = %s,
                            output_data = %s::jsonb,
                            metadata = worker_spans.metadata || %s::jsonb,
                            end_time = %s
                        WHERE span_id = %s;
                        """,
                        (status, cost_usd, out_json, meta_json, now_iso, span_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE worker_spans
                        SET status = %s,
                            cost_usd = %s,
                            output_data = %s::jsonb,
                            end_time = %s
                        WHERE span_id = %s;
                        """,
                        (status, cost_usd, out_json, now_iso, span_id),
                    )
            self._commit_if_needed(conn)

    def end_trace(
        self,
        trace_id: str,
        verdict: str | None = None,
        status: str = "completed",
        total_cost_usd: float | None = None,
        step_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Finalize an ad verification trace with verdict and aggregated metrics."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                # If total_cost_usd or step_count not provided, aggregate from spans
                if total_cost_usd is None or step_count is None:
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(cost_usd), 0.0), COUNT(*)
                        FROM worker_spans
                        WHERE trace_id = %s;
                        """,
                        (trace_id,),
                    )
                    row = cur.fetchone()
                    calc_cost = float(row[0]) if row else 0.0
                    calc_steps = int(row[1]) if row else 0
                    if total_cost_usd is None:
                        total_cost_usd = calc_cost
                    if step_count is None:
                        step_count = calc_steps

                meta_json = json.dumps(metadata) if metadata else None
                if meta_json:
                    cur.execute(
                        """
                        UPDATE worker_traces
                        SET status = %s,
                            verdict = %s,
                            total_cost_usd = %s,
                            step_count = %s,
                            metadata = worker_traces.metadata || %s::jsonb,
                            end_time = %s
                        WHERE trace_id = %s;
                        """,
                        (status, verdict, total_cost_usd, step_count, meta_json, now_iso, trace_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE worker_traces
                        SET status = %s,
                            verdict = %s,
                            total_cost_usd = %s,
                            step_count = %s,
                            end_time = %s
                        WHERE trace_id = %s;
                        """,
                        (status, verdict, total_cost_usd, step_count, now_iso, trace_id),
                    )
            self._commit_if_needed(conn)

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Retrieve a full trace and its ordered spans from PostgreSQL."""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT trace_id, batch_id, ad_id, client_id, status, verdict,
                           total_cost_usd, step_count, metadata, start_time, end_time
                    FROM worker_traces
                    WHERE trace_id = %s;
                    """,
                    (trace_id,),
                )
                t_row = cur.fetchone()
                if not t_row:
                    return None

                tid, bid, aid, cid, status, verd, cost, steps, meta, st, et = t_row
                meta_dict = meta if isinstance(meta, dict) else json.loads(meta or "{}")

                cur.execute(
                    """
                    SELECT span_id, step_name, status, cost_usd, input_data,
                           output_data, metadata, start_time, end_time
                    FROM worker_spans
                    WHERE trace_id = %s
                    ORDER BY start_time ASC, created_at ASC;
                    """,
                    (trace_id,),
                )
                span_rows = cur.fetchall()

                spans: list[dict[str, Any]] = []
                for s in span_rows:
                    spans.append({
                        "span_id": s[0],
                        "step_name": s[1],
                        "status": s[2],
                        "cost_usd": float(s[3]),
                        "input_data": s[4] if isinstance(s[4], dict) else json.loads(s[4] or "{}"),
                        "output_data": s[5] if isinstance(s[5], dict) else json.loads(s[5] or "{}"),
                        "metadata": s[6] if isinstance(s[6], dict) else json.loads(s[6] or "{}"),
                        "start_time": str(s[7]),
                        "end_time": str(s[8]) if s[8] else None,
                    })

                return {
                    "trace_id": tid,
                    "batch_id": bid,
                    "ad_id": aid,
                    "client_id": cid,
                    "status": status,
                    "verdict": verd,
                    "total_cost_usd": float(cost),
                    "step_count": int(steps),
                    "metadata": meta_dict,
                    "start_time": str(st),
                    "end_time": str(et) if et else None,
                    "spans": spans,
                }

    def list_traces_by_batch(self, batch_id: str) -> list[dict[str, Any]]:
        """List summary of all traces belonging to a batch."""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT trace_id, batch_id, ad_id, client_id, status, verdict,
                           total_cost_usd, step_count, start_time, end_time
                    FROM worker_traces
                    WHERE batch_id = %s
                    ORDER BY start_time ASC;
                    """,
                    (batch_id,),
                )
                rows = cur.fetchall()
                results: list[dict[str, Any]] = []
                for r in rows:
                    results.append({
                        "trace_id": r[0],
                        "batch_id": r[1],
                        "ad_id": r[2],
                        "client_id": r[3],
                        "status": r[4],
                        "verdict": r[5],
                        "total_cost_usd": float(r[6]),
                        "step_count": int(r[7]),
                        "start_time": str(r[8]),
                        "end_time": str(r[9]) if r[9] else None,
                    })
                return results


# --- Langfuse Tracer ---

class LangfuseTracer:
    """Langfuse tracer emitting spans and traces to the hosted or self-hosted Langfuse server."""

    def __init__(
        self,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ) -> None:
        self.public_key = public_key
        self.secret_key = secret_key
        self.host = host or "https://cloud.langfuse.com"
        self._client: Any = None
        self._active_spans: dict[str, Any] = {}
        self._trace_contexts: dict[str, dict[str, Any]] = {}
        self.is_enabled = bool(self.public_key and self.secret_key)

        if self.is_enabled:
            try:
                from langfuse import Langfuse
                self._client = Langfuse(
                    public_key=self.public_key,
                    secret_key=self.secret_key,
                    host=self.host,
                    debug=False,
                )
                logger.info("Langfuse tracer initialized successfully pointing to %s", self.host)
            except Exception as exc:
                logger.warning("Failed to initialize Langfuse client: %s. Tracing will fall back to PostgreSQL.", exc)
                self.is_enabled = False
                self._client = None

    def start_trace(
        self,
        trace_id: str,
        batch_id: str,
        ad_id: str,
        client_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.is_enabled or not self._client:
            return
        try:
            self._trace_contexts[trace_id] = {
                "batch_id": batch_id,
                "ad_id": ad_id,
                "client_id": client_id,
                "metadata": metadata or {},
            }
        except Exception as exc:
            logger.warning("Langfuse start_trace failed: %s", exc)

    def start_span(
        self,
        trace_id: str,
        step_name: str,
        input_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        span_id: str | None = None,
    ) -> str:
        effective_span_id = span_id or f"{trace_id}:{step_name}:{uuid4().hex[:8]}"
        if not self.is_enabled or not self._client:
            return effective_span_id
        try:
            ctx = self._trace_contexts.get(trace_id, {})
            meta = {
                "trace_id": trace_id,
                "batch_id": ctx.get("batch_id"),
                "ad_id": ctx.get("ad_id"),
                "client_id": ctx.get("client_id"),
                **(metadata or {}),
            }
            span = self._client.start_observation(
                name=step_name,
                as_type="span",
                input=input_data,
                metadata=meta,
            )
            self._active_spans[effective_span_id] = span
        except Exception as exc:
            logger.warning("Langfuse start_span failed for step %s: %s", step_name, exc)
        return effective_span_id

    def end_span(
        self,
        span_id: str,
        trace_id: str,
        output_data: dict[str, Any] | None = None,
        status: str = "completed",
        cost_usd: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.is_enabled:
            return
        span = self._active_spans.pop(span_id, None)
        if span is None:
            return
        try:
            cost_details = {"total": cost_usd} if cost_usd > 0 else None
            span.update(
                output=output_data,
                status_message=status,
                cost_details=cost_details,
                metadata=metadata,
            )
            span.end()
        except Exception as exc:
            logger.warning("Langfuse end_span failed for %s: %s", span_id, exc)

    def end_trace(
        self,
        trace_id: str,
        verdict: str | None = None,
        status: str = "completed",
        total_cost_usd: float | None = None,
        step_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.is_enabled or not self._client:
            return
        try:
            self._trace_contexts.pop(trace_id, None)
            self._client.flush()
        except Exception as exc:
            logger.warning("Langfuse end_trace/flush failed: %s", exc)


# --- Composite Ad Workflow Tracer ---

class AdWorkflowTracer:
    """Composite tracer that coordinates Langfuse export with guaranteed PostgreSQL/In-Memory fallback."""

    def __init__(
        self,
        postgres_tracer: PostgresTracer | None = None,
        langfuse_tracer: LangfuseTracer | None = None,
    ) -> None:
        self.postgres_tracer = postgres_tracer
        self.langfuse_tracer = langfuse_tracer
        # In-memory buffer guaranteeing immediate local readability even without a live DB connection
        self._memory_traces: dict[str, dict[str, Any]] = {}
        self._memory_spans: dict[str, list[dict[str, Any]]] = {}

    def setup(self) -> None:
        """Initialize database tables if postgres tracer is configured."""
        if self.postgres_tracer:
            try:
                self.postgres_tracer.setup()
            except Exception as exc:
                logger.warning("Could not setup PostgreSQL trace tables: %s", exc)

    def start_trace(
        self,
        trace_id: str,
        batch_id: str,
        ad_id: str,
        client_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        now_iso = datetime.now(timezone.utc).isoformat()
        # Always record in memory
        self._memory_traces[trace_id] = {
            "trace_id": trace_id,
            "batch_id": batch_id,
            "ad_id": ad_id,
            "client_id": client_id,
            "status": "in_progress",
            "verdict": None,
            "total_cost_usd": 0.0,
            "step_count": 0,
            "metadata": metadata or {},
            "start_time": now_iso,
            "end_time": None,
        }
        self._memory_spans[trace_id] = []

        # Record in Postgres if available
        if self.postgres_tracer:
            try:
                self.postgres_tracer.start_trace(trace_id, batch_id, ad_id, client_id, metadata)
            except Exception as exc:
                logger.warning("Postgres start_trace failed: %s", exc)

        # Record in Langfuse if available
        if self.langfuse_tracer:
            try:
                self.langfuse_tracer.start_trace(trace_id, batch_id, ad_id, client_id, metadata)
            except Exception as exc:
                logger.warning("Langfuse start_trace failed: %s", exc)

        return trace_id

    def start_span(
        self,
        trace_id: str,
        step_name: str,
        input_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        span_id = f"{trace_id}:{step_name}:{uuid4().hex[:8]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        # In-memory record
        if trace_id not in self._memory_spans:
            self._memory_spans[trace_id] = []
        self._memory_spans[trace_id].append({
            "span_id": span_id,
            "step_name": step_name,
            "status": "in_progress",
            "cost_usd": 0.0,
            "input_data": input_data or {},
            "output_data": {},
            "metadata": metadata or {},
            "start_time": now_iso,
            "end_time": None,
        })

        # Postgres record
        if self.postgres_tracer:
            try:
                self.postgres_tracer.start_span(trace_id, step_name, input_data, metadata, span_id=span_id)
            except Exception as exc:
                logger.warning("Postgres start_span failed: %s", exc)

        # Langfuse record
        if self.langfuse_tracer:
            try:
                self.langfuse_tracer.start_span(trace_id, step_name, input_data, metadata, span_id=span_id)
            except Exception as exc:
                logger.warning("Langfuse start_span failed: %s", exc)

        return span_id

    def end_span(
        self,
        span_id: str,
        trace_id: str,
        output_data: dict[str, Any] | None = None,
        status: str = "completed",
        cost_usd: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()

        # Update in-memory record
        for s in self._memory_spans.get(trace_id, []):
            if s["span_id"] == span_id:
                s["output_data"] = output_data or {}
                s["status"] = status
                s["cost_usd"] = cost_usd
                s["end_time"] = now_iso
                if metadata:
                    s["metadata"].update(metadata)
                break

        # Postgres update
        if self.postgres_tracer:
            try:
                self.postgres_tracer.end_span(span_id, trace_id, output_data, status, cost_usd, metadata)
            except Exception as exc:
                logger.warning("Postgres end_span failed: %s", exc)

        # Langfuse update
        if self.langfuse_tracer:
            try:
                self.langfuse_tracer.end_span(span_id, trace_id, output_data, status, cost_usd, metadata)
            except Exception as exc:
                logger.warning("Langfuse end_span failed: %s", exc)

    def end_trace(
        self,
        trace_id: str,
        verdict: str | None = None,
        status: str = "completed",
        total_cost_usd: float | None = None,
        step_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()

        # Update in-memory record
        t = self._memory_traces.get(trace_id)
        if t:
            spans = self._memory_spans.get(trace_id, [])
            calc_cost = sum(s["cost_usd"] for s in spans)
            t["verdict"] = verdict
            t["status"] = status
            t["total_cost_usd"] = total_cost_usd if total_cost_usd is not None else calc_cost
            t["step_count"] = step_count if step_count is not None else len(spans)
            t["end_time"] = now_iso
            if metadata:
                t["metadata"].update(metadata)

        # Postgres finalize
        if self.postgres_tracer:
            try:
                self.postgres_tracer.end_trace(trace_id, verdict, status, total_cost_usd, step_count, metadata)
            except Exception as exc:
                logger.warning("Postgres end_trace failed: %s", exc)

        # Langfuse finalize
        if self.langfuse_tracer:
            try:
                self.langfuse_tracer.end_trace(trace_id, verdict, status, total_cost_usd, step_count, metadata)
            except Exception as exc:
                logger.warning("Langfuse end_trace failed: %s", exc)

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Get full trace record. Prefers PostgreSQL record if available, else in-memory."""
        if self.postgres_tracer:
            try:
                pg_trace = self.postgres_tracer.get_trace(trace_id)
                if pg_trace:
                    return pg_trace
            except Exception as exc:
                logger.warning("Postgres get_trace failed: %s", exc)

        t = self._memory_traces.get(trace_id)
        if not t:
            return None
        return {
            **t,
            "spans": list(self._memory_spans.get(trace_id, [])),
        }

    def list_traces_by_batch(self, batch_id: str) -> list[dict[str, Any]]:
        """List all traces belonging to a batch."""
        if self.postgres_tracer:
            try:
                pg_traces = self.postgres_tracer.list_traces_by_batch(batch_id)
                if pg_traces:
                    return pg_traces
            except Exception as exc:
                logger.warning("Postgres list_traces_by_batch failed: %s", exc)

        return [
            {
                "trace_id": t["trace_id"],
                "batch_id": t["batch_id"],
                "ad_id": t["ad_id"],
                "client_id": t["client_id"],
                "status": t["status"],
                "verdict": t["verdict"],
                "total_cost_usd": t["total_cost_usd"],
                "step_count": t["step_count"],
                "start_time": t["start_time"],
                "end_time": t["end_time"],
            }
            for t in self._memory_traces.values()
            if t["batch_id"] == batch_id
        ]


# --- Default Factory ---

def get_default_tracer(
    connection: Any = None,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> AdWorkflowTracer:
    """Construct default workflow tracer inspecting environment variables and database config."""
    load_dotenv()

    # 1. Langfuse Tracer
    pub_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip() or None
    sec_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip() or None
    host = os.environ.get("LANGFUSE_HOST", "").strip() or "https://cloud.langfuse.com"
    lf_tracer = LangfuseTracer(public_key=pub_key, secret_key=sec_key, host=host)

    # 2. Postgres Tracer
    pg_tracer: PostgresTracer | None = None
    if connection is not None or connection_factory is not None:
        pg_tracer = PostgresTracer(connection=connection, connection_factory=connection_factory)
    elif os.environ.get("DATABASE_URL", "").strip():
        pg_tracer = PostgresTracer()

    return AdWorkflowTracer(postgres_tracer=pg_tracer, langfuse_tracer=lf_tracer)
