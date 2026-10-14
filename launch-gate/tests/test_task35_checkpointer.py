"""Unit and integration tests for PostgreSQL checkpointer with thread_id = batch_id:ad_id.

Covers:
- Helper functions: make_thread_id, parse_thread_id, validate_thread_id.
- Standalone DDL creation for checkpoints, checkpoint_blobs, checkpoint_writes.
- CheckpointSaver core operations: put, get_tuple, put_writes, list, delete_thread.
- Channel blobs serialization and deserialization across checkpoint steps.
- Pending writes ordering via writes_sort_key.
- LangGraph flow interruption and resumption from checkpoint without repetition.
- End-to-end worker flow checkpointing.
- Opt-in integration test against real PostgreSQL when RUN_INTEGRATION=1.
"""

from __future__ import annotations

import os
from typing import Any, TypedDict

import psycopg
import pytest
from langgraph.checkpoint.base import (
    CheckpointTuple,
    RunnableConfig,
    empty_checkpoint,
)
from langgraph.graph import END, START, StateGraph

from apps.worker.checkpointer import (
    PostgresCheckpointSaver,
    create_checkpoint_tables,
    make_thread_id,
    parse_thread_id,
    validate_thread_id,
)
from apps.worker.graph import (
    create_ad_worker_graph,
    run_ad_worker_flow,
)
from packages.contracts import AdContract, FindingContract, VerdictStatus


# --- In-Memory Fake Database for Unit Tests ---

class FakeCursor:
    """Simulates a psycopg cursor recording rows in memory."""

    def __init__(self, db: FakePostgresDatabase) -> None:
        self.db = db
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        params_tuple = tuple(params) if params else ()
        s = " ".join(sql.strip().split())

        if s.startswith("CREATE TABLE"):
            self.db.ddl_statements.append(s)
            self.rows = []
        elif "INSERT INTO checkpoints" in s:
            tid, ns, cid, parent_cid, ctype, cdata, mtype, mdata = params_tuple
            self.db.checkpoints[(tid, ns, cid)] = {
                "thread_id": tid,
                "checkpoint_ns": ns,
                "checkpoint_id": cid,
                "parent_checkpoint_id": parent_cid,
                "checkpoint_type": ctype,
                "checkpoint": cdata,
                "metadata_type": mtype,
                "metadata": mdata,
            }
            self.rows = []
        elif "INSERT INTO checkpoint_blobs" in s:
            tid, ns, ch, ver, btype, bdata = params_tuple
            self.db.blobs[(tid, ns, ch, str(ver))] = {
                "thread_id": tid,
                "checkpoint_ns": ns,
                "channel": ch,
                "version": str(ver),
                "type": btype,
                "blob": bdata,
            }
            self.rows = []
        elif "INSERT INTO checkpoint_writes" in s:
            tid, ns, cid, task_id, idx, ch, wtype, wdata, task_path = params_tuple
            self.db.writes[(tid, ns, cid, task_id, idx)] = {
                "thread_id": tid,
                "checkpoint_ns": ns,
                "checkpoint_id": cid,
                "task_id": task_id,
                "idx": idx,
                "channel": ch,
                "type": wtype,
                "value": wdata,
                "task_path": task_path,
            }
            self.rows = []
        elif "SELECT channel, version, type, blob FROM checkpoint_blobs" in s:
            tid, ns = params_tuple
            self.rows = [
                (r["channel"], r["version"], r["type"], r["blob"])
                for k, r in self.db.blobs.items()
                if k[0] == tid and k[1] == ns
            ]
        elif "SELECT task_id, idx, channel, type, value, task_path FROM checkpoint_writes" in s:
            tid, ns, cid = params_tuple
            self.rows = [
                (
                    r["task_id"],
                    r["idx"],
                    r["channel"],
                    r["type"],
                    r["value"],
                    r["task_path"],
                )
                for k, r in self.db.writes.items()
                if k[0] == tid and k[1] == ns and k[2] == cid
            ]
        elif "DELETE FROM checkpoint_writes" in s:
            tid = params_tuple[0]
            self.db.writes = {k: v for k, v in self.db.writes.items() if k[0] != tid}
            self.rows = []
        elif "DELETE FROM checkpoint_blobs" in s:
            tid = params_tuple[0]
            self.db.blobs = {k: v for k, v in self.db.blobs.items() if k[0] != tid}
            self.rows = []
        elif "DELETE FROM checkpoints" in s:
            tid = params_tuple[0]
            self.db.checkpoints = {
                k: v for k, v in self.db.checkpoints.items() if k[0] != tid
            }
            self.rows = []
        elif "FROM checkpoints" in s:
            # Check if 8 columns (including thread_id, checkpoint_ns) are requested for list()
            is_list_query = "SELECT thread_id, checkpoint_ns" in s

            def format_row(r: dict[str, Any]) -> tuple[Any, ...]:
                if is_list_query:
                    return (
                        r["thread_id"],
                        r["checkpoint_ns"],
                        r["checkpoint_id"],
                        r["parent_checkpoint_id"],
                        r["checkpoint_type"],
                        r["checkpoint"],
                        r["metadata_type"],
                        r["metadata"],
                    )
                return (
                    r["checkpoint_id"],
                    r["parent_checkpoint_id"],
                    r["checkpoint_type"],
                    r["checkpoint"],
                    r["metadata_type"],
                    r["metadata"],
                )

            if "WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s" in s:
                tid, ns, cid = params_tuple
                r = self.db.checkpoints.get((tid, ns, cid))
                self.rows = [format_row(r)] if r else []
            elif "WHERE thread_id = %s AND checkpoint_ns = %s" in s:
                tid, ns = params_tuple
                matches = [
                    r
                    for (t, n, c), r in self.db.checkpoints.items()
                    if t == tid and n == ns
                ]
                matches.sort(key=lambda r: r["checkpoint_id"], reverse=True)
                if "LIMIT 1" in s:
                    matches = matches[:1]
                self.rows = [format_row(r) for r in matches]
            elif "WHERE thread_id = %s" in s:
                tid = params_tuple[0]
                matches = [
                    r
                    for (t, n, c), r in self.db.checkpoints.items()
                    if t == tid
                ]
                matches.sort(key=lambda r: r["checkpoint_id"], reverse=True)
                self.rows = [format_row(r) for r in matches]
            else:
                matches = list(self.db.checkpoints.values())
                matches.sort(key=lambda r: r["checkpoint_id"], reverse=True)
                self.rows = [format_row(r) for r in matches]
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
    """In-memory database storing simulated PostgreSQL checkpoint tables."""

    def __init__(self, *, autocommit: bool = False) -> None:
        self.ddl_statements: list[str] = []
        self.checkpoints: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.blobs: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.writes: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
        self.autocommit = autocommit
        self.commits_count = 0
        self.rollbacks_count = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self) -> None:
        self.commits_count += 1

    def rollback(self) -> None:
        self.rollbacks_count += 1


# --- 1. Thread ID Helper Tests ---

def test_make_thread_id_valid() -> None:
    assert make_thread_id("batch_123", "ad_456") == "batch_123:ad_456"
    assert make_thread_id(" batch_123 ", " ad_456 ") == "batch_123:ad_456"


def test_make_thread_id_rejects_empty_or_whitespace() -> None:
    with pytest.raises(ValueError, match="batch_id must be a non-empty string"):
        make_thread_id("", "ad_456")
    with pytest.raises(ValueError, match="batch_id must be a non-empty string"):
        make_thread_id("   ", "ad_456")
    with pytest.raises(ValueError, match="ad_id must be a non-empty string"):
        make_thread_id("batch_123", "")
    with pytest.raises(ValueError, match="ad_id must be a non-empty string"):
        make_thread_id("batch_123", "   ")


def test_make_thread_id_rejects_colons_in_batch_id() -> None:
    with pytest.raises(ValueError, match="batch_id cannot contain a colon"):
        make_thread_id("batch:invalid", "ad_456")


def test_parse_thread_id_valid() -> None:
    b, a = parse_thread_id("batch_123:ad_456")
    assert b == "batch_123"
    assert a == "ad_456"


def test_parse_thread_id_supports_composite_ad_id() -> None:
    b, a = parse_thread_id("batch_123:creative:variation_1")
    assert b == "batch_123"
    assert a == "creative:variation_1"


def test_parse_thread_id_rejects_malformed_formats() -> None:
    with pytest.raises(ValueError, match="thread_id must be formatted as 'batch_id:ad_id'"):
        parse_thread_id("no_colon_here")
    with pytest.raises(ValueError, match="thread_id must contain non-empty batch_id and ad_id"):
        parse_thread_id(":ad_456")
    with pytest.raises(ValueError, match="thread_id must contain non-empty batch_id and ad_id"):
        parse_thread_id("batch_123:")
    with pytest.raises(ValueError, match="thread_id must be formatted as 'batch_id:ad_id'"):
        parse_thread_id("")


def test_validate_thread_id_boolean_behavior() -> None:
    assert validate_thread_id("batch_1:ad_1") is True
    assert validate_thread_id("batch_1:creative:var") is True
    assert validate_thread_id("invalid") is False
    assert validate_thread_id(":ad_1") is False
    assert validate_thread_id("batch_1:") is False
    assert validate_thread_id("") is False


# --- 2. Table Creation Tests ---

def test_create_checkpoint_tables_executes_ddl() -> None:
    fake_conn = FakePostgresDatabase()
    create_checkpoint_tables(connection=fake_conn)

    assert len(fake_conn.ddl_statements) == 3
    assert any("CREATE TABLE IF NOT EXISTS checkpoints" in s for s in fake_conn.ddl_statements)
    assert any("CREATE TABLE IF NOT EXISTS checkpoint_blobs" in s for s in fake_conn.ddl_statements)
    assert any("CREATE TABLE IF NOT EXISTS checkpoint_writes" in s for s in fake_conn.ddl_statements)


def test_api_schema_does_not_depend_on_checkpointer() -> None:
    from apps.api import schema
    import inspect
    schema_src = inspect.getsource(schema)
    assert "checkpoint" not in schema_src.lower()


# --- 3. Core Checkpointer Operation Tests ---

def test_checkpointer_put_and_get_tuple() -> None:
    fake_conn = FakePostgresDatabase()
    saver = PostgresCheckpointSaver(connection=fake_conn)
    saver.setup()

    config: RunnableConfig = {"configurable": {"thread_id": "b1:a1", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "0001-checkpoint"
    checkpoint["channel_values"] = {"status": "in_progress", "count": 42}
    checkpoint["channel_versions"] = {"status": "v1", "count": "v1"}
    metadata = {"step": 1, "source": "input"}
    new_versions = {"status": "v1", "count": "v1"}

    saved_config = saver.put(config, checkpoint, metadata, new_versions)
    assert saved_config["configurable"]["checkpoint_id"] == "0001-checkpoint"
    assert saved_config["configurable"]["thread_id"] == "b1:a1"

    # Retrieve tuple
    tup = saver.get_tuple(config)
    assert tup is not None
    assert tup.config["configurable"]["checkpoint_id"] == "0001-checkpoint"
    assert tup.checkpoint["channel_values"] == {"status": "in_progress", "count": 42}
    assert tup.metadata.get("step") == 1
    assert tup.parent_config is None


def test_checkpointer_get_latest_checkpoint_without_checkpoint_id() -> None:
    fake_conn = FakePostgresDatabase()
    saver = PostgresCheckpointSaver(connection=fake_conn)

    config: RunnableConfig = {"configurable": {"thread_id": "b1:a1", "checkpoint_ns": ""}}

    cp1 = empty_checkpoint()
    cp1["id"] = "0001-cp"
    cp1["channel_values"] = {"step": 1}
    cp1["channel_versions"] = {"step": "v1"}
    saver.put(config, cp1, {"step": 1}, {"step": "v1"})

    cp2 = empty_checkpoint()
    cp2["id"] = "0002-cp"
    cp2["channel_values"] = {"step": 2}
    cp2["channel_versions"] = {"step": "v2"}
    saver.put(config, cp2, {"step": 2}, {"step": "v2"})

    # Query without checkpoint_id returns latest (0002-cp)
    latest_tup = saver.get_tuple(config)
    assert latest_tup is not None
    assert latest_tup.config["configurable"]["checkpoint_id"] == "0002-cp"
    assert latest_tup.checkpoint["channel_values"] == {"step": 2}

    # Query with specific checkpoint_id returns cp1
    specific_config: RunnableConfig = {
        "configurable": {"thread_id": "b1:a1", "checkpoint_ns": "", "checkpoint_id": "0001-cp"}
    }
    cp1_tup = saver.get_tuple(specific_config)
    assert cp1_tup is not None
    assert cp1_tup.config["configurable"]["checkpoint_id"] == "0001-cp"
    assert cp1_tup.checkpoint["channel_values"] == {"step": 1}


def test_checkpointer_put_writes_and_ordered_retrieval() -> None:
    fake_conn = FakePostgresDatabase()
    saver = PostgresCheckpointSaver(connection=fake_conn)

    config: RunnableConfig = {
        "configurable": {
            "thread_id": "b1:a1",
            "checkpoint_ns": "",
            "checkpoint_id": "0001-cp",
        }
    }

    cp = empty_checkpoint()
    cp["id"] = "0001-cp"
    saver.put(config, cp, {}, {})

    # Save pending writes
    saver.put_writes(
        config,
        writes=[("findings", {"code": "utm_missing"}), ("status", "blocked")],
        task_id="task-mechanical",
        task_path="path-1",
    )

    tup = saver.get_tuple(config)
    assert tup is not None
    assert len(tup.pending_writes) == 2
    channels = [w[1] for w in tup.pending_writes]
    assert channels == ["findings", "status"]


def test_checkpointer_list_filters_and_limits() -> None:
    fake_conn = FakePostgresDatabase()
    saver = PostgresCheckpointSaver(connection=fake_conn)

    config: RunnableConfig = {"configurable": {"thread_id": "b1:a1", "checkpoint_ns": ""}}

    for i in range(1, 6):
        cp = empty_checkpoint()
        cp["id"] = f"000{i}-cp"
        cp["channel_values"] = {"idx": i}
        cp["channel_versions"] = {"idx": f"v{i}"}
        saver.put(config, cp, {"iteration": i, "tag": "even" if i % 2 == 0 else "odd"}, {"idx": f"v{i}"})

    # List all
    all_tuples = list(saver.list(config))
    assert len(all_tuples) == 5
    assert [t.config["configurable"]["checkpoint_id"] for t in all_tuples] == [
        "0005-cp",
        "0004-cp",
        "0003-cp",
        "0002-cp",
        "0001-cp",
    ]

    # Limit to 2
    limited = list(saver.list(config, limit=2))
    assert len(limited) == 2
    assert [t.config["configurable"]["checkpoint_id"] for t in limited] == [
        "0005-cp",
        "0004-cp",
    ]

    # Filter by metadata tag="even"
    evens = list(saver.list(config, filter={"tag": "even"}))
    assert len(evens) == 2
    assert [t.config["configurable"]["checkpoint_id"] for t in evens] == [
        "0004-cp",
        "0002-cp",
    ]

    # Filter before 0004-cp
    before_cfg: RunnableConfig = {"configurable": {"checkpoint_id": "0004-cp"}}
    older = list(saver.list(config, before=before_cfg))
    assert [t.config["configurable"]["checkpoint_id"] for t in older] == [
        "0003-cp",
        "0002-cp",
        "0001-cp",
    ]


def test_checkpointer_delete_thread() -> None:
    fake_conn = FakePostgresDatabase()
    saver = PostgresCheckpointSaver(connection=fake_conn)

    cfg1: RunnableConfig = {"configurable": {"thread_id": "b1:a1", "checkpoint_ns": ""}}
    cfg2: RunnableConfig = {"configurable": {"thread_id": "b2:a2", "checkpoint_ns": ""}}

    cp1 = empty_checkpoint()
    cp1["id"] = "cp-1"
    cp1["channel_values"] = {"x": 1}
    cp1["channel_versions"] = {"x": "1"}
    saver.put(cfg1, cp1, {}, {"x": "1"})

    cp2 = empty_checkpoint()
    cp2["id"] = "cp-2"
    cp2["channel_values"] = {"x": 2}
    cp2["channel_versions"] = {"x": "2"}
    saver.put(cfg2, cp2, {}, {"x": "2"})

    assert saver.get_tuple(cfg1) is not None
    assert saver.get_tuple(cfg2) is not None

    # Delete thread b1:a1
    saver.delete_thread("b1:a1")
    assert saver.get_tuple(cfg1) is None
    assert saver.get_tuple(cfg2) is not None


def test_checkpointer_enforces_thread_id_format() -> None:
    fake_conn = FakePostgresDatabase()
    saver = PostgresCheckpointSaver(connection=fake_conn, enforce_thread_format=True)

    invalid_config: RunnableConfig = {"configurable": {"thread_id": "no_colon", "checkpoint_ns": ""}}
    with pytest.raises(ValueError, match="thread_id must be formatted as 'batch_id:ad_id'"):
        saver.get_tuple(invalid_config)

    with pytest.raises(ValueError, match="thread_id must be formatted as 'batch_id:ad_id'"):
        cp = empty_checkpoint()
        cp["id"] = "cp-1"
        saver.put(invalid_config, cp, {}, {})

    with pytest.raises(ValueError, match="thread_id must be formatted as 'batch_id:ad_id'"):
        saver.delete_thread("invalid_thread")


def test_checkpointer_commits_transaction() -> None:
    fake_conn = FakePostgresDatabase(autocommit=False)
    saver = PostgresCheckpointSaver(connection=fake_conn)

    config: RunnableConfig = {"configurable": {"thread_id": "b1:a1", "checkpoint_ns": ""}}
    cp = empty_checkpoint()
    cp["id"] = "cp-commit"
    saver.put(config, cp, {}, {})
    assert fake_conn.commits_count >= 1


# --- 4. LangGraph Flow Resumption Tests ---

class ResumableState(TypedDict):
    step1_done: bool
    step2_done: bool
    counter: int


def test_workflow_resumes_from_checkpoint_without_repetition() -> None:
    """Verify that when execution is interrupted, resuming with the same thread_id
    picks up from the checkpoint and does NOT repeat previously executed nodes.
    """
    execution_trace: list[str] = []

    def node_step1(state: ResumableState) -> dict[str, Any]:
        execution_trace.append("step1")
        return {"step1_done": True, "counter": state.get("counter", 0) + 1}

    def node_step2(state: ResumableState) -> dict[str, Any]:
        execution_trace.append("step2")
        return {"step2_done": True, "counter": state.get("counter", 0) + 10}

    builder = StateGraph(ResumableState)
    builder.add_node("step1", node_step1)
    builder.add_node("step2", node_step2)
    builder.add_edge(START, "step1")
    builder.add_edge("step1", "step2")
    builder.add_edge("step2", END)

    fake_conn = FakePostgresDatabase()
    saver = PostgresCheckpointSaver(connection=fake_conn)

    # First compile: interrupt before step2
    interrupted_graph = builder.compile(checkpointer=saver, interrupt_before=["step2"])
    thread_id = make_thread_id("batch_alpha", "ad_100")
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    state1 = interrupted_graph.invoke(
        {"step1_done": False, "step2_done": False, "counter": 0},
        config=config,
    )
    assert state1["step1_done"] is True
    assert state1["step2_done"] is False
    assert state1["counter"] == 1
    assert execution_trace == ["step1"]

    # Resume graph without interrupt
    resumed_graph = builder.compile(checkpointer=saver)
    state2 = resumed_graph.invoke(None, config=config)

    assert state2["step1_done"] is True
    assert state2["step2_done"] is True
    assert state2["counter"] == 11
    # step1 MUST NOT have been re-executed!
    assert execution_trace == ["step1", "step2"]


def test_ad_worker_flow_checkpoints_state() -> None:
    """Test full ad worker graph execution with PostgreSQL checkpointer."""
    manifest = {"static_1080x1080.png": {"width": 1080, "height": 1080}}
    ad = AdContract(
        ad_id="ad-999",
        batch_id="batch-888",
        client_id="nordic_rest",
        row_number=1,
        platform="Meta",
        ad_text="Premium cooling mattress for restful sleep.",
        headline="Cool Comfort",
        destination_url="https://nordicrest.com/deal?utm_source=facebook&utm_medium=paid_social&utm_campaign=summer_jul",
        creative_file="static_1080x1080.png",
        row_hash="hash-999",
        sheet_hash="sheet-888",
    )

    fake_conn = FakePostgresDatabase()
    saver = PostgresCheckpointSaver(connection=fake_conn)

    result = run_ad_worker_flow(
        ad,
        checkpointer=saver,
        manifest=manifest,
        run_page_checks=False,
        run_wording_checks=False,
    )

    assert result.verdict.status == VerdictStatus.READY
    # Verify thread_id batch_id:ad_id was automatically derived and checkpointed
    expected_thread_id = make_thread_id("batch-888", "ad-999")
    tup = saver.get_tuple({"configurable": {"thread_id": expected_thread_id}})
    assert tup is not None
    assert tup.checkpoint["channel_values"]["verdict"].status == VerdictStatus.READY


# --- 5. Real PostgreSQL Integration Test (Opt-in) ---

@pytest.mark.integration
def test_postgres_checkpointer_real_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in integration test running against real PostgreSQL when RUN_INTEGRATION=1."""
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION is not 1; skipping PostgreSQL checkpointer integration test")

    test_db_url = os.environ.get("TEST_DATABASE_URL", "")
    if not test_db_url or not test_db_url.strip():
        pytest.fail("TEST_DATABASE_URL must be configured for integration tests")

    with psycopg.connect(test_db_url, autocommit=True) as conn:
        create_checkpoint_tables(conn)
        saver = PostgresCheckpointSaver(connection=conn)

        thread_id = make_thread_id("real_batch_1", "real_ad_1")
        config: RunnableConfig = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

        cp = empty_checkpoint()
        cp["id"] = "cp-real-001"
        cp["channel_values"] = {"metric": 100, "status": "ok"}
        cp["channel_versions"] = {"metric": "v1", "status": "v1"}

        saver.put(config, cp, {"source": "integration_test"}, {"metric": "v1", "status": "v1"})

        tup = saver.get_tuple(config)
        assert tup is not None
        assert tup.checkpoint["channel_values"] == {"metric": 100, "status": "ok"}

        # Clean up
        saver.delete_thread(thread_id)
        assert saver.get_tuple(config) is None
