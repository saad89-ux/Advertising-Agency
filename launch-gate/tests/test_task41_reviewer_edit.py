"""Tests for Task 41: Reviewer edit with reasons saved. Needs: 38, 40.

Verifies:
- Reviewer edits ad headline/text/URL with mandatory reason (PRD 4.6).
- Edits, reasons, iterations, and content snapshots are permanently saved.
- Ad row hash is updated.
- Approval state resets to 'pending_review' (PRD D13).
- Bounded re-check loop is enforced via MAX_EDIT_LOOPS (PRD D17).
- Client assignment check is enforced (Needs: 7: 403 Forbidden).
- Ads in 'sending' or 'sent' state cannot be edited (409 Conflict).
- Outbox re-arming triggers worker re-check (Architecture §1).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
import pytest
from fastapi.testclient import TestClient

from apps.api import auth, main, users
from apps.api.database import get_session
from apps.api.edits import (
    create_ad_edits_table,
    get_ad_edits_history,
    perform_ad_edit,
)
from apps.api.main import app
from apps.api.settings import max_edit_loops


# --- Mock Database Setup ---

class MockDB:
    def __init__(self):
        self.ads = {
            "ad-edit-1": {
                "ad_id": "ad-edit-1",
                "batch_id": "batch-e1",
                "client_id": "client-alpha",
                "row_number": 1,
                "platform": "facebook",
                "ad_text": "Original copy with a typo",
                "headline": "Original Headline 35 chars long exceeds limit",
                "destination_url": "https://alpha.example/offer?utm_source=fb",
                "row_hash": "hash-original-1",
                "sheet_hash": "sheet-hash-1",
                "creative_file": "banner.png",
            },
            "ad-edit-beta": {
                "ad_id": "ad-edit-beta",
                "batch_id": "batch-e1",
                "client_id": "client-beta",
                "row_number": 2,
                "platform": "google",
                "ad_text": "Beta copy",
                "headline": "Beta headline",
                "destination_url": "https://beta.example",
                "row_hash": "hash-beta-1",
                "sheet_hash": "sheet-hash-1",
                "creative_file": "",
            },
        }
        self.approvals = {
            "ad-edit-1": {
                "ad_id": "ad-edit-1",
                "client_id": "client-alpha",
                "state": "approved",
                "content_hash": "hash-original-1",
                "approved_by": "buyer-1",
                "approved_at": datetime.now(timezone.utc),
                "evidence_note": None,
                "evidence_file": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": None,
                "send_attempts": 0,
                "sent_at": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            "ad-edit-beta": {
                "ad_id": "ad-edit-beta",
                "client_id": "client-beta",
                "state": "pending_review",
                "content_hash": None,
                "approved_by": None,
                "approved_at": None,
                "evidence_note": None,
                "evidence_file": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": None,
                "send_attempts": 0,
                "sent_at": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
        }
        self.edits = []
        self.outbox = []
        self.history = []


class FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


@pytest.fixture
def mock_db():
    return MockDB()


@pytest.fixture
def fake_session(mock_db: MockDB, monkeypatch: pytest.MonkeyPatch):
    class FakeCursor:
        def __init__(self, db: MockDB):
            self.db = db

        def execute(self, statement: str, params: Any = None):
            s = statement.strip()
            params_tuple = tuple(params) if params is not None else ()

            # 1. Query ad record
            if "FROM ads" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                ad = self.db.ads.get(aid)
                if ad:
                    return FakeResult([(
                        ad["ad_id"], ad["batch_id"], ad["client_id"], ad["row_number"],
                        ad["platform"], ad["ad_text"], ad["headline"], ad["destination_url"],
                        ad["row_hash"], ad["sheet_hash"], ad.get("creative_file", "")
                    )])
                return FakeResult([])

            # 2. Update ads
            if "UPDATE ads" in s:
                hd, txt, url, cr, r_hash, aid = params_tuple
                ad = self.db.ads.get(aid)
                if ad:
                    ad["headline"] = hd
                    ad["ad_text"] = txt
                    ad["destination_url"] = url
                    ad["creative_file"] = cr
                    ad["row_hash"] = r_hash
                return FakeResult([])

            # 3. Query ad_approvals
            if "FROM ad_approvals" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                appr = self.db.approvals.get(aid)
                if appr:
                    return FakeResult([(
                        appr["ad_id"], appr["client_id"], appr["state"], appr["content_hash"],
                        appr["approved_by"], appr["approved_at"], appr["evidence_note"],
                        appr["evidence_file"], appr["rejection_reason"], appr["rejected_by"],
                        appr["rejected_at"], appr["idempotency_key"], appr["send_attempts"],
                        appr["sent_at"], appr["created_at"], appr["updated_at"],
                        appr.get("content_snapshot")
                    )])
                return FakeResult([])

            # 4. Update ad_approvals
            if "UPDATE ad_approvals" in s:
                to_state = params_tuple[0]
                aid = params_tuple[-1]
                appr = self.db.approvals.get(aid)
                if appr:
                    appr["state"] = to_state
                    appr["updated_at"] = datetime.now(timezone.utc)
                return FakeResult([])

            # 5. Count ad_edits
            if "SELECT COUNT(*) FROM ad_edits WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                cnt = sum(1 for e in self.db.edits if e["ad_id"] == aid)
                return FakeResult([(cnt,)])

            # 6. Insert ad_edits
            if "INSERT INTO ad_edits" in s:
                aid, bid, cid, u_id, rsn, prev_c, new_c, prev_h, new_h, iter_num = params_tuple
                self.db.edits.append({
                    "edit_id": len(self.db.edits) + 1,
                    "ad_id": aid,
                    "batch_id": bid,
                    "client_id": cid,
                    "edited_by": u_id,
                    "reason": rsn,
                    "previous_content": prev_c,
                    "new_content": new_c,
                    "previous_row_hash": prev_h,
                    "new_row_hash": new_h,
                    "edit_iteration": iter_num,
                    "created_at": datetime.now(timezone.utc),
                })
                return FakeResult([])

            # 7. Query ad_edits
            if "FROM ad_edits" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                matching = [e for e in self.db.edits if e["ad_id"] == aid]
                matching.sort(key=lambda e: e["edit_iteration"])
                return FakeResult([
                    (
                        e["edit_id"], e["ad_id"], e["batch_id"], e["client_id"],
                        e["edited_by"], e["reason"], e["previous_content"],
                        e["new_content"], e["previous_row_hash"], e["new_row_hash"],
                        e["edit_iteration"], e["created_at"]
                    )
                    for e in matching
                ])

            # 8. Insert into outbox
            if "INSERT INTO ad_job_outbox" in s:
                bid, aid = params_tuple[:2]
                self.db.outbox.append((bid, aid))
                return FakeResult([])

            # 9. Insert approval_history
            if "INSERT INTO approval_history" in s:
                return FakeResult([])

            return FakeResult([])

    class Connection:
        def __init__(self, db: MockDB):
            self.cursor_obj = FakeCursor(db)

        def execute(self, statement: str, params: Any = None):
            return self.cursor_obj.execute(statement, params)

    conn = Connection(mock_db)

    @contextmanager
    def _session():
        yield conn

    monkeypatch.setattr("apps.api.edits.get_session", _session)
    monkeypatch.setattr("apps.api.approvals.get_session", _session)
    return conn


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")


def make_token(user_id: str, role: str) -> str:
    return auth.create_access_token(user_id=user_id, role=role)


def mock_users_helper(monkeypatch: pytest.MonkeyPatch, assigned_clients: list[str], role: str = "buyer"):
    uid = "test-reviewer-uuid"
    record = {
        "user_id": uid,
        "email": "reviewer@example.test",
        "display_name": "Reviewer Test",
        "role": role,
        "is_active": True,
    }
    monkeypatch.setattr(users, "get_user_by_id", lambda u: record if u == uid else None)
    monkeypatch.setattr(users, "get_user_client_ids", lambda u: assigned_clients if u == uid else [])
    return uid


# --- Tests: Schema Creation ---

def test_create_ad_edits_table_ddl(monkeypatch: pytest.MonkeyPatch):
    """Verify create_ad_edits_table issues DDL for ad_edits audit storage."""
    statements = []

    class FakeConn:
        def execute(self, s, params=None):
            statements.append((s, params))

    @contextmanager
    def _fake_session():
        yield FakeConn()

    monkeypatch.setattr("apps.api.edits.get_session", _fake_session)
    monkeypatch.setattr("apps.api.edits.create_ads_table", lambda: None)
    monkeypatch.setattr("apps.api.edits.create_ad_job_outbox_table", lambda: None)

    create_ad_edits_table()

    flat_sql = " ".join(s[0] for s in statements)
    assert "CREATE TABLE IF NOT EXISTS ad_edits" in flat_sql
    assert "edit_iteration" in flat_sql
    assert "reason TEXT NOT NULL" in flat_sql
    assert "previous_row_hash TEXT NOT NULL" in flat_sql
    assert "new_row_hash TEXT NOT NULL" in flat_sql


# --- Tests: Edit Ad Success & Reason Saved ---

def test_reviewer_edit_success_and_reason_saved(fake_session, mock_db, monkeypatch):
    """Verify reviewer can edit headline and copy, saving mandatory reason and resetting approval."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    old_hash = mock_db.ads["ad-edit-1"]["row_hash"]
    assert mock_db.approvals["ad-edit-1"]["state"] == "approved"

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-edit-1/edit",
            json={
                "headline": "Clean Short Headline",
                "ad_text": "Corrected copy fixing typo",
                "reason": "Truncated headline to meet 30 char platform limit and corrected typo",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["ad_id"] == "ad-edit-1"
    assert data["headline"] == "Clean Short Headline"
    assert data["ad_text"] == "Corrected copy fixing typo"
    assert data["previous_row_hash"] == old_hash
    assert data["new_row_hash"] != old_hash
    assert data["reason"] == "Truncated headline to meet 30 char platform limit and corrected typo"
    assert data["edit_iteration"] == 1
    assert data["recheck_queued"] is True
    # State reset to pending_review!
    assert data["state"] == "pending_review"
    assert mock_db.approvals["ad-edit-1"]["state"] == "pending_review"

    # Verify audit record saved in ad_edits
    assert len(mock_db.edits) == 1
    e = mock_db.edits[0]
    assert e["ad_id"] == "ad-edit-1"
    assert e["edited_by"] == uid
    assert e["reason"] == "Truncated headline to meet 30 char platform limit and corrected typo"
    assert e["edit_iteration"] == 1
    assert e["previous_row_hash"] == old_hash
    assert e["new_row_hash"] == data["new_row_hash"]

    # Verify outbox re-armed
    assert ("batch-e1", "ad-edit-1") in mock_db.outbox


# --- Tests: Mandatory Reason Validation ---

def test_reviewer_edit_requires_reason(fake_session, mock_db, monkeypatch):
    """Verify edit without a reason returns 422 Unprocessable Content."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        # Empty string
        res = client.post(
            "/ads/ad-edit-1/edit",
            json={
                "headline": "New Headline",
                "reason": "   ",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 422
    assert "edit reason is required" in res.json()["detail"]


# --- Tests: No Changes Detected ---

def test_reviewer_edit_rejects_identical_content(fake_session, mock_db, monkeypatch):
    """Verify submitting an edit with unchanged values returns 422."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    cur_ad = mock_db.ads["ad-edit-1"]
    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-edit-1/edit",
            json={
                "headline": cur_ad["headline"],
                "ad_text": cur_ad["ad_text"],
                "destination_url": cur_ad["destination_url"],
                "reason": "No actual change",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 422
    assert "no changes detected" in res.json()["detail"]


# --- Tests: Client Scoping (Needs: 7) ---

def test_reviewer_edit_forbidden_for_unassigned_client(fake_session, mock_db, monkeypatch):
    """Verify buyer assigned to Client A CANNOT edit Client B ad -> 403 Forbidden."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-edit-beta/edit",
            json={
                "headline": "New Beta Headline",
                "reason": "Attempting edit without client assignment",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 403
    assert res.json() == {"detail": "forbidden"}


# --- Tests: Terminal State Protection ---

@pytest.mark.parametrize("terminal_state", ["sending", "sent"])
def test_reviewer_edit_blocked_on_terminal_state(fake_session, mock_db, monkeypatch, terminal_state):
    """Verify ad in sending or sent state cannot be edited."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    mock_db.approvals["ad-edit-1"]["state"] = terminal_state

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-edit-1/edit",
            json={
                "headline": "Too Late Headline",
                "reason": "Should fail",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 409
    assert f"cannot edit ad in '{terminal_state}' state" in res.json()["detail"]


# --- Tests: Bounded Loop Cap (PRD D17) ---

def test_reviewer_edit_bounded_loop_cap_enforced(fake_session, mock_db, monkeypatch):
    """PRD D17: Re-check in a bounded loop; cap in configuration prevents endless edits."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    # Set cap to 3 for test
    monkeypatch.setattr("apps.api.edits.max_edit_loops", lambda: 3)

    # Simulate 3 previous edits
    for i in range(1, 4):
        mock_db.edits.append({
            "edit_id": i,
            "ad_id": "ad-edit-1",
            "batch_id": "batch-e1",
            "client_id": "client-alpha",
            "edited_by": uid,
            "reason": f"Edit {i}",
            "previous_content": "{}",
            "new_content": "{}",
            "previous_row_hash": f"h{i-1}",
            "new_row_hash": f"h{i}",
            "edit_iteration": i,
            "created_at": datetime.now(timezone.utc),
        })

    # 4th edit attempt must exceed cap and fail with 422
    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-edit-1/edit",
            json={
                "headline": "Fourth Edit",
                "reason": "Exceeding cap",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 422
    assert "maximum edit loop cap (3) reached" in res.json()["detail"]


# --- Tests: Get Ad Edits History ---

def test_get_ad_edits_history_endpoint(fake_session, mock_db, monkeypatch):
    """Verify GET /ads/{ad_id}/edits returns full edit history with reasons and iterations."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    # Perform two edits
    with TestClient(app) as client:
        res1 = client.post(
            "/ads/ad-edit-1/edit",
            json={"headline": "Edit One", "reason": "First fix"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res1.status_code == 200

        res2 = client.post(
            "/ads/ad-edit-1/edit",
            json={"headline": "Edit Two", "reason": "Second fix"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res2.status_code == 200

        res_history = client.get(
            "/ads/ad-edit-1/edits",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res_history.status_code == 200
    data = res_history.json()
    assert data["ad_id"] == "ad-edit-1"
    assert data["total_edits"] == 2
    assert len(data["edits"]) == 2
    assert data["edits"][0]["reason"] == "First fix"
    assert data["edits"][0]["edit_iteration"] == 1
    assert data["edits"][1]["reason"] == "Second fix"
    assert data["edits"][1]["edit_iteration"] == 2


# --- Opt-in Real Database Integration Test ---

@pytest.mark.skipif(
    not os.environ.get("RUN_DATABASE_TESTS"),
    reason="Opt-in database integration tests require RUN_DATABASE_TESTS=1",
)
def test_real_database_ad_edits_table():
    """Verify ad_edits table creation and row persistence in real PostgreSQL."""
    with get_session() as conn:
        create_ad_edits_table()
        conn.execute("INSERT INTO clients (client_id, name) VALUES ('c-edit-test', 'Client Edit') ON CONFLICT DO NOTHING;")
        conn.execute(
            "INSERT INTO batches (batch_id, uploaded_by, filename, sheet_hash) VALUES ('b-edit-test', 'u-edit', 'edit.csv', 'shash-edit') ON CONFLICT DO NOTHING;"
        )
        conn.execute(
            """
            INSERT INTO ads (ad_id, batch_id, client_id, row_number, platform, headline, ad_text, destination_url, row_hash, sheet_hash)
            VALUES ('ad-edit-real', 'b-edit-test', 'c-edit-test', 1, 'facebook', 'Real Head', 'Real Text', 'https://c.test', 'rhash-edit-real', 'shash-edit')
            ON CONFLICT DO NOTHING;
            """
        )
        history = get_ad_edits_history(conn, "ad-edit-real")
        assert isinstance(history, list)
