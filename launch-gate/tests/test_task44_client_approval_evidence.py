"""Tests for Task 44: Client approval evidence (note/screenshot, validated, linked to hash). Needs: 21, 40.

Verifies:
1. Reviewer attaches client approval evidence (note, screenshot image, or PDF document) per PRD D3.
2. At least a note or file must be provided; blank note with no file is rejected (HTTP 422).
3. File types restricted to images (PNG, JPEG, WEBP) and PDF; executables or oversized files rejected (Needs: 21).
4. Evidence is stored via LocalFileStore returning opaque 32-hex root-confined references (Needs: 21).
5. Evidence is validated against and bound to the ad's canonical content hash (Needs: 40);
   mismatched content hash rejected with HTTP 409 Conflict.
6. If an ad's content changes after evidence was attached, evidence indicates content divergence.
7. Scoping and role checks enforced: only assigned buyers or account managers can attach evidence (Needs: 7; HTTP 403).
8. Terminal state protection: cannot attach evidence to ads in 'sending' or 'sent' state (HTTP 409).
9. Retrieval endpoints for evidence metadata and binary file streaming.
10. UI components render evidence controls, preview, badges, and upload inputs.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import pytest
from fastapi.testclient import TestClient

from apps.api import auth, main, users
from apps.api.database import get_session
from apps.api.evidence import (
    CREATE_APPROVAL_EVIDENCE_TABLE,
    create_approval_evidence_table,
    record_approval_evidence,
    get_ad_evidence_history,
)
from apps.api.main import app
from apps.worker.file_storage import LocalFileStore


# --- Mock Database Setup ---

class MockDB:
    def __init__(self):
        self.ads = {
            "ad-ev-1": {
                "ad_id": "ad-ev-1",
                "batch_id": "batch-ev",
                "client_id": "client-alpha",
                "row_number": 1,
                "platform": "facebook",
                "platform_raw": "Facebook Feed",
                "ad_text": "Great summer promotion copy",
                "headline": "Summer Promotion",
                "destination_url": "https://alpha.example/offer?utm_source=fb",
                "row_hash": "hash-alpha-canonical-1",
                "sheet_hash": "sheet-ev-1",
                "creative_file": "banner.png",
            },
            "ad-ev-beta": {
                "ad_id": "ad-ev-beta",
                "batch_id": "batch-ev",
                "client_id": "client-beta",
                "row_number": 2,
                "platform": "google",
                "platform_raw": "Google Search",
                "ad_text": "Beta ad text",
                "headline": "Beta Headline",
                "destination_url": "https://beta.example",
                "row_hash": "hash-beta-canonical-1",
                "sheet_hash": "sheet-ev-1",
                "creative_file": "",
            },
        }
        self.batches = {
            "batch-ev": {
                "batch_id": "batch-ev",
                "filename": "evidence_batch.xlsx",
                "uploaded_by": "uploader-1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sheet_hash": "sheet-ev-1",
            }
        }
        self.approvals = {
            "ad-ev-1": {
                "ad_id": "ad-ev-1",
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
            "ad-ev-beta": {
                "ad_id": "ad-ev-beta",
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
        self.evidence = []


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
def fake_session(mock_db: MockDB, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_DIR", str(storage_root))

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
                aid = params_tuple[-1]
                appr = self.db.approvals.get(aid)
                if appr:
                    if "SET evidence_note" in s:
                        appr["evidence_note"] = params_tuple[0]
                        appr["evidence_file"] = params_tuple[1]
                    elif "SET state" in s:
                        appr["state"] = params_tuple[0]
                        if len(params_tuple) > 2:
                            appr["content_hash"] = params_tuple[1]
                    appr["updated_at"] = datetime.now(timezone.utc)
                return FakeResult([])

            # 4. Insert approval_evidence
            if "INSERT INTO approval_evidence" in s:
                aid, cid, c_hash, note, f_ref, f_name, f_size, mime, created_by, created_at = params_tuple
                ev_id = len(self.db.evidence) + 1
                ev = {
                    "evidence_id": ev_id,
                    "ad_id": aid,
                    "client_id": cid,
                    "content_hash": c_hash,
                    "evidence_note": note,
                    "evidence_file": f_ref,
                    "file_name": f_name,
                    "file_size": f_size,
                    "mime_type": mime,
                    "created_by": created_by,
                    "created_at": created_at,
                }
                self.db.evidence.append(ev)
                if "RETURNING" in s:
                    return FakeResult([(ev_id, created_at)])
                return FakeResult([])

            # 5. Query mime_type for file download
            if "SELECT mime_type" in s:
                aid, f_ref = params_tuple
                for e in self.db.evidence:
                    if e["ad_id"] == aid and e["evidence_file"] == f_ref:
                        return FakeResult([(e["mime_type"],)])
                return FakeResult([("application/octet-stream",)])

            # 6. Query approval_evidence by ad_id
            if "FROM approval_evidence" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                rows = [
                    (
                        e["evidence_id"], e["ad_id"], e["client_id"], e["content_hash"],
                        e["evidence_note"], e["evidence_file"], e["file_name"], e["file_size"],
                        e["mime_type"], e["created_by"], e["created_at"]
                    )
                    for e in reversed(self.db.evidence)
                    if e["ad_id"] == aid
                ]
                return FakeResult(rows)

            # 7. Batch ads query
            if "SELECT COUNT(*)" in s and "FROM ads" in s:
                bid = params_tuple[0]
                cnt = sum(1 for a in self.db.ads.values() if a["batch_id"] == bid)
                return FakeResult([(cnt,)])

            if "FROM ads" in s and "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                client_filter = params_tuple[1] if len(params_tuple) > 1 else None
                matched = []
                for a in self.db.ads.values():
                    if a["batch_id"] == bid:
                        if client_filter is None or a["client_id"] in client_filter:
                            matched.append((
                                a["ad_id"], a["batch_id"], a["client_id"], a["row_number"],
                                a["platform"], a.get("platform_raw", a["platform"]),
                                a.get("ad_text"), a.get("headline"), a["destination_url"],
                                a.get("creative_file"), a["row_hash"], a["sheet_hash"]
                            ))
                matched.sort(key=lambda x: x[3])
                return FakeResult(matched)

            if "FROM batches" in s and "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                b = self.db.batches.get(bid)
                if b:
                    return FakeResult([(
                        b["batch_id"], b["filename"], b["uploaded_by"], b["created_at"], b["sheet_hash"]
                    )])
                return FakeResult([])

            if "SELECT ad_id, evidence_note, evidence_file, state FROM ad_approvals" in s:
                ad_ids = list(params_tuple[0])
                rows = []
                for aid in ad_ids:
                    appr = self.db.approvals.get(aid)
                    if appr:
                        rows.append((aid, appr["evidence_note"], appr["evidence_file"], appr["state"]))
                return FakeResult(rows)

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

    monkeypatch.setattr("apps.api.evidence.get_session", _session)
    monkeypatch.setattr("apps.api.approvals.get_session", _session)
    monkeypatch.setattr("apps.api.batches.get_session", _session)
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


def mock_users_helper(monkeypatch: pytest.MonkeyPatch, assigned_clients: list[str], role: str = "account_manager"):
    uid = "test-am-tobias"
    record = {
        "user_id": uid,
        "email": "tobias@agency.test",
        "display_name": "Tobias Account Manager",
        "role": role,
        "is_active": True,
    }
    monkeypatch.setattr(users, "get_user_by_id", lambda u: record if u == uid else None)
    monkeypatch.setattr(users, "get_user_client_ids", lambda u: assigned_clients if u == uid else [])
    return uid


# --- Tests: Schema & DDL ---

def test_create_approval_evidence_table_ddl():
    """Verify approval_evidence table DDL structures required columns and foreign keys."""
    assert "CREATE TABLE IF NOT EXISTS approval_evidence" in CREATE_APPROVAL_EVIDENCE_TABLE
    assert "ad_id TEXT NOT NULL REFERENCES ads" in CREATE_APPROVAL_EVIDENCE_TABLE
    assert "client_id TEXT NOT NULL REFERENCES clients" in CREATE_APPROVAL_EVIDENCE_TABLE
    assert "content_hash TEXT NOT NULL" in CREATE_APPROVAL_EVIDENCE_TABLE
    assert "evidence_note TEXT" in CREATE_APPROVAL_EVIDENCE_TABLE
    assert "evidence_file TEXT" in CREATE_APPROVAL_EVIDENCE_TABLE
    assert "file_name TEXT" in CREATE_APPROVAL_EVIDENCE_TABLE
    assert "created_by TEXT NOT NULL REFERENCES users" in CREATE_APPROVAL_EVIDENCE_TABLE


# --- Tests: Evidence Validation & Linkage ---

def test_attach_evidence_note_success_and_linked_to_hash(fake_session, mock_db, monkeypatch):
    """Reviewer attaches a note linked to content hash (PRD D3)."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="account_manager")
    token = make_token(uid, role="account_manager")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-ev-1/evidence",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "evidence_note": "Client confirmed approval on Zoom call with Sarah at 11:00 AM.",
            "content_hash": "hash-alpha-canonical-1",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ad_id"] == "ad-ev-1"
    assert data["content_hash"] == "hash-alpha-canonical-1"
    assert data["content_hash_matches"] is True
    assert data["evidence_note"] == "Client confirmed approval on Zoom call with Sarah at 11:00 AM."
    assert data["created_by"] == uid

    # Database records updated
    assert len(mock_db.evidence) == 1
    ev_rec = mock_db.evidence[0]
    assert ev_rec["content_hash"] == "hash-alpha-canonical-1"
    assert ev_rec["evidence_note"] == "Client confirmed approval on Zoom call with Sarah at 11:00 AM."

    # ad_approvals synchronized
    appr = mock_db.approvals["ad-ev-1"]
    assert appr["evidence_note"] == "Client confirmed approval on Zoom call with Sarah at 11:00 AM."


def test_attach_evidence_rejects_empty_payload(fake_session, mock_db, monkeypatch):
    """Evidence submission with neither note nor file is rejected with HTTP 422."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    # Empty JSON
    r1 = client.post(
        "/ads/ad-ev-1/evidence",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert r1.status_code == 422
    assert "at least a note or screenshot file" in r1.json()["detail"].lower()

    # Blank whitespace note
    r2 = client.post(
        "/ads/ad-ev-1/evidence",
        headers={"Authorization": f"Bearer {token}"},
        json={"evidence_note": "   \n\t  "},
    )
    assert r2.status_code == 422


def test_attach_evidence_rejects_mismatched_content_hash(fake_session, mock_db, monkeypatch):
    """PRD D13 / D3: Attempting to link evidence with a mismatched/stale content hash fails with 409 Conflict."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="account_manager")
    token = make_token(uid, role="account_manager")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-ev-1/evidence",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "evidence_note": "Approved by client",
            "content_hash": "hash-different-from-ad-content",
        },
    )
    assert response.status_code == 409
    assert "content hash mismatch" in response.json()["detail"].lower()


def test_attach_evidence_auto_binds_to_current_hash_if_omitted(fake_session, mock_db, monkeypatch):
    """When content_hash is omitted, evidence automatically binds to ad's current canonical row_hash."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-ev-1/evidence",
        headers={"Authorization": f"Bearer {token}"},
        json={"evidence_note": "Verbal OK via telephone."},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["content_hash"] == "hash-alpha-canonical-1"
    assert data["content_hash_matches"] is True


def test_attach_evidence_forbidden_for_unassigned_client(fake_session, mock_db, monkeypatch):
    """Reviewer assigned to client-alpha cannot attach evidence to ad-ev-beta (403 Forbidden)."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="account_manager")
    token = make_token(uid, role="account_manager")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-ev-beta/evidence",
        headers={"Authorization": f"Bearer {token}"},
        json={"evidence_note": "Trying to attach unassigned evidence"},
    )
    assert response.status_code == 403


def test_attach_evidence_conflict_on_terminal_state(fake_session, mock_db, monkeypatch):
    """Cannot attach evidence to an ad in 'sending' or 'sent' state (409 Conflict)."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    mock_db.approvals["ad-ev-1"]["state"] = "sent"

    client = TestClient(app)
    response = client.post(
        "/ads/ad-ev-1/evidence",
        headers={"Authorization": f"Bearer {token}"},
        json={"evidence_note": "Late evidence"},
    )
    assert response.status_code == 409
    assert "cannot attach evidence" in response.json()["detail"].lower()


# --- Tests: File Upload & Binary Storage (Needs: 21) ---

def test_upload_evidence_screenshot_file_success(fake_session, mock_db, monkeypatch, tmp_path):
    """Reviewer uploads screenshot file (PNG) persisting in LocalFileStore with opaque 32-hex ref (Needs: 21)."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="account_manager")
    token = make_token(uid, role="account_manager")

    screenshot_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRfake-screenshot-data"
    client = TestClient(app)

    response = client.post(
        "/ads/ad-ev-1/evidence/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "note": "Client approved Slack screenshot attached by Tobias.",
            "content_hash": "hash-alpha-canonical-1",
        },
        files={
            "file": ("slack_approval.png", io.BytesIO(screenshot_bytes), "image/png"),
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ad_id"] == "ad-ev-1"
    assert data["content_hash"] == "hash-alpha-canonical-1"
    assert data["evidence_note"] == "Client approved Slack screenshot attached by Tobias."
    assert data["file_name"] == "slack_approval.png"
    assert data["file_size"] == len(screenshot_bytes)
    assert data["mime_type"] == "image/png"
    assert data["evidence_file"] is not None
    assert len(data["evidence_file"]) == 32  # Opaque 32-hex ref

    # Verify file stored in LocalFileStore
    store = LocalFileStore()
    stored_bytes = store.get(data["evidence_file"])
    assert stored_bytes == screenshot_bytes


def test_upload_evidence_rejects_unassigned_client_before_storage(fake_session, mock_db, monkeypatch):
    from unittest.mock import Mock

    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")
    save_file = Mock(return_value="a" * 32)
    monkeypatch.setattr("apps.api.evidence.save_evidence_file", save_file)

    response = TestClient(app).post(
        "/ads/ad-ev-beta/evidence/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"note": "Unauthorized upload"},
        files={"file": ("approval.png", io.BytesIO(b"image-bytes"), "image/png")},
    )

    assert response.status_code == 403
    save_file.assert_not_called()


def test_upload_evidence_size_limit_is_checked_before_storage(fake_session, mock_db, monkeypatch):
    from unittest.mock import Mock

    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")
    save_file = Mock(return_value="a" * 32)
    monkeypatch.setattr("apps.api.evidence.MAX_EVIDENCE_SIZE_BYTES", 8)
    monkeypatch.setattr("apps.api.evidence.save_evidence_file", save_file)

    response = TestClient(app).post(
        "/ads/ad-ev-1/evidence/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"note": "Too large"},
        files={"file": ("approval.png", io.BytesIO(b"1234567890"), "image/png")},
    )

    assert response.status_code == 413
    save_file.assert_not_called()


def test_upload_evidence_rejects_unsupported_file_extension(fake_session, mock_db, monkeypatch):
    """Uploading executable or unsupported file extension fails with HTTP 422."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-ev-1/evidence/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"note": "Executable"},
        files={"file": ("malicious.exe", io.BytesIO(b"MZ..."), "application/x-dosexec")},
    )
    assert response.status_code == 422
    assert "unsupported file extension" in response.json()["detail"].lower()


def test_upload_evidence_rejects_empty_file(fake_session, mock_db, monkeypatch):
    """Uploading a 0-byte file fails with HTTP 422."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-ev-1/evidence/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"note": "Empty file"},
        files={"file": ("empty.png", io.BytesIO(b""), "image/png")},
    )
    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()


def test_get_evidence_file_stream_endpoint(fake_session, mock_db, monkeypatch):
    """GET /ads/{ad_id}/evidence/file downloads the stored evidence file."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="account_manager")
    token = make_token(uid, role="account_manager")

    # Store file in LocalFileStore
    store = LocalFileStore()
    file_bytes = b"%PDF-1.4 sample client approval email printout"
    ref = store.put(file_bytes)

    # Record evidence
    record_approval_evidence(
        fake_session,
        ad_id="ad-ev-1",
        client_id="client-alpha",
        content_hash="hash-alpha-canonical-1",
        created_by=uid,
        evidence_note="Attached PDF email",
        evidence_file=ref,
        file_name="client_email.pdf",
        mime_type="application/pdf",
    )

    client = TestClient(app)
    res = client.get(
        "/ads/ad-ev-1/evidence/file",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.content == file_bytes
    assert "application/pdf" in res.headers["content-type"]


def test_get_evidence_file_forbidden_for_unassigned_client(fake_session, mock_db, monkeypatch):
    """Attempting to download evidence file for unassigned client returns HTTP 403."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    res = client.get(
        "/ads/ad-ev-beta/evidence/file",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


# --- Tests: Content Divergence Detection (Needs: 40) ---

def test_evidence_detects_content_divergence_after_ad_edit(fake_session, mock_db, monkeypatch):
    """PRD D13: If an ad copy changes after evidence was linked, content_hash_matches becomes False."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="account_manager")
    token = make_token(uid, role="account_manager")

    # 1. Attach evidence for original content hash
    record_approval_evidence(
        fake_session,
        ad_id="ad-ev-1",
        client_id="client-alpha",
        content_hash="hash-alpha-canonical-1",
        created_by=uid,
        evidence_note="Approved original version",
    )

    client = TestClient(app)
    r1 = client.get("/ads/ad-ev-1/evidence", headers={"Authorization": f"Bearer {token}"})
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["latest"]["content_hash_matches"] is True

    # 2. Simulate edit: ad row_hash changes
    mock_db.ads["ad-ev-1"]["row_hash"] = "hash-alpha-edited-copy-2"

    r2 = client.get("/ads/ad-ev-1/evidence", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["current_content_hash"] == "hash-alpha-edited-copy-2"
    # Evidence was linked to original hash -> content_hash_matches is now False!
    assert d2["latest"]["content_hash_matches"] is False


# --- Tests: Batch Read API & Approval Record Enrichment ---

def test_batch_read_api_includes_evidence_fields_on_ads(fake_session, mock_db, monkeypatch):
    """GET /batches/{batch_id} returns evidence_note, evidence_file, and has_evidence on visible ads."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    mock_db.approvals["ad-ev-1"]["evidence_note"] = "Approved by client via Slack screenshot"
    mock_db.approvals["ad-ev-1"]["evidence_file"] = "a" * 32

    client = TestClient(app)
    res = client.get("/batches/batch-ev", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.json()

    ad1 = next(a for a in data["ads"] if a["ad_id"] == "ad-ev-1")
    assert ad1["has_evidence"] is True
    assert ad1["evidence_note"] == "Approved by client via Slack screenshot"
    assert ad1["evidence_file"] == "a" * 32


# --- Tests: UI Components Render Evidence Controls ---

def test_ad_card_component_renders_evidence_badge_and_preview():
    """Verify AdCard.jsx renders evidence badge and approval evidence preview text/file."""
    code = (Path(__file__).resolve().parents[1] / "apps" / "web" / "components" / "AdCard.jsx").read_text(encoding="utf-8")
    assert "evidence-badge" in code
    assert "ad-evidence-preview" in code
    assert "Approval Evidence:" in code
    assert "btn-evidence" in code


def test_batch_review_screen_component_renders_evidence_modal_and_file_upload():
    """Verify BatchReviewScreen.jsx wires onAttachEvidence, file input, and linked content hash."""
    code = (Path(__file__).resolve().parents[1] / "apps" / "web" / "components" / "BatchReviewScreen.jsx").read_text(encoding="utf-8")
    assert "onAttachEvidence" in code
    assert 'activeModal === "evidence"' in code
    assert "textarea-evidence-note" in code
    assert "input-evidence-file" in code
    assert "evidence-hash" in code
    assert "Save Evidence" in code
