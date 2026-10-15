"""Unit and integration tests for Server-Sent Events (SSE) Live Status (Task 50).

Covers:
- Authentication & Authorization:
  - 401 Unauthorized when unauthenticated (no token or bad token).
  - Valid JWT token accepted via Authorization: Bearer header.
  - Valid JWT token accepted via ?token=<jwt> query parameter (for EventSource).
  - 404 Not Found for non-existent batches.
  - 403 Forbidden when user is not assigned to any client in the batch.
- Multi-Tenant Client Scoping (PRD §4.8, Never-break Rule 3):
  - On a mixed-client batch, user assigned to client A sees ONLY client A ads, counts, and client IDs.
  - Live broadcast ad updates for unassigned clients are filtered and suppressed from the stream.
- Event Protocol & Lifecycle (PRD §7 / D12):
  - text/event-stream content type and Cache-Control headers.
  - connected handshake event with batch_id and client_ids.
  - batch_status event with progress, verdict_counts, and ad_statuses.
  - complete event emitted when all ads reach terminal verdict (pending == 0).
- Broadcaster Pub-Sub & Delivery:
  - notify_ad_update and notify_batch_status dispatch to active subscribers.
  - Thread-safe delivery across worker execution contexts.
- REST Status Snapshot:
  - GET /batches/{batch_id}/status returns JSON summary of live progress.
- Worker Integration:
  - Worker process_job triggers notify_ad_update.
  - Bounded recheck triggers notify_ad_update.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from apps.api import auth, main, sse
from apps.api.auth import CurrentUser, create_access_token
from apps.api.main import app
from apps.api.sse import (
    BatchEventBroadcaster,
    calculate_batch_live_status,
    format_sse,
    notify_ad_update,
    notify_batch_status,
)


class FakeSSEDatabase:
    """In-memory database storing simulated batches, ads, findings, traces, and approvals."""

    def __init__(self) -> None:
        self.batches: dict[str, dict[str, Any]] = {}
        self.ads: dict[str, dict[str, Any]] = {}
        self.findings: dict[str, dict[str, Any]] = {}
        self.traces: dict[tuple[str, str], dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}

    @contextmanager
    def session(self) -> Any:
        db = self

        class Connection:
            def execute(self, statement: str, params: Any = None) -> Any:
                params_tuple = tuple(params) if params else ()
                s = " ".join(statement.strip().split())

                # 1. Query batch record
                if "FROM batches WHERE batch_id = %s" in s:
                    bid = params_tuple[0]
                    b = db.batches.get(bid)
                    if b:
                        return FakeResult([
                            (b["batch_id"], b["uploaded_by"], b["created_at"], b["filename"], b["sheet_hash"])
                        ])
                    return FakeResult([])

                # 2. Count total ads in batch
                if "SELECT COUNT(*) FROM ads WHERE batch_id = %s" in s and "AND client_id" not in s:
                    bid = params_tuple[0]
                    cnt = sum(1 for a in db.ads.values() if a["batch_id"] == bid)
                    return FakeResult([(cnt,)])

                # 3. Query ads in batch scoped to allowed clients
                if "FROM ads WHERE batch_id = %s AND client_id = ANY(%s)" in s and "COUNT" not in s:
                    bid, clients = params_tuple
                    allowed = set(clients)
                    matching = [a for a in db.ads.values() if a["batch_id"] == bid and a["client_id"] in allowed]
                    matching.sort(key=lambda a: a["row_number"])
                    return FakeResult([
                        (
                            a["ad_id"], a["batch_id"], a["client_id"], a["row_number"],
                            a["platform"], a.get("platform_raw"), a.get("ad_text"),
                            a.get("headline"), a["destination_url"], a.get("creative_file"),
                            a["row_hash"], a["sheet_hash"]
                        )
                        for a in matching
                    ])

                # 4. Query findings for ad IDs
                if "FROM findings WHERE ad_id = ANY(%s)" in s:
                    ad_ids = set(params_tuple[0])
                    matching_f = [f for f in db.findings.values() if f["ad_id"] in ad_ids]
                    matching_f.sort(key=lambda f: (f["row_number"], f["finding_id"]))
                    return FakeResult([
                        (
                            f["finding_id"], f["ad_id"], f["row_number"], f["check_type"],
                            f["message"], f.get("field"), f.get("proposed_fix"),
                            f.get("rule_id"), f.get("file_ref")
                        )
                        for f in matching_f
                    ])

                # 5. Query worker_traces for verdicts
                if "FROM worker_traces WHERE batch_id = %s AND ad_id = ANY(%s)" in s:
                    bid, ad_ids = params_tuple
                    ad_set = set(ad_ids)
                    matching_t = [
                        (aid, tr["verdict"])
                        for (b, aid), tr in db.traces.items()
                        if b == bid and aid in ad_set
                    ]
                    return FakeResult(matching_t)

                # 6. Query ad_approvals
                if "FROM ad_approvals WHERE ad_id = ANY(%s)" in s:
                    ad_ids = set(params_tuple[0])
                    matching_appr = [
                        (aid, appr.get("state", "pending_review"))
                        for aid, appr in db.approvals.items()
                        if aid in ad_set
                    ]
                    return FakeResult(matching_appr)

                return FakeResult([])

        yield Connection()


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")


def make_token(user_id: str, role: str = "buyer") -> str:
    return create_access_token(user_id, role)


def setup_users(monkeypatch: pytest.MonkeyPatch, user_map: dict[str, list[str]]) -> None:
    """Mock auth users and their assigned clients."""
    def fake_get_user(uid: str):
        if uid in user_map:
            return {"user_id": uid, "email": f"{uid}@test.test", "display_name": uid, "role": "buyer", "is_active": True}
        return None

    monkeypatch.setattr(auth.users, "get_user_by_id", fake_get_user)
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda uid: user_map.get(uid, []))


# --- Tests ---

def test_sse_endpoint_requires_authentication() -> None:
    """Unauthenticated requests must return 401."""
    with TestClient(app) as client:
        res1 = client.get("/batches/batch-1/events")
        assert res1.status_code == 401
        assert "invalid or missing bearer token" in res1.json()["detail"]

        res2 = client.get("/batches/batch-1/live")
        assert res2.status_code == 401

        res3 = client.get("/batches/batch-1/status")
        assert res3.status_code == 401


def test_sse_endpoint_accepts_bearer_header_and_query_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid token accepted either via Authorization header or ?token= query parameter."""
    setup_users(monkeypatch, {"buyer-1": ["lumina_skin"]})
    db = FakeSSEDatabase()
    db.batches["batch-1"] = {
        "batch_id": "batch-1",
        "uploaded_by": "buyer-1",
        "created_at": "2026-10-14T10:00:00Z",
        "filename": "ads.csv",
        "sheet_hash": "hash-1",
    }
    db.ads["ad-1"] = {
        "ad_id": "ad-1",
        "batch_id": "batch-1",
        "client_id": "lumina_skin",
        "row_number": 1,
        "platform": "Meta",
        "destination_url": "https://lumina.test",
        "row_hash": "rh1",
        "sheet_hash": "hash-1",
    }
    monkeypatch.setattr(sse, "get_session", db.session)

    token = make_token("buyer-1")

    with TestClient(app) as client:
        # 1. Test Authorization header
        res_header = client.get("/batches/batch-1/events?once=true", headers={"Authorization": f"Bearer {token}"})
        assert res_header.status_code == 200
        assert "text/event-stream" in res_header.headers["content-type"]
        assert "Cache-Control" in res_header.headers
        assert "no-cache" in res_header.headers["Cache-Control"]
        assert "event: connected" in res_header.text
        assert "event: batch_status" in res_header.text

        # 2. Test query parameter ?token=
        res_query = client.get(f"/batches/batch-1/events?token={token}&once=true")
        assert res_query.status_code == 200
        assert "text/event-stream" in res_query.headers["content-type"]
        assert "event: connected" in res_query.text


def test_sse_not_found_and_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 on missing batch; 403 when user is not assigned to any client in the batch."""
    setup_users(monkeypatch, {
        "buyer-a": ["client_a"],
        "buyer-b": ["client_b"],
    })
    db = FakeSSEDatabase()
    db.batches["batch-b"] = {
        "batch_id": "batch-b",
        "uploaded_by": "buyer-b",
        "created_at": "2026-10-14T10:00:00Z",
        "filename": "b.csv",
        "sheet_hash": "hash-b",
    }
    db.ads["ad-b-1"] = {
        "ad_id": "ad-b-1",
        "batch_id": "batch-b",
        "client_id": "client_b",
        "row_number": 1,
        "platform": "Meta",
        "destination_url": "https://b.test",
        "row_hash": "rh-b",
        "sheet_hash": "hash-b",
    }
    monkeypatch.setattr(sse, "get_session", db.session)

    token_a = make_token("buyer-a")

    with TestClient(app) as client:
        # Non-existent batch -> 404
        res_404 = client.get("/batches/nonexistent-batch/events", headers={"Authorization": f"Bearer {token_a}"})
        assert res_404.status_code == 404
        assert res_404.json()["detail"] == "batch not found"

        # Forbidden batch -> 403
        res_403 = client.get("/batches/batch-b/events", headers={"Authorization": f"Bearer {token_a}"})
        assert res_403.status_code == 403
        assert res_403.json()["detail"] == "forbidden"


def test_sse_multi_client_scoping(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a mixed batch with Client A and Client B, each user only sees their own client's ads."""
    setup_users(monkeypatch, {
        "buyer-a": ["client_a"],
        "buyer-b": ["client_b"],
    })
    db = FakeSSEDatabase()
    db.batches["batch-mixed"] = {
        "batch_id": "batch-mixed",
        "uploaded_by": "buyer-a",
        "created_at": "2026-10-14T10:00:00Z",
        "filename": "mixed.csv",
        "sheet_hash": "hash-m",
    }
    # 2 ads for client_a, 1 ad for client_b
    db.ads["ad-a-1"] = {
        "ad_id": "ad-a-1",
        "batch_id": "batch-mixed",
        "client_id": "client_a",
        "row_number": 1,
        "platform": "Meta",
        "destination_url": "https://a.test",
        "row_hash": "rha1",
        "sheet_hash": "hash-m",
    }
    db.ads["ad-a-2"] = {
        "ad_id": "ad-a-2",
        "batch_id": "batch-mixed",
        "client_id": "client_a",
        "row_number": 2,
        "platform": "Google",
        "destination_url": "https://a.test",
        "row_hash": "rha2",
        "sheet_hash": "hash-m",
    }
    db.ads["ad-b-1"] = {
        "ad_id": "ad-b-1",
        "batch_id": "batch-mixed",
        "client_id": "client_b",
        "row_number": 3,
        "platform": "TikTok",
        "destination_url": "https://b.test",
        "row_hash": "rhb1",
        "sheet_hash": "hash-m",
    }
    # Traces: ad-a-1 ready, ad-a-2 ready, ad-b-1 fix_needed
    db.traces[("batch-mixed", "ad-a-1")] = {"verdict": "ready"}
    db.traces[("batch-mixed", "ad-a-2")] = {"verdict": "ready"}
    db.traces[("batch-mixed", "ad-b-1")] = {"verdict": "fix needed"}

    monkeypatch.setattr(sse, "get_session", db.session)

    token_a = make_token("buyer-a")
    token_b = make_token("buyer-b")

    with TestClient(app) as client:
        # Buyer A sees only client_a
        res_a = client.get("/batches/batch-mixed/events?once=true", headers={"Authorization": f"Bearer {token_a}"})
        assert res_a.status_code == 200
        # Parse data blocks
        data_lines_a = [json.loads(line[6:]) for line in res_a.text.splitlines() if line.startswith("data: ")]
        status_a = next(d for d in data_lines_a if "total_ads" in d)
        assert status_a["total_ads"] == 2
        assert status_a["client_ids"] == ["client_a"]
        assert len(status_a["ad_statuses"]) == 2
        assert all(ad["client_id"] == "client_a" for ad in status_a["ad_statuses"])

        # Buyer B sees only client_b
        res_b = client.get("/batches/batch-mixed/events?once=true", headers={"Authorization": f"Bearer {token_b}"})
        assert res_b.status_code == 200
        data_lines_b = [json.loads(line[6:]) for line in res_b.text.splitlines() if line.startswith("data: ")]
        status_b = next(d for d in data_lines_b if "total_ads" in d)
        assert status_b["total_ads"] == 1
        assert status_b["client_ids"] == ["client_b"]
        assert len(status_b["ad_statuses"]) == 1
        assert status_b["ad_statuses"][0]["client_id"] == "client_b"


def test_sse_complete_event_when_all_ads_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """When all ads in a batch have verdicts resolved (pending == 0), event: complete is emitted."""
    setup_users(monkeypatch, {"buyer-1": ["lumina_skin"]})
    db = FakeSSEDatabase()
    db.batches["batch-done"] = {
        "batch_id": "batch-done",
        "uploaded_by": "buyer-1",
        "created_at": "2026-10-14T10:00:00Z",
        "filename": "ads.csv",
        "sheet_hash": "hash-d",
    }
    db.ads["ad-1"] = {
        "ad_id": "ad-1",
        "batch_id": "batch-done",
        "client_id": "lumina_skin",
        "row_number": 1,
        "platform": "Meta",
        "destination_url": "https://lumina.test",
        "row_hash": "rh1",
        "sheet_hash": "hash-d",
    }
    db.traces[("batch-done", "ad-1")] = {"verdict": "ready"}
    monkeypatch.setattr(sse, "get_session", db.session)

    token = make_token("buyer-1")
    with TestClient(app) as client:
        res = client.get("/batches/batch-done/events", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        assert "event: complete" in res.text
        data_lines = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]
        complete_event = next(d for d in data_lines if d.get("status") == "completed")
        assert complete_event["total_ads"] == 1
        assert complete_event["verdict_counts"]["ready"] == 1


def test_sse_broadcaster_pub_sub() -> None:
    """Test standalone BatchEventBroadcaster subscribe, publish, and delivery."""
    broadcaster = BatchEventBroadcaster()

    async def run_pub_sub():
        q = await broadcaster.subscribe("b-123")
        # Publish ad status
        ad_payload = {
            "batch_id": "b-123",
            "ad_id": "ad-1",
            "verdict": "blocked",
            "client_id": "client_x",
        }
        published_count = broadcaster.publish_sync("b-123", "ad_status", ad_payload)
        assert published_count == 1

        msg = q.get_nowait()
        assert "event: ad_status" in msg
        assert '"verdict":"blocked"' in msg
        assert '"ad_id":"ad-1"' in msg

        # Unsubscribe
        await broadcaster.unsubscribe("b-123", q)
        assert len(broadcaster._subscribers.get("b-123", set())) == 0

    asyncio.run(run_pub_sub())


def test_sse_format_sse_helper() -> None:
    """Verify format_sse formatting according to SSE specification."""
    # Dict payload
    res = format_sse({"status": "ok", "count": 5}, event="test_event", id="evt-1", retry=5000)
    lines = res.splitlines()
    assert lines[0] == "id: evt-1"
    assert lines[1] == "event: test_event"
    assert lines[2] == "retry: 5000"
    assert lines[3] == 'data: {"status":"ok","count":5}'
    assert res.endswith("\n\n")


def test_get_batch_status_json_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /batches/{batch_id}/status returns a JSON snapshot of progress and counts."""
    setup_users(monkeypatch, {"buyer-1": ["lumina_skin"]})
    db = FakeSSEDatabase()
    db.batches["batch-json"] = {
        "batch_id": "batch-json",
        "uploaded_by": "buyer-1",
        "created_at": "2026-10-14T10:00:00Z",
        "filename": "ads.csv",
        "sheet_hash": "hash-j",
    }
    db.ads["ad-1"] = {
        "ad_id": "ad-1",
        "batch_id": "batch-json",
        "client_id": "lumina_skin",
        "row_number": 1,
        "platform": "Meta",
        "destination_url": "https://lumina.test",
        "row_hash": "rh1",
        "sheet_hash": "hash-j",
    }
    db.ads["ad-2"] = {
        "ad_id": "ad-2",
        "batch_id": "batch-json",
        "client_id": "lumina_skin",
        "row_number": 2,
        "platform": "Google",
        "destination_url": "https://lumina.test",
        "row_hash": "rh2",
        "sheet_hash": "hash-j",
    }
    db.traces[("batch-json", "ad-1")] = {"verdict": "ready"}
    # ad-2 has no trace -> pending

    monkeypatch.setattr(sse, "get_session", db.session)

    token = make_token("buyer-1")
    with TestClient(app) as client:
        res = client.get("/batches/batch-json/status", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()
        assert data["batch_id"] == "batch-json"
        assert data["status"] == "processing"
        assert data["total_ads"] == 2
        assert data["processed_ads"] == 1
        assert data["pending_ads"] == 1
        assert data["progress_percent"] == 50.0
        assert data["verdict_counts"]["ready"] == 1
        assert data["verdict_counts"]["pending"] == 1


def test_live_alias_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /batches/{batch_id}/live serves identical SSE streaming as /events."""
    setup_users(monkeypatch, {"buyer-1": ["lumina_skin"]})
    db = FakeSSEDatabase()
    db.batches["batch-alias"] = {
        "batch_id": "batch-alias",
        "uploaded_by": "buyer-1",
        "created_at": "2026-10-14T10:00:00Z",
        "filename": "ads.csv",
        "sheet_hash": "hash-al",
    }
    db.ads["ad-1"] = {
        "ad_id": "ad-1",
        "batch_id": "batch-alias",
        "client_id": "lumina_skin",
        "row_number": 1,
        "platform": "Meta",
        "destination_url": "https://lumina.test",
        "row_hash": "rh1",
        "sheet_hash": "hash-al",
    }
    db.traces[("batch-alias", "ad-1")] = {"verdict": "ready"}
    monkeypatch.setattr(sse, "get_session", db.session)

    token = make_token("buyer-1")
    with TestClient(app) as client:
        res = client.get("/batches/batch-alias/live?once=true", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        assert "event: connected" in res.text
        assert "event: complete" in res.text


def test_worker_job_runner_emits_ad_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that apps.worker.job_runner.process_job calls notify_ad_update upon job completion."""
    from apps.api.queue import AdJob
    from apps.worker.job_runner import process_job

    notifications: list[dict[str, Any]] = []

    def mock_notify_ad_update(batch_id: str, ad_id: str, **kwargs):
        notifications.append({"batch_id": batch_id, "ad_id": ad_id, **kwargs})

    monkeypatch.setattr("apps.api.sse.notify_ad_update", mock_notify_ad_update)

    job = AdJob(batch_id="batch-test", ad_id="ad-test")

    # Mock DB connection row lookup
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = {
        "ad_id": "ad-test",
        "batch_id": "batch-test",
        "client_id": "lumina_skin",
        "row_number": 1,
        "platform": "Meta",
        "ad_text": "Buy now",
        "headline": "Great Sale",
        "destination_url": "https://lumina.test?utm_source=meta",
        "creative_file": None,
        "row_hash": "rh",
        "sheet_hash": "sh",
    }
    mock_conn.execute.return_value = mock_cur

    process_job(
        job,
        client_id="lumina_skin",
        connection=mock_conn,
        run_page_checks=False,
        run_wording_checks=False,
    )

    assert len(notifications) == 1
    assert notifications[0]["batch_id"] == "batch-test"
    assert notifications[0]["ad_id"] == "ad-test"
    assert "verdict" in notifications[0]
    assert notifications[0]["client_id"] == "lumina_skin"
