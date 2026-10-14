"""Unit and integration tests for Tracing (Langfuse via .env; PostgreSQL fallback).

Covers:
- Standalone DDL creation for worker_traces and worker_spans.
- PostgresTracer: start_trace, start_span, end_span, end_trace, get_trace, list_traces_by_batch.
- LangfuseTracer: enabled/disabled detection, resilient error suppression.
- AdWorkflowTracer: composite execution and in-memory fallback.
- End-to-end integration with LangGraph worker flow: verifying that mechanical,
  page_check, wording_check, and verdict steps emit spans tagged with ad_id and batch_id,
  including step inputs, applied rule findings, and costs.
- Opt-in real PostgreSQL integration test when RUN_INTEGRATION=1.
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
import pytest

from apps.worker.graph import run_ad_worker_flow
from apps.worker.tracing import (
    AdWorkflowTracer,
    LangfuseTracer,
    PostgresTracer,
    create_trace_tables,
    get_default_tracer,
)
from packages.contracts import AdContract, VerdictStatus


# --- In-Memory Fake Database for PostgresTracer Unit Tests ---

class FakeCursor:
    def __init__(self, db: FakePostgresDatabase) -> None:
        self.db = db
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        params_tuple = tuple(params) if params else ()
        s = " ".join(sql.strip().split())

        if s.startswith("CREATE TABLE") or s.startswith("CREATE INDEX"):
            self.db.ddl_statements.append(s)
            self.rows = []
        elif "INSERT INTO worker_traces" in s:
            tid, bid, aid, cid, status, meta, st = params_tuple
            self.db.traces[tid] = {
                "trace_id": tid,
                "batch_id": bid,
                "ad_id": aid,
                "client_id": cid,
                "status": status,
                "verdict": None,
                "total_cost_usd": 0.0,
                "step_count": 0,
                "metadata": meta,
                "start_time": st,
                "end_time": None,
            }
            self.rows = []
        elif "INSERT INTO worker_spans" in s:
            sid, tid, sname, status, inp, meta, st = params_tuple
            self.db.spans[sid] = {
                "span_id": sid,
                "trace_id": tid,
                "step_name": sname,
                "status": status,
                "cost_usd": 0.0,
                "input_data": inp,
                "output_data": "{}",
                "metadata": meta,
                "start_time": st,
                "end_time": None,
            }
            self.rows = []
        elif "UPDATE worker_spans" in s:
            # (status, cost_usd, out_json, [meta_json], end_time, span_id)
            if "worker_spans.metadata ||" in s:
                status, cost_usd, out_json, meta_json, end_time, sid = params_tuple
                if sid in self.db.spans:
                    sp = self.db.spans[sid]
                    sp["status"] = status
                    sp["cost_usd"] = cost_usd
                    sp["output_data"] = out_json
                    sp["end_time"] = end_time
            else:
                status, cost_usd, out_json, end_time, sid = params_tuple
                if sid in self.db.spans:
                    sp = self.db.spans[sid]
                    sp["status"] = status
                    sp["cost_usd"] = cost_usd
                    sp["output_data"] = out_json
                    sp["end_time"] = end_time
            self.rows = []
        elif "SELECT COALESCE(SUM(cost_usd), 0.0), COUNT(*)" in s:
            tid = params_tuple[0]
            spans_for_t = [s for s in self.db.spans.values() if s["trace_id"] == tid]
            tot_cost = sum(float(s["cost_usd"]) for s in spans_for_t)
            tot_cnt = len(spans_for_t)
            self.rows = [(tot_cost, tot_cnt)]
        elif "UPDATE worker_traces" in s:
            # (status, verdict, total_cost_usd, step_count, [meta_json], end_time, trace_id)
            if "worker_traces.metadata ||" in s:
                status, verd, cost, steps, meta, et, tid = params_tuple
            else:
                status, verd, cost, steps, et, tid = params_tuple
            if tid in self.db.traces:
                tr = self.db.traces[tid]
                tr["status"] = status
                tr["verdict"] = verd
                tr["total_cost_usd"] = float(cost)
                tr["step_count"] = int(steps)
                tr["end_time"] = et
            self.rows = []
        elif "FROM worker_traces" in s:
            if "WHERE trace_id = %s" in s:
                tid = params_tuple[0]
                tr = self.db.traces.get(tid)
                self.rows = (
                    [
                        (
                            tr["trace_id"],
                            tr["batch_id"],
                            tr["ad_id"],
                            tr["client_id"],
                            tr["status"],
                            tr["verdict"],
                            tr["total_cost_usd"],
                            tr["step_count"],
                            tr["metadata"],
                            tr["start_time"],
                            tr["end_time"],
                        )
                    ]
                    if tr
                    else []
                )
            elif "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                matches = [tr for tr in self.db.traces.values() if tr["batch_id"] == bid]
                self.rows = [
                    (
                        tr["trace_id"],
                        tr["batch_id"],
                        tr["ad_id"],
                        tr["client_id"],
                        tr["status"],
                        tr["verdict"],
                        tr["total_cost_usd"],
                        tr["step_count"],
                        tr["start_time"],
                        tr["end_time"],
                    )
                    for tr in matches
                ]
            else:
                self.rows = []
        elif "FROM worker_spans" in s:
            if "WHERE trace_id = %s" in s:
                tid = params_tuple[0]
                matches = [sp for sp in self.db.spans.values() if sp["trace_id"] == tid]
                self.rows = [
                    (
                        sp["span_id"],
                        sp["step_name"],
                        sp["status"],
                        sp["cost_usd"],
                        sp["input_data"],
                        sp["output_data"],
                        sp["metadata"],
                        sp["start_time"],
                        sp["end_time"],
                    )
                    for sp in matches
                ]
            else:
                self.rows = []
        else:
            self.rows = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class FakePostgresDatabase:
    def __init__(self) -> None:
        self.ddl_statements: list[str] = []
        self.traces: dict[str, dict[str, Any]] = {}
        self.spans: dict[str, dict[str, Any]] = {}
        self.autocommit = True

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


# --- 1. DDL & Setup Tests ---

def test_create_trace_tables_ddl() -> None:
    fake_conn = FakePostgresDatabase()
    create_trace_tables(connection=fake_conn)

    assert len(fake_conn.ddl_statements) == 4
    assert any("CREATE TABLE IF NOT EXISTS worker_traces" in s for s in fake_conn.ddl_statements)
    assert any("CREATE TABLE IF NOT EXISTS worker_spans" in s for s in fake_conn.ddl_statements)
    assert any("idx_worker_traces_batch" in s for s in fake_conn.ddl_statements)
    assert any("idx_worker_spans_trace" in s for s in fake_conn.ddl_statements)


def test_schema_does_not_contain_worker_traces() -> None:
    from apps.api import schema
    import inspect
    schema_code = inspect.getsource(schema)
    assert "worker_traces" not in schema_code
    assert "worker_spans" not in schema_code


# --- 2. PostgresTracer Tests ---

def test_postgres_tracer_trace_and_span_lifecycle() -> None:
    fake_conn = FakePostgresDatabase()
    tracer = PostgresTracer(connection=fake_conn)
    tracer.setup()

    # 1. Start trace
    trace_id = "batch-1:ad-100"
    tracer.start_trace(
        trace_id=trace_id,
        batch_id="batch-1",
        ad_id="ad-100",
        client_id="nordic_rest",
        metadata={"platform": "Meta"},
    )
    assert trace_id in fake_conn.traces
    assert fake_conn.traces[trace_id]["status"] == "in_progress"

    # 2. Start mechanical span
    span_id = tracer.start_span(
        trace_id=trace_id,
        step_name="mechanical",
        input_data={"headline": "Cozy Bed"},
    )
    assert span_id in fake_conn.spans
    assert fake_conn.spans[span_id]["step_name"] == "mechanical"

    # 3. End mechanical span
    tracer.end_span(
        span_id=span_id,
        trace_id=trace_id,
        output_data={"findings_count": 0},
        status="completed",
        cost_usd=0.0,
    )
    assert fake_conn.spans[span_id]["status"] == "completed"

    # 4. Start wording span with model cost
    span_id_2 = tracer.start_span(
        trace_id=trace_id,
        step_name="wording_check",
        input_data={"rules_retrieved": 3},
    )
    tracer.end_span(
        span_id_2,
        trace_id=trace_id,
        output_data={"citations": ["claims:1"]},
        status="completed",
        cost_usd=0.00015,
    )

    # 5. End trace
    tracer.end_trace(
        trace_id=trace_id,
        verdict="ready",
        status="completed",
    )
    assert fake_conn.traces[trace_id]["verdict"] == "ready"
    assert fake_conn.traces[trace_id]["total_cost_usd"] == 0.00015
    assert fake_conn.traces[trace_id]["step_count"] == 2

    # 6. Retrieve trace
    full_trace = tracer.get_trace(trace_id)
    assert full_trace is not None
    assert full_trace["trace_id"] == trace_id
    assert full_trace["verdict"] == "ready"
    assert len(full_trace["spans"]) == 2
    assert full_trace["spans"][0]["step_name"] == "mechanical"
    assert full_trace["spans"][1]["step_name"] == "wording_check"
    assert full_trace["spans"][1]["cost_usd"] == 0.00015

    # 7. List traces by batch
    batch_traces = tracer.list_traces_by_batch("batch-1")
    assert len(batch_traces) == 1
    assert batch_traces[0]["trace_id"] == trace_id


# --- 3. LangfuseTracer Tests ---

def test_langfuse_tracer_disabled_when_no_credentials() -> None:
    tracer = LangfuseTracer(public_key="", secret_key="")
    assert tracer.is_enabled is False

    # Calling methods when disabled does not raise exceptions
    tracer.start_trace("t1", "b1", "a1", "c1")
    s_id = tracer.start_span("t1", "mechanical")
    assert s_id.startswith("t1:mechanical:")
    tracer.end_span(s_id, "t1")
    tracer.end_trace("t1")


def test_langfuse_tracer_handles_client_errors_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    tracer = LangfuseTracer(public_key="pk-lf-test", secret_key="sk-lf-test", host="http://invalid-host:9999")
    tracer.is_enabled = True

    class FailingClient:
        def start_observation(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("Langfuse host unreachable")

        def flush(self) -> None:
            raise ConnectionError("Langfuse flush unreachable")

    tracer._client = FailingClient()

    # Never raises exceptions; suppresses and logs warnings
    tracer.start_trace("t1", "b1", "a1", "c1")
    s_id = tracer.start_span("t1", "step1")
    tracer.end_span(s_id, "t1")
    tracer.end_trace("t1")


# --- 4. AdWorkflowTracer & In-Memory Fallback Tests ---

def test_ad_workflow_tracer_in_memory_fallback_without_db() -> None:
    """When no database connection or Langfuse keys exist, tracer functions via memory buffer."""
    tracer = AdWorkflowTracer()

    trace_id = "batch-x:ad-y"
    tracer.start_trace(trace_id, "batch-x", "ad-y", "client-z")
    s1 = tracer.start_span(trace_id, "mechanical", input_data={"url": "https://example.com"})
    tracer.end_span(s1, trace_id, output_data={"ok": True}, cost_usd=0.0)

    s2 = tracer.start_span(trace_id, "wording_check")
    tracer.end_span(s2, trace_id, output_data={"rule_matched": "R1"}, cost_usd=0.0002)

    tracer.end_trace(trace_id, verdict="ready")

    # Read back from memory
    trace = tracer.get_trace(trace_id)
    assert trace is not None
    assert trace["verdict"] == "ready"
    assert trace["total_cost_usd"] == 0.0002
    assert trace["step_count"] == 2
    assert len(trace["spans"]) == 2
    assert trace["spans"][0]["input_data"] == {"url": "https://example.com"}

    # List by batch
    batch_list = tracer.list_traces_by_batch("batch-x")
    assert len(batch_list) == 1
    assert batch_list[0]["ad_id"] == "ad-y"


def test_get_default_tracer_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    tracer = get_default_tracer()
    assert tracer is not None
    assert tracer.langfuse_tracer is not None
    assert tracer.langfuse_tracer.is_enabled is False


# --- 5. LangGraph Worker Flow Tracing Integration ---

def test_langgraph_worker_flow_records_step_spans() -> None:
    """Verify that running the LangGraph worker flow records step spans for each executed node."""
    ad = AdContract(
        ad_id="ad-trace-01",
        batch_id="batch-trace-01",
        client_id="nordic_rest",
        row_number=1,
        platform="Meta",
        ad_text="Experience deep sleep with organic cooling sheets.",
        headline="Cooling Comfort",
        destination_url="https://nordicrest.com/deal?utm_source=facebook&utm_medium=paid_social&utm_campaign=summer_jul",
        creative_file="static_1080x1080.png",
        row_hash="hash-t1",
        sheet_hash="sheet-t1",
    )
    manifest = {"static_1080x1080.png": {"width": 1080, "height": 1080}}

    fake_conn = FakePostgresDatabase()
    pg_tracer = PostgresTracer(connection=fake_conn)
    tracer = AdWorkflowTracer(postgres_tracer=pg_tracer)

    result = run_ad_worker_flow(
        ad,
        manifest=manifest,
        tracer=tracer,
        run_page_checks=False,
        run_wording_checks=False,
    )

    assert result.verdict.status == VerdictStatus.READY
    assert result.trace_id == "batch-trace-01:ad-trace-01"

    # Verify trace in tracer
    trace = tracer.get_trace("batch-trace-01:ad-trace-01")
    assert trace is not None
    assert trace["verdict"] == "ready"
    assert trace["status"] == "completed"

    # Spans recorded: mechanical, page_check (skipped), wording_check (skipped), verdict
    span_names = [s["step_name"] for s in trace["spans"]]
    assert "mechanical" in span_names
    assert "verdict" in span_names

    # Verify mechanical span has input parameters and output findings
    mech_span = next(s for s in trace["spans"] if s["step_name"] == "mechanical")
    assert mech_span["input_data"]["platform"] == "Meta"
    assert mech_span["input_data"]["headline"] == "Cooling Comfort"
    assert "findings" in mech_span["output_data"]

    # Verify verdict span has output verdict
    verd_span = next(s for s in trace["spans"] if s["step_name"] == "verdict")
    assert verd_span["output_data"]["verdict"] == "ready"


# --- 6. Real PostgreSQL Integration Test (Opt-in) ---

@pytest.mark.integration
def test_postgres_tracer_real_db() -> None:
    """Opt-in integration test running against real PostgreSQL when RUN_INTEGRATION=1."""
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION is not 1; skipping PostgreSQL tracer integration test")

    test_db_url = os.environ.get("TEST_DATABASE_URL", "")
    if not test_db_url or not test_db_url.strip():
        pytest.fail("TEST_DATABASE_URL must be configured for integration tests")

    with psycopg.connect(test_db_url, autocommit=True) as conn:
        create_trace_tables(conn)
        tracer = PostgresTracer(connection=conn)

        trace_id = "real_batch:real_ad_1"
        tracer.start_trace(
            trace_id=trace_id,
            batch_id="real_batch",
            ad_id="real_ad_1",
            client_id="nordic_rest",
            metadata={"source": "real_integration_test"},
        )

        span_id = tracer.start_span(
            trace_id=trace_id,
            step_name="mechanical",
            input_data={"test": "data"},
        )
        tracer.end_span(
            span_id=span_id,
            trace_id=trace_id,
            output_data={"findings_count": 0},
            status="completed",
            cost_usd=0.0001,
        )

        tracer.end_trace(trace_id=trace_id, verdict="ready")

        trace_data = tracer.get_trace(trace_id)
        assert trace_data is not None
        assert trace_data["verdict"] == "ready"
        assert len(trace_data["spans"]) == 1
        assert trace_data["spans"][0]["step_name"] == "mechanical"
        assert trace_data["spans"][0]["cost_usd"] == 0.0001
