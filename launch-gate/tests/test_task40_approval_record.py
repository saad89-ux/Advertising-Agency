"""Tests for Task 40: Approval record + content hash. Needs: 39.

Verifies the immutable approval record model, content hash binding per PRD D13,
content divergence detection (preventing "approved one, launched the other"),
evidence linking per PRD D3, and pre-send verification.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
import pytest
from fastapi.testclient import TestClient

from apps.api import auth, main, users
from apps.api.approvals import (
    ApprovalState,
    build_canonical_content_snapshot,
    compute_canonical_content_hash,
    get_ad_approval_record,
    verify_approval_for_send,
)
from apps.api.database import get_session
from apps.api.main import app


# --- Mock Database Setup ---

class MockDB:
    def __init__(self):
        self.ads = {
            "ad-101": {
                "ad_id": "ad-101",
                "batch_id": "batch-100",
                "client_id": "client-alpha",
                "row_number": 1,
                "platform": "facebook",
                "platform_raw": "FB",
                "ad_text": "Original approved ad copy",
                "headline": "Original Headline",
                "destination_url": "https://alpha.example/offer?utm_source=fb",
                "row_hash": "hash-original-101",
                "sheet_hash": "sheet-hash-100",
                "creative_file": "banner.png",
            },
            "ad-102": {
                "ad_id": "ad-102",
                "batch_id": "batch-100",
                "client_id": "client-beta",
                "row_number": 2,
                "platform": "google",
                "platform_raw": "Google Ads",
                "ad_text": "Beta ad text",
                "headline": "Beta Headline",
                "destination_url": "https://beta.example/landing",
                "row_hash": "hash-beta-102",
                "sheet_hash": "sheet-hash-100",
                "creative_file": "",
            },
        }
        self.approvals = {
            "ad-101": {
                "ad_id": "ad-101",
                "client_id": "client-alpha",
                "state": "pending_review",
                "content_hash": None,
                "approved_by": None,
                "approved_at": None,
                "evidence_note": None,
                "evidence_file": None,
                "content_snapshot": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": None,
                "send_attempts": 0,
                "sent_at": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            "ad-102": {
                "ad_id": "ad-102",
                "client_id": "client-beta",
                "state": "pending_review",
                "content_hash": None,
                "approved_by": None,
                "approved_at": None,
                "evidence_note": None,
                "evidence_file": None,
                "content_snapshot": None,
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
                        appr["sent_at"], appr["created_at"], appr["updated_at"],
                        appr.get("content_snapshot")
                    )])
                return FakeResult([])

            # 3. Update ad_approvals
            if "UPDATE ad_approvals" in s:
                to_state = params_tuple[0]
                aid = params_tuple[-1]
                appr = self.db.approvals.get(aid)
                if appr:
                    appr["state"] = to_state
                    appr["updated_at"] = datetime.now(timezone.utc)
                    if to_state == "approved":
                        appr["content_hash"] = params_tuple[1]
                        appr["approved_by"] = params_tuple[2]
                        appr["approved_at"] = params_tuple[3]
                        if len(params_tuple) > 4 and params_tuple[4]:
                            appr["evidence_note"] = params_tuple[4]
                        if len(params_tuple) > 5 and params_tuple[5]:
                            appr["evidence_file"] = params_tuple[5]
                        if len(params_tuple) > 6 and params_tuple[6]:
                            appr["content_snapshot"] = params_tuple[6]
                    elif to_state == "rejected":
                        appr["rejection_reason"] = params_tuple[1]
                        appr["rejected_by"] = params_tuple[2]
                        appr["rejected_at"] = params_tuple[3]
                return FakeResult([])

            # 4. Insert approval_history
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
    uid = "test-buyer-uuid"
    record = {
        "user_id": uid,
        "email": "buyer@example.test",
        "display_name": "Test Reviewer",
        "role": role,
        "is_active": True,
    }
    monkeypatch.setattr(users, "get_user_by_id", lambda u: record if u == uid else None)
    monkeypatch.setattr(users, "get_user_client_ids", lambda u: assigned_clients if u == uid else [])
    return uid


# --- Tests: Canonical Content Hashing (PRD D13, D23) ---

def test_canonical_content_hash_covers_canonical_fields_only():
    """Verify hash covers platform, text, headline, link, creative and ignores extras."""
    ad_data = {
        "platform": "facebook",
        "headline": "Super Offer",
        "ad_text": "Great discount today only",
        "destination_url": "https://example.com/deal",
        "creative_file": "deal.jpg",
        "extra_custom_column": "ignored metadata",
    }
    snapshot = build_canonical_content_snapshot(ad_data)
    assert "extra_custom_column" not in snapshot
    assert snapshot["platform"] == "facebook"
    assert snapshot["headline"] == "Super Offer"

    hash_val = compute_canonical_content_hash(ad_data)
    assert isinstance(hash_val, str)
    assert len(hash_val) > 0


# --- Tests: Approval Record Creation & Retrieval ---

def test_get_approval_record_unapproved_state(fake_session, mock_db, monkeypatch):
    """Verify approval record for pending ad reports is_approved=False and is_valid_for_send=False."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.get(
            "/ads/ad-101/approval-record",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["ad_id"] == "ad-101"
    assert data["client_id"] == "client-alpha"
    assert data["batch_id"] == "batch-100"
    assert data["state"] == "pending_review"
    assert data["is_approved"] is False
    assert data["is_valid_for_send"] is False
    assert data["content_hash"] is None


def test_approve_locks_content_hash_and_creates_approval_record(fake_session, mock_db, monkeypatch):
    """Verify approval locks exact content hash and stores snapshot per PRD D13 & D3."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-101/approve",
            json={
                "content_hash": "hash-original-101",
                "evidence_note": "Approved by client via Slack screenshot",
                "evidence_file": "screenshot_slack_20261014.png",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["ad_id"] == "ad-101"
    assert data["state"] == "approved"
    assert data["content_hash"] == "hash-original-101"
    assert data["is_approved"] is True
    assert data["is_valid_for_send"] is True
    assert data["evidence_note"] == "Approved by client via Slack screenshot"
    assert data["evidence_file"] == "screenshot_slack_20261014.png"
    assert data["content_snapshot"] is not None
    assert data["content_snapshot"]["headline"] == "Original Headline"


# --- Tests: Content Divergence / Tampering Detection (PRD D13) ---

def test_content_divergence_blocks_send_validation(fake_session, mock_db, monkeypatch):
    """PRD D13: Fixes 'approved one, launched the other'.

    If ad content changes after approval (simulated by updated ad row_hash),
    is_valid_for_send must become False, and verify-send must reject with 409 Conflict.
    """
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        # 1. Approve original ad
        res_approve = client.post(
            "/ads/ad-101/approve",
            json={"content_hash": "hash-original-101"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_approve.status_code == 200

        # 2. Verify send is initially valid
        res_verify_ok = client.post(
            "/ads/ad-101/verify-send",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_verify_ok.status_code == 200
        assert res_verify_ok.json()["ready_to_send"] is True

        # 3. Simulate ad modification after approval (e.g. ad text or headline edited)
        mock_db.ads["ad-101"]["ad_text"] = "Sneakily modified text after approval"
        mock_db.ads["ad-101"]["row_hash"] = "hash-modified-different-101"

        # 4. Check approval-record reflects divergence
        res_rec = client.get(
            "/ads/ad-101/approval-record",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_rec.status_code == 200
        rec_data = res_rec.json()
        assert rec_data["content_hash"] == "hash-original-101"
        assert rec_data["current_ad_hash"] == "hash-modified-different-101"
        assert rec_data["is_valid_for_send"] is False  # DIVERGENCE DETECTED!

        # 5. verify-send must FAIL with 409 Conflict
        res_verify_tampered = client.post(
            "/ads/ad-101/verify-send",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_verify_tampered.status_code == 409
        err_msg = res_verify_tampered.json()["detail"]
        assert "content hash mismatch" in err_msg
        assert "Only the approved version can be sent" in err_msg


# --- Tests: Pre-Send Verification for Unapproved & Rejected Ads ---

def test_verify_send_fails_for_unapproved_ad(fake_session, mock_db, monkeypatch):
    """Verify attempting to send a pending_review ad returns 400 Bad Request."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-101/verify-send",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 400
    assert "approval is required" in res.json()["detail"]


def test_verify_send_fails_for_rejected_ad(fake_session, mock_db, monkeypatch):
    """Verify attempting to send a rejected ad returns 400 Bad Request."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    mock_db.approvals["ad-101"]["state"] = "rejected"
    mock_db.approvals["ad-101"]["rejection_reason"] = "Bad copy"

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-101/verify-send",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 400
    assert "cannot send ad with approval state 'rejected'" in res.json()["detail"]


def test_verify_send_forbidden_for_unassigned_client(fake_session, mock_db, monkeypatch):
    """Verify buyer assigned to Client A cannot verify or send Client B ad."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        res = client.post(
            "/ads/ad-102/verify-send",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 403
    assert res.json() == {"detail": "forbidden"}


# --- Tests: Evidence Linking (PRD D3) ---

def test_evidence_note_and_file_preserved_in_approval_record(fake_session, mock_db, monkeypatch):
    """PRD D3: Reviewer attaches note or screenshot linked to content hash."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, "buyer")

    with TestClient(app) as client:
        client.post(
            "/ads/ad-101/approve",
            json={
                "content_hash": "hash-original-101",
                "evidence_note": "Client verbally approved on 2026-10-14 call",
                "evidence_file": "storage/evidence/call_recording_101.mp3",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        res = client.get(
            "/ads/ad-101/approval-record",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["evidence_note"] == "Client verbally approved on 2026-10-14 call"
    assert data["evidence_file"] == "storage/evidence/call_recording_101.mp3"
    assert data["content_hash"] == "hash-original-101"


# --- Opt-in Real Database Integration Test ---

@pytest.mark.skipif(
    not os.environ.get("RUN_DATABASE_TESTS"),
    reason="Opt-in database integration tests require RUN_DATABASE_TESTS=1",
)
def test_real_database_approval_record_persistence():
    """Verify approval record persistence with content snapshot in real PostgreSQL."""
    with get_session() as conn:
        from apps.api.approvals import create_approval_tables
        create_approval_tables()
        conn.execute("INSERT INTO clients (client_id, name) VALUES ('c-rec-test', 'Client Rec Test') ON CONFLICT DO NOTHING;")
        conn.execute(
            "INSERT INTO batches (batch_id, uploaded_by, filename, sheet_hash) VALUES ('b-rec-test', 'u-rec', 'rec.csv', 'shash-rec') ON CONFLICT DO NOTHING;"
        )
        conn.execute(
            """
            INSERT INTO ads (ad_id, batch_id, client_id, row_number, platform, headline, ad_text, destination_url, row_hash, sheet_hash)
            VALUES ('ad-rec-real', 'b-rec-test', 'c-rec-test', 1, 'facebook', 'Real Head', 'Real Text', 'https://c.test', 'rhash-real', 'shash-rec')
            ON CONFLICT DO NOTHING;
            """
        )
        record = get_ad_approval_record(conn, "ad-rec-real")
        assert record["ad_id"] == "ad-rec-real"
        assert record["state"] == "pending_review"
        assert record["is_approved"] is False
