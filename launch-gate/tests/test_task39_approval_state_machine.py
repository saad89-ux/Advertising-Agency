"""Tests for Task 39: Approval state machine (DB-enforced transitions, assignment check).

Verifies the approval state machine, database-enforced transitions,
client assignment checks, content hash locking (PRD D13), rejection reasons (PRD 4.6),
and audit history per PRD 4.6, 4.8 and Dataflow Architecture §3.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api import auth, main, users
from apps.api.approvals import (
    ALLOWED_TRANSITIONS,
    ApprovalState,
    ContentHashMismatchError,
    InvalidTransitionError,
    create_approval_tables,
    is_valid_transition,
    transition_ad_state,
    validate_transition,
)
from apps.api.auth import CurrentUser
from apps.api.database import get_session
from apps.api.main import app


# --- Mock / Fake Database Storage ---

class MockDB:
    def __init__(self):
        self.ads = {
            "ad-alpha-1": {
                "ad_id": "ad-alpha-1",
                "batch_id": "batch-1",
                "client_id": "client-alpha",
                "row_number": 1,
                "platform": "facebook",
                "ad_text": "Alpha Copy 1",
                "headline": "Alpha Headline 1",
                "destination_url": "https://alpha.example/offer?utm_source=fb",
                "row_hash": "hash-alpha-1",
                "sheet_hash": "sheet-hash-1",
            },
            "ad-beta-1": {
                "ad_id": "ad-beta-1",
                "batch_id": "batch-1",
                "client_id": "client-beta",
                "row_number": 2,
                "platform": "google",
                "ad_text": "Beta Copy 1",
                "headline": "Beta Headline 1",
                "destination_url": "https://beta.example/landing",
                "row_hash": "hash-beta-1",
                "sheet_hash": "sheet-hash-1",
            },
        }
        self.approvals = {
            "ad-alpha-1": {
                "ad_id": "ad-alpha-1",
                "client_id": "client-alpha",
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
            "ad-beta-1": {
                "ad_id": "ad-beta-1",
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
        self.history = []
        self.executed_statements = []


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
            self.db.executed_statements.append((s, params))
            params_tuple = tuple(params) if params is not None else ()

            # 1. Query ad record
            if "FROM ads" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                ad = self.db.ads.get(aid)
                if ad:
                    return FakeResult([(
                        ad["ad_id"], ad["batch_id"], ad["client_id"], ad["row_number"],
                        ad["platform"], ad["ad_text"], ad["headline"], ad["destination_url"],
                        ad["row_hash"], ad["sheet_hash"]
                    )])
                return FakeResult([])

            # 2. Query ad_approvals
            if "FROM ad_approvals" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                appr = self.db.approvals.get(aid)
                if appr:
                    return FakeResult([(
                        appr["ad_id"], appr["client_id"], appr["state"], appr["content_hash"],
                        appr["approved_by"], appr["approved_at"], appr["evidence_note"],
                        appr["evidence_file"], appr["rejection_reason"], appr["rejected_by"],
                        appr["rejected_at"], appr["idempotency_key"], appr["send_attempts"],
                        appr["sent_at"], appr["created_at"], appr["updated_at"]
                    )])
                return FakeResult([])

            # 3. Insert ad_approvals
            if "INSERT INTO ad_approvals" in s:
                aid, cid = params_tuple[:2]
                if aid not in self.db.approvals:
                    self.db.approvals[aid] = {
                        "ad_id": aid,
                        "client_id": cid,
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
                    }
                return FakeResult([])

            # 4. Update ad_approvals
            if "UPDATE ad_approvals" in s:
                if "SET state = %s" in s:
                    # Update state
                    to_state = params_tuple[0]
                    aid = params_tuple[-1]
                    appr = self.db.approvals.get(aid)
                    if appr:
                        from_state = appr["state"]
                        # Validate transition
                        validate_transition(from_state, to_state)
                        appr["state"] = to_state
                        appr["updated_at"] = datetime.now(timezone.utc)

                        if to_state == "approved":
                            appr["content_hash"] = params_tuple[1]
                            appr["approved_by"] = params_tuple[2]
                            appr["approved_at"] = params_tuple[3]
                            if len(params_tuple) > 5 and params_tuple[4]:
                                appr["evidence_note"] = params_tuple[4]
                            if len(params_tuple) > 6 and params_tuple[5]:
                                appr["evidence_file"] = params_tuple[5]
                        elif to_state == "rejected":
                            appr["rejection_reason"] = params_tuple[1]
                            appr["rejected_by"] = params_tuple[2]
                            appr["rejected_at"] = params_tuple[3]
                        elif to_state == "sending":
                            appr["idempotency_key"] = params_tuple[1]
                            appr["send_attempts"] += 1
                        elif to_state == "sent":
                            appr["sent_at"] = params_tuple[1]
                return FakeResult([])

            # 5. Insert approval_history
            if "INSERT INTO approval_history" in s:
                aid, f_st, t_st, changed_by, reason, c_hash, idemp = params_tuple
                self.db.history.append({
                    "history_id": len(self.db.history) + 1,
                    "ad_id": aid,
                    "from_state": f_st,
                    "to_state": t_st,
                    "changed_by": changed_by,
                    "reason": reason,
                    "content_hash": c_hash,
                    "idempotency_key": idemp,
                    "created_at": datetime.now(timezone.utc),
                })
                return FakeResult([])

            # 6. Query approval_history
            if "FROM approval_history" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                matching = [h for h in self.db.history if h["ad_id"] == aid]
                return FakeResult([
                    (
                        h["history_id"], h["ad_id"], h["from_state"], h["to_state"],
                        h["changed_by"], h["reason"], h["content_hash"],
                        h["idempotency_key"], h["created_at"]
                    )
                    for h in matching
                ])

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

    monkeypatch.setattr("apps.api.approvals.get_session", _session)
    monkeypatch.setattr("apps.api.ads.get_session", _session)
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
    uid = "test-user-id"
    record = {
        "user_id": uid,
        "email": "tester@example.test",
        "display_name": "Test Reviewer",
        "role": role,
        "is_active": True,
    }
    monkeypatch.setattr(users, "get_user_by_id", lambda u: record if u == uid else None)
    monkeypatch.setattr(users, "get_user_client_ids", lambda u: assigned_clients if u == uid else [])
    return uid


# --- Tests: State Machine Transition Rules ---

def test_transition_rules_match_specification():
    """Verify state transitions exactly match Dataflow Architecture §3."""
    # Valid transitions
    assert is_valid_transition("pending_review", "approved")
    assert is_valid_transition("pending_review", "rejected")
    assert is_valid_transition("approved", "sending")
    assert is_valid_transition("sending", "sent")
    assert is_valid_transition("sending", "approved")  # retry with backoff
    assert is_valid_transition("sending", "dead_letter")  # retries exhausted
    assert is_valid_transition("rejected", "pending_review")  # re-check after edit
    assert is_valid_transition("approved", "pending_review")  # re-check after edit
    assert is_valid_transition("approved", "rejected")  # reviewer changes mind before sending

    # Invalid transitions
    assert not is_valid_transition("pending_review", "sent")  # No ad sent without approval
    assert not is_valid_transition("pending_review", "sending")
    assert not is_valid_transition("rejected", "sending")
    assert not is_valid_transition("rejected", "sent")
    assert not is_valid_transition("sent", "approved")
    assert not is_valid_transition("sent", "rejected")
    assert not is_valid_transition("sent", "pending_review")
    assert not is_valid_transition("dead_letter", "sent")


def test_validate_transition_raises_on_invalid():
    """Verify validate_transition raises InvalidTransitionError for illegal moves."""
    validate_transition("pending_review", "approved")  # Should not raise

    with pytest.raises(InvalidTransitionError, match="Invalid approval state transition"):
        validate_transition("pending_review", "sent")

    with pytest.raises(InvalidTransitionError, match="Invalid approval state transition"):
        validate_transition("sent", "approved")


# --- Tests: Schema and Table Setup ---

def test_create_approval_tables_ddl(monkeypatch: pytest.MonkeyPatch):
    """Verify create_approval_tables issues DDL for all required tables and seeds transitions."""
    statements = []

    class FakeConn:
        def execute(self, s, params=None):
            statements.append((s, params))

    @contextmanager
    def _fake_session():
        yield FakeConn()

    monkeypatch.setattr("apps.api.approvals.get_session", _fake_session)
    monkeypatch.setattr("apps.api.approvals.create_ads_table", lambda: None)

    create_approval_tables()

    flat_sql = " ".join(s[0] for s in statements)
    assert "CREATE TABLE IF NOT EXISTS allowed_approval_transitions" in flat_sql
    assert "CREATE TABLE IF NOT EXISTS ad_approvals" in flat_sql
    assert "CREATE TABLE IF NOT EXISTS approval_history" in flat_sql
    assert "enforce_approval_state_transition" in flat_sql

    # Verify allowed transitions are populated
    inserted_transitions = {
        params for sql, params in statements if "INSERT INTO allowed_approval_transitions" in sql and params
    }
    assert ("pending_review", "approved") in inserted_transitions
    assert ("approved", "sending") in inserted_transitions
    assert ("sending", "sent") in inserted_transitions


# --- Tests: Approve API & Client Assignment (Needs: 7) ---

def test_approve_ad_success(fake_session, mock_db, monkeypatch):
    """Verify assigned buyer can approve an ad, recording content_hash and history."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-alpha-1/approve",
            json={"content_hash": "hash-alpha-1", "evidence_note": "Approved via call with Tobias"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["ad_id"] == "ad-alpha-1"
    assert data["state"] == "approved"
    assert data["content_hash"] == "hash-alpha-1"
    assert data["approved_by"] == uid
    assert data["evidence_note"] == "Approved via call with Tobias"
    assert data["approved_at"] is not None

    # Verify audit history
    assert len(mock_db.history) == 1
    h = mock_db.history[0]
    assert h["ad_id"] == "ad-alpha-1"
    assert h["from_state"] == "pending_review"
    assert h["to_state"] == "approved"
    assert h["changed_by"] == uid
    assert h["content_hash"] == "hash-alpha-1"


def test_approve_ad_client_assignment_enforced(fake_session, mock_db, monkeypatch):
    """Verify buyer assigned to Client A CANNOT approve Client B ad -> 403 Forbidden."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-beta-1/approve",
            json={"content_hash": "hash-beta-1"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 403
    assert res.json() == {"detail": "forbidden"}
    # Verify state remains pending_review
    assert mock_db.approvals["ad-beta-1"]["state"] == "pending_review"


def test_approve_ad_requires_authentication():
    """Verify unauthenticated approve request returns 401."""
    with TestClient(app) as client:
        res = client.post("/ads/ad-alpha-1/approve", json={})
    assert res.status_code == 401


def test_approve_ad_not_found(fake_session, monkeypatch):
    """Verify approving a non-existent ad returns 404."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/non-existent-ad/approve",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 404
    assert res.json() == {"detail": "ad not found"}


def test_approve_ad_content_hash_mismatch(fake_session, mock_db, monkeypatch):
    """Verify approving with an altered/mismatched content hash returns 409 Conflict (PRD D13)."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-alpha-1/approve",
            json={"content_hash": "tampered-hash-does-not-match"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 409
    assert "content hash mismatch" in res.json()["detail"]
    assert mock_db.approvals["ad-alpha-1"]["state"] == "pending_review"


def test_approve_ad_idempotency_and_second_click(fake_session, mock_db, monkeypatch):
    """Verify second click on approve with matching hash succeeds idempotently without duplicate history."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        # First approve click
        res1 = client.post(
            "/ads/ad-alpha-1/approve",
            json={"content_hash": "hash-alpha-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res1.status_code == 200

        # Second approve click
        res2 = client.post(
            "/ads/ad-alpha-1/approve",
            json={"content_hash": "hash-alpha-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res2.status_code == 200
        assert res2.json()["state"] == "approved"

    # Only 1 history record should have been appended
    assert len(mock_db.history) == 1


def test_approve_ad_in_sending_or_sent_state_does_nothing(fake_session, mock_db, monkeypatch):
    """Per PRD §3: A second click on Approve finds the row already sending or sent and does nothing."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    # Manually transition to sending
    mock_db.approvals["ad-alpha-1"]["state"] = "sending"

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-alpha-1/approve",
            json={"content_hash": "hash-alpha-1"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    assert res.json()["state"] == "sending"


# --- Tests: Reject API & Validation ---

def test_reject_ad_success(fake_session, mock_db, monkeypatch):
    """Verify assigned buyer can reject an ad with reason, updating audit history."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-alpha-1/reject",
            json={"reason": "Contains unsubstantiated superlative claim 'best in the world'."},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["ad_id"] == "ad-alpha-1"
    assert data["state"] == "rejected"
    assert data["rejection_reason"] == "Contains unsubstantiated superlative claim 'best in the world'."
    assert data["rejected_by"] == uid
    assert data["rejected_at"] is not None

    # Verify audit history
    assert len(mock_db.history) == 1
    h = mock_db.history[0]
    assert h["from_state"] == "pending_review"
    assert h["to_state"] == "rejected"
    assert h["reason"] == "Contains unsubstantiated superlative claim 'best in the world'."


def test_reject_ad_missing_reason_rejected(fake_session, mock_db, monkeypatch):
    """Verify rejection without a non-empty reason returns 422."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        # Empty string
        res = client.post(
            "/ads/ad-alpha-1/reject",
            json={"reason": "   "},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 422
    assert "rejection reason is required" in res.json()["detail"]


def test_reject_ad_client_assignment_enforced(fake_session, mock_db, monkeypatch):
    """Verify rejection on unassigned client ad returns 403 Forbidden."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-beta-1/reject",
            json={"reason": "Rejection note"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 403
    assert res.json() == {"detail": "forbidden"}


def test_reject_ad_already_sent_returns_conflict(fake_session, mock_db, monkeypatch):
    """Verify cannot reject an ad already in sending or sent state."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    mock_db.approvals["ad-alpha-1"]["state"] = "sent"

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-alpha-1/reject",
            json={"reason": "Too late"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 409
    assert "cannot reject ad in 'sent' state" in res.json()["detail"]


# --- Tests: Get Approval Status & History ---

def test_get_ad_approval_status_and_history(fake_session, mock_db, monkeypatch):
    """Verify GET /ads/{ad_id}/approval returns record and transition history."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        # Approve first
        client.post(
            "/ads/ad-alpha-1/approve",
            json={"content_hash": "hash-alpha-1"},
            headers={"Authorization": f"Bearer {token}"},
        )

        res = client.get(
            "/ads/ad-alpha-1/approval",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["ad_id"] == "ad-alpha-1"
    assert data["state"] == "approved"
    assert "history" in data
    assert len(data["history"]) == 1
    assert data["history"][0]["to_state"] == "approved"


def test_get_ad_approval_status_forbidden_for_unassigned_client(fake_session, monkeypatch):
    """Verify viewing approval of unassigned client returns 403 Forbidden."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.get(
            "/ads/ad-beta-1/approval",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 403


# --- Tests: Role Verification (Account Manager can approve per PRD D2) ---

def test_account_manager_role_can_approve(fake_session, mock_db, monkeypatch):
    """PRD D2: Backup reviewer is account manager. Verify account_manager can approve."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="account_manager")
    token = make_token(uid, "account_manager")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-alpha-1/approve",
            json={"content_hash": "hash-alpha-1"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    assert res.json()["state"] == "approved"


# --- Tests: Programmatic Sender Worker Transitions ---

def test_programmatic_transitions_to_sending_and_sent(fake_session, mock_db):
    """Verify transitions used by the sender worker: approved -> sending -> sent."""
    # Start as approved
    mock_db.approvals["ad-alpha-1"]["state"] = "approved"

    # Transition to sending with idempotency key
    res_sending = transition_ad_state(
        fake_session,
        ad_id="ad-alpha-1",
        to_state="sending",
        idempotency_key="idemp-key-123",
    )
    assert res_sending["state"] == "sending"
    assert res_sending["idempotency_key"] == "idemp-key-123"
    assert res_sending["send_attempts"] == 1

    # Transition to sent
    res_sent = transition_ad_state(
        fake_session,
        ad_id="ad-alpha-1",
        to_state="sent",
    )
    assert res_sent["state"] == "sent"
    assert res_sent["sent_at"] is not None


def test_programmatic_timeout_retry_transition(fake_session, mock_db):
    """Verify sending -> approved transition for timeouts with backoff."""
    mock_db.approvals["ad-alpha-1"]["state"] = "sending"

    res_retry = transition_ad_state(
        fake_session,
        ad_id="ad-alpha-1",
        to_state="approved",
        reason="timeout retry backoff",
    )
    assert res_retry["state"] == "approved"


def test_programmatic_dead_letter_transition(fake_session, mock_db):
    """Verify sending -> dead_letter transition when max retries are exhausted."""
    mock_db.approvals["ad-alpha-1"]["state"] = "sending"

    res_dl = transition_ad_state(
        fake_session,
        ad_id="ad-alpha-1",
        to_state="dead_letter",
        reason="exhausted 5 retries",
    )
    assert res_dl["state"] == "dead_letter"


# --- Opt-in Real Database Integration Test ---

@pytest.mark.skipif(
    not os.environ.get("RUN_DATABASE_TESTS"),
    reason="Opt-in database integration tests require RUN_DATABASE_TESTS=1",
)
def test_real_database_enforces_transitions():
    """Verify that PostgreSQL itself raises check_violation on illegal transition via trigger."""
    with get_session() as conn:
        create_approval_tables()
        # Seed test client, batch, ad
        conn.execute("INSERT INTO clients (client_id, name) VALUES ('c-test', 'Client Test') ON CONFLICT DO NOTHING;")
        conn.execute(
            "INSERT INTO batches (batch_id, uploaded_by, filename, sheet_hash) VALUES ('b-test', 'u-test', 'file.csv', 'shash') ON CONFLICT DO NOTHING;"
        )
        conn.execute(
            """
            INSERT INTO ads (ad_id, batch_id, client_id, row_number, platform, destination_url, row_hash, sheet_hash)
            VALUES ('ad-trg-test', 'b-test', 'c-test', 1, 'facebook', 'https://c.test', 'rhash', 'shash')
            ON CONFLICT DO NOTHING;
            """
        )
        # Initialize approval
        conn.execute(
            "INSERT INTO ad_approvals (ad_id, client_id, state) VALUES ('ad-trg-test', 'c-test', 'pending_review') ON CONFLICT (ad_id) DO UPDATE SET state = 'pending_review';"
        )

        # Illegal direct jump: pending_review -> sent
        with pytest.raises(Exception) as exc_info:
            conn.execute("UPDATE ad_approvals SET state = 'sent' WHERE ad_id = 'ad-trg-test';")
        assert "Invalid approval state transition" in str(exc_info.value)
