"""Tests for Task 43: Reject with reason + dismiss finding (false-alarm record). Needs: 38, 40.

Verifies:
1. Reviewer rejects ad with mandatory reason, recording rejection_reason, rejected_by,
   rejected_at, and transitioning ad_approvals to 'rejected' with audit history.
2. Reviewer dismisses any finding as a false alarm with a mandatory non-empty reason,
   persisting immutable false-alarm records in dismissed_findings.
3. Dismissing all findings on an ad clears its blockers, updating active verdict to 'ready'.
4. Batch read API (/batches/{batch_id}) annotates findings with is_dismissed, dismissal_reason,
   dismissed_by, dismissed_at, excludes dismissed findings from active verdict calculation,
   and reports false_alarms_count in batch summary metrics.
5. Role and client assignment checks enforced (Needs: 7: 403 Forbidden).
6. Terminal state protection: ads in 'sending' or 'sent' cannot be rejected or have findings dismissed (409 Conflict).
7. UI component integration: dismiss button, false-alarm badges, modal dialogs, and summary metrics.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import pytest
from fastapi.testclient import TestClient

from apps.api import auth, main, users
from apps.api.approvals import ApprovalState, get_ad_approval_record
from apps.api.database import get_session
from apps.api.findings import (
    CREATE_DISMISSED_FINDINGS_TABLE,
    create_dismissed_findings_table,
    record_finding_dismissal,
    get_dismissed_findings_map,
    get_batch_false_alarms,
)
from apps.api.main import app
from apps.api.batches import get_batch_detail


# --- Mock Database Setup ---

class MockDB:
    def __init__(self):
        self.ads = {
            "ad-alpha-1": {
                "ad_id": "ad-alpha-1",
                "batch_id": "batch-43",
                "client_id": "client-alpha",
                "row_number": 1,
                "platform": "facebook",
                "platform_raw": "Facebook Feed",
                "ad_text": "Great summer sale headline with valid copy",
                "headline": "Summer Sale",
                "destination_url": "https://alpha.example/offer?utm_source=fb&utm_medium=cpc",
                "row_hash": "hash-alpha-1",
                "sheet_hash": "sheet-43",
                "creative_file": "banner.png",
            },
            "ad-alpha-2": {
                "ad_id": "ad-alpha-2",
                "batch_id": "batch-43",
                "client_id": "client-alpha",
                "row_number": 2,
                "platform": "google",
                "platform_raw": "Google Search",
                "ad_text": "Headline that had wording flags",
                "headline": "Cure All Pain",
                "destination_url": "https://alpha.example/pain?utm_source=google",
                "row_hash": "hash-alpha-2",
                "sheet_hash": "sheet-43",
                "creative_file": "",
            },
            "ad-beta-1": {
                "ad_id": "ad-beta-1",
                "batch_id": "batch-43",
                "client_id": "client-beta",
                "row_number": 3,
                "platform": "meta",
                "platform_raw": "Meta",
                "ad_text": "Beta ad copy",
                "headline": "Beta Title",
                "destination_url": "https://beta.example",
                "row_hash": "hash-beta-1",
                "sheet_hash": "sheet-43",
                "creative_file": "",
            },
        }
        self.batches = {
            "batch-43": {
                "batch_id": "batch-43",
                "filename": "summer_campaign.xlsx",
                "uploaded_by": "uploader-1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sheet_hash": "sheet-43",
            }
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
            "ad-alpha-2": {
                "ad_id": "ad-alpha-2",
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
        self.findings = {
            "find-1": {
                "finding_id": "find-1",
                "ad_id": "ad-alpha-1",
                "row_number": 1,
                "check_type": "wording",
                "message": "Potential unsubstantiated guarantee",
                "field": "headline",
                "proposed_fix": "Special Summer Offer",
                "rule_id": "CLAIM_01",
                "file_ref": "brand_book.pdf",
            },
            "find-2": {
                "finding_id": "find-2",
                "ad_id": "ad-alpha-2",
                "row_number": 2,
                "check_type": "page",
                "message": "Landing page 404 response",
                "field": "destination_url",
                "proposed_fix": None,
                "rule_id": "URL_01",
                "file_ref": None,
            },
            "find-3": {
                "finding_id": "find-3",
                "ad_id": "ad-alpha-2",
                "row_number": 2,
                "check_type": "wording",
                "message": "Prohibited medical cure claim",
                "field": "headline",
                "proposed_fix": None,
                "rule_id": "HEALTH_01",
                "file_ref": "compliance.pdf",
            },
        }
        self.dismissed_findings = {}
        self.history = []
        self.traces = {
            "ad-alpha-1": {"batch_id": "batch-43", "ad_id": "ad-alpha-1", "verdict": "fix needed"},
            "ad-alpha-2": {"batch_id": "batch-43", "ad_id": "ad-alpha-2", "verdict": "blocked"},
            "ad-beta-1": {"batch_id": "batch-43", "ad_id": "ad-beta-1", "verdict": "ready"},
        }


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

            # 2. Query batch record
            if "FROM batches" in s and "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                b = self.db.batches.get(bid)
                if b:
                    return FakeResult([(
                        b["batch_id"], b["filename"], b["uploaded_by"], b["created_at"], b["sheet_hash"]
                    )])
                return FakeResult([])

            # 3. Total count of ads in batch
            if "SELECT COUNT(*)" in s and "FROM ads" in s:
                bid = params_tuple[0]
                cnt = sum(1 for a in self.db.ads.values() if a["batch_id"] == bid)
                return FakeResult([(cnt,)])

            # 4. Query batch ads
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

            # 5. Query findings
            if "FROM findings" in s and "WHERE ad_id = ANY(%s)" in s:
                ad_ids = list(params_tuple[0])
                rows = []
                for fid, f in self.db.findings.items():
                    if f["ad_id"] in ad_ids:
                        rows.append((
                            f["finding_id"], f["ad_id"], f["row_number"], f["check_type"],
                            f["message"], f["field"], f["proposed_fix"], f["rule_id"], f["file_ref"]
                        ))
                rows.sort(key=lambda x: (x[2], x[0]))
                return FakeResult(rows)

            if "FROM findings WHERE ad_id = %s AND finding_id = %s" in s:
                aid, fid = params_tuple
                f = self.db.findings.get(fid)
                if f and f["ad_id"] == aid:
                    return FakeResult([(f["check_type"], f["rule_id"])])
                return FakeResult([])

            if "FROM findings WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                rows = [(f["finding_id"],) for f in self.db.findings.values() if f["ad_id"] == aid]
                return FakeResult(rows)

            # 6. Dismissed findings queries & insert
            if "INSERT INTO dismissed_findings" in s:
                fid, aid, bid, cid, user_id, reason, c_type, r_id = params_tuple
                dismissal = {
                    "dismissal_id": len(self.db.dismissed_findings) + 1,
                    "finding_id": fid,
                    "ad_id": aid,
                    "batch_id": bid,
                    "client_id": cid,
                    "dismissed_by": user_id,
                    "reason": reason,
                    "check_type": c_type,
                    "rule_id": r_id,
                    "created_at": datetime.now(timezone.utc),
                }
                self.db.dismissed_findings[fid] = dismissal
                if "RETURNING" in s:
                    return FakeResult([(
                        dismissal["dismissal_id"], dismissal["finding_id"], dismissal["ad_id"],
                        dismissal["batch_id"], dismissal["client_id"], dismissal["dismissed_by"],
                        dismissal["reason"], dismissal["created_at"]
                    )])
                return FakeResult([])

            if "FROM dismissed_findings" in s and "WHERE ad_id = ANY(%s)" in s:
                ad_ids = list(params_tuple[0])
                rows = []
                for d in self.db.dismissed_findings.values():
                    if d["ad_id"] in ad_ids:
                        rows.append((
                            d["dismissal_id"], d["finding_id"], d["ad_id"], d["batch_id"],
                            d["client_id"], d["dismissed_by"], d["reason"], d["created_at"]
                        ))
                return FakeResult(rows)

            if "FROM dismissed_findings" in s and "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                rows = []
                for d in self.db.dismissed_findings.values():
                    if d["batch_id"] == bid:
                        rows.append((
                            d["dismissal_id"], d["finding_id"], d["ad_id"], d["batch_id"],
                            d["client_id"], d["dismissed_by"], d["reason"], d.get("check_type"),
                            d.get("rule_id"), d["created_at"]
                        ))
                return FakeResult(rows)

            # 7. worker_traces query & update
            if "FROM worker_traces WHERE batch_id = %s AND ad_id = ANY(%s)" in s:
                bid = params_tuple[0]
                ad_ids = list(params_tuple[1])
                rows = []
                for aid in ad_ids:
                    t = self.db.traces.get(aid)
                    if t and t["batch_id"] == bid:
                        rows.append((aid, t["verdict"]))
                return FakeResult(rows)

            if "UPDATE worker_traces" in s:
                bid, aid = params_tuple
                if aid in self.db.traces:
                    self.db.traces[aid]["verdict"] = "ready"
                return FakeResult([])

            # 8. Query ad_approvals
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

            # 9. Update ad_approvals (approve/reject)
            if "UPDATE ad_approvals" in s:
                to_state = params_tuple[0]
                aid = params_tuple[-1]
                appr = self.db.approvals.get(aid)
                if appr:
                    appr["state"] = to_state
                    if to_state == ApprovalState.REJECTED.value:
                        appr["rejection_reason"] = params_tuple[1]
                        appr["rejected_by"] = params_tuple[2]
                        appr["rejected_at"] = params_tuple[3]
                    elif to_state == ApprovalState.APPROVED.value:
                        appr["content_hash"] = params_tuple[1]
                        appr["approved_by"] = params_tuple[2]
                        appr["approved_at"] = params_tuple[3]
                        appr["evidence_note"] = params_tuple[4]
                        appr["evidence_file"] = params_tuple[5]
                return FakeResult([])

            # 10. Insert approval history
            if "INSERT INTO approval_history" in s:
                self.db.history.append(params_tuple)
                return FakeResult([])

            # 11. Rejected rows
            if "FROM batch_rejected_rows" in s:
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

    monkeypatch.setattr("apps.api.findings.get_session", _session)
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


def mock_users_helper(monkeypatch: pytest.MonkeyPatch, assigned_clients: list[str], role: str = "buyer"):
    uid = "test-buyer-1"
    record = {
        "user_id": uid,
        "email": "buyer@agency.test",
        "display_name": "Test Buyer",
        "role": role,
        "is_active": True,
    }
    monkeypatch.setattr(users, "get_user_by_id", lambda u: record if u == uid else None)
    monkeypatch.setattr(users, "get_user_client_ids", lambda u: assigned_clients if u == uid else [])
    return uid


# --- Tests: Schema & DDL ---

def test_create_dismissed_findings_table_ddl():
    """Verify dismissed_findings table DDL contains proper primary keys, unique constraint, and non-empty check."""
    assert "CREATE TABLE IF NOT EXISTS dismissed_findings" in CREATE_DISMISSED_FINDINGS_TABLE
    assert "finding_id TEXT NOT NULL" in CREATE_DISMISSED_FINDINGS_TABLE
    assert "CONSTRAINT uq_dismissed_finding UNIQUE (finding_id)" in CREATE_DISMISSED_FINDINGS_TABLE
    assert "CHECK (length(trim(reason)) > 0)" in CREATE_DISMISSED_FINDINGS_TABLE
    assert "REFERENCES ads" in CREATE_DISMISSED_FINDINGS_TABLE
    assert "REFERENCES batches" in CREATE_DISMISSED_FINDINGS_TABLE
    assert "REFERENCES clients" in CREATE_DISMISSED_FINDINGS_TABLE
    assert "REFERENCES users" in CREATE_DISMISSED_FINDINGS_TABLE


# --- Tests: Rejection with Reason (PRD 4.6, Needs: 38, 40) ---

def test_reject_ad_success_with_mandatory_reason(fake_session, mock_db, monkeypatch):
    """Reviewer rejects an ad with a mandatory reason, transitioning state to rejected and recording audit trail."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-alpha-1/reject",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Incompatible holiday discount dates with client promotion schedule."},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["state"] == "rejected"
    assert data["rejection_reason"] == "Incompatible holiday discount dates with client promotion schedule."
    assert data["rejected_by"] == uid
    assert data["rejected_at"] is not None

    # Verify database state was updated
    appr = mock_db.approvals["ad-alpha-1"]
    assert appr["state"] == "rejected"
    assert appr["rejection_reason"] == "Incompatible holiday discount dates with client promotion schedule."
    assert appr["rejected_by"] == uid

    # Verify history recorded
    assert len(mock_db.history) == 1
    hist_entry = mock_db.history[0]
    assert hist_entry[1] == "pending_review"  # from_state
    assert hist_entry[2] == "rejected"        # to_state
    assert hist_entry[3] == uid               # changed_by
    assert hist_entry[4] == "Incompatible holiday discount dates with client promotion schedule."


def test_reject_ad_requires_non_empty_reason(fake_session, mock_db, monkeypatch):
    """Rejection fails with 422 if reason is empty or whitespace-only."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    # Empty string
    r1 = client.post(
        "/ads/ad-alpha-1/reject",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": ""},
    )
    assert r1.status_code == 422

    # Whitespace only
    r2 = client.post(
        "/ads/ad-alpha-1/reject",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "    "},
    )
    assert r2.status_code == 422


def test_reject_ad_forbidden_for_unassigned_client(fake_session, mock_db, monkeypatch):
    """Reviewer assigned only to client-alpha cannot reject an ad belonging to client-beta."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-beta-1/reject",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Should not be allowed"},
    )
    assert response.status_code == 403


def test_reject_ad_conflict_on_terminal_state(fake_session, mock_db, monkeypatch):
    """Ads in 'sending' or 'sent' state cannot be rejected (409 Conflict)."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    mock_db.approvals["ad-alpha-1"]["state"] = "sent"

    client = TestClient(app)
    response = client.post(
        "/ads/ad-alpha-1/reject",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Too late to reject"},
    )
    assert response.status_code == 409
    assert "cannot reject" in response.json()["detail"].lower()


# --- Tests: False-Alarm Finding Dismissal (PRD Line 90, Line 216, §4.6, Needs: 40) ---

def test_dismiss_finding_endpoint_success(fake_session, mock_db, monkeypatch):
    """Reviewer dismisses a finding as false alarm with mandatory reason, recording false-alarm entry."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-alpha-1/findings/find-1/dismiss",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Client confirmed guarantee has substantiated legal backing in Brand Guidelines §3.2"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["finding_id"] == "find-1"
    assert data["ad_id"] == "ad-alpha-1"
    assert data["dismissed_by"] == uid
    assert data["reason"] == "Client confirmed guarantee has substantiated legal backing in Brand Guidelines §3.2"
    assert data["is_dismissed"] is True
    assert data["active_findings_count"] == 0
    assert data["new_verdict"] == "ready"

    # Database records false alarm
    assert "find-1" in mock_db.dismissed_findings
    rec = mock_db.dismissed_findings["find-1"]
    assert rec["reason"] == "Client confirmed guarantee has substantiated legal backing in Brand Guidelines §3.2"
    assert rec["dismissed_by"] == uid
    assert rec["check_type"] == "wording"
    assert rec["rule_id"] == "CLAIM_01"


def test_dismiss_finding_requires_non_empty_reason(fake_session, mock_db, monkeypatch):
    """Dismissing a finding fails with 422 if reason is empty or whitespace-only."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    # Empty reason
    r1 = client.post(
        "/ads/ad-alpha-1/findings/find-1/dismiss",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": ""},
    )
    assert r1.status_code == 422

    # Whitespace-only reason
    r2 = client.post(
        "/ads/ad-alpha-1/findings/find-1/dismiss",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "   \t\n   "},
    )
    assert r2.status_code == 422


def test_dismiss_all_findings_clears_blockers_and_updates_verdict_to_ready(fake_session, mock_db, monkeypatch):
    """Dismissing all findings on an ad updates its active verdict to 'ready'."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)

    # ad-alpha-2 initially has 2 findings: find-2 (page) and find-3 (wording)
    # Dismiss first finding: find-2
    r1 = client.post(
        "/ads/ad-alpha-2/findings/find-2/dismiss",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "DNS propagation delayed; URL resolves correctly in target geo."},
    )
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["active_findings_count"] == 1
    assert d1["new_verdict"] == "fix needed"  # Remaining find-3 keeps fix needed / blocked

    # Dismiss second finding: find-3
    r2 = client.post(
        "/ads/ad-alpha-2/findings/find-3/dismiss",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Disclaimer added in footer compliant with regulatory guidelines."},
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["active_findings_count"] == 0
    assert d2["new_verdict"] == "ready"

    # Trace is updated to ready
    assert mock_db.traces["ad-alpha-2"]["verdict"] == "ready"


def test_dismiss_finding_client_assignment_enforced(fake_session, mock_db, monkeypatch):
    """User not assigned to client-beta cannot dismiss findings on ad-beta-1 (403 Forbidden)."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    client = TestClient(app)
    response = client.post(
        "/ads/ad-beta-1/findings/find-1/dismiss",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Attempting unassigned dismissal"},
    )
    assert response.status_code == 403


def test_dismiss_finding_terminal_state_conflict(fake_session, mock_db, monkeypatch):
    """Findings cannot be dismissed on ads in 'sending' or 'sent' state (409 Conflict)."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    mock_db.approvals["ad-alpha-1"]["state"] = "sending"

    client = TestClient(app)
    response = client.post(
        "/ads/ad-alpha-1/findings/find-1/dismiss",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Cannot dismiss when already sending"},
    )
    assert response.status_code == 409
    assert "cannot dismiss findings" in response.json()["detail"].lower()


# --- Tests: Batch Read API Annotates Dismissals & False-Alarms Metric ---

def test_batch_read_api_annotates_findings_with_dismissal_details(fake_session, mock_db, monkeypatch):
    """GET /batches/{batch_id} annotates findings with is_dismissed, reason, by, at, and false_alarms_count."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    # Dismiss find-1 first
    record_finding_dismissal(
        fake_session,
        finding_id="find-1",
        ad_id="ad-alpha-1",
        batch_id="batch-43",
        client_id="client-alpha",
        dismissed_by=uid,
        reason="Brand exception confirmed.",
        check_type="wording",
        rule_id="CLAIM_01",
    )

    client = TestClient(app)
    res = client.get("/batches/batch-43", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    data = res.json()

    # Verify false_alarms_count in summary
    assert data["summary"]["false_alarms_count"] == 1

    # Check ad-alpha-1 findings
    ad1 = next(a for a in data["ads"] if a["ad_id"] == "ad-alpha-1")
    assert len(ad1["findings"]) == 1
    f1 = ad1["findings"][0]
    assert f1["finding_id"] == "find-1"
    assert f1["is_dismissed"] is True
    assert f1["dismissal_reason"] == "Brand exception confirmed."
    assert f1["dismissed_by"] == uid
    assert f1["dismissed_at"] is not None

    # Since all findings on ad-alpha-1 are dismissed, its active verdict becomes ready!
    assert ad1["verdict"] == "ready"


def test_batch_false_alarms_endpoint(fake_session, mock_db, monkeypatch):
    """GET /batches/{batch_id}/false-alarms returns false-alarm records scoped to user's clients."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    # Add 2 dismissals: 1 for client-alpha, 1 for client-beta
    record_finding_dismissal(
        fake_session,
        finding_id="find-1",
        ad_id="ad-alpha-1",
        batch_id="batch-43",
        client_id="client-alpha",
        dismissed_by=uid,
        reason="Alpha exception",
    )
    record_finding_dismissal(
        fake_session,
        finding_id="find-beta-1",
        ad_id="ad-beta-1",
        batch_id="batch-43",
        client_id="client-beta",
        dismissed_by="other-user",
        reason="Beta exception",
    )

    client = TestClient(app)
    res = client.get("/batches/batch-43/false-alarms", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.json()
    assert data["batch_id"] == "batch-43"
    # User is only assigned to client-alpha, so client-beta record is excluded!
    assert data["total_false_alarms"] == 1
    assert data["false_alarms"][0]["finding_id"] == "find-1"


def test_ad_dismissals_history_endpoint(fake_session, mock_db, monkeypatch):
    """GET /ads/{ad_id}/findings/dismissals returns dismissals history for an ad."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")

    record_finding_dismissal(
        fake_session,
        finding_id="find-1",
        ad_id="ad-alpha-1",
        batch_id="batch-43",
        client_id="client-alpha",
        dismissed_by=uid,
        reason="Alpha exception verified",
    )

    client = TestClient(app)
    res = client.get("/ads/ad-alpha-1/findings/dismissals", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.json()
    assert data["ad_id"] == "ad-alpha-1"
    assert data["total_dismissed"] == 1
    assert data["dismissals"][0]["finding_id"] == "find-1"


# --- Tests: End-to-End False-Alarm Dismissal to Approval Workflow ---

def test_dismissed_blockers_enable_subsequent_approval_with_content_hash(fake_session, mock_db, monkeypatch):
    """Full lifecycle: Ad has findings -> findings dismissed as false alarms -> ad verdict is ready -> approved with content hash."""
    uid = mock_users_helper(monkeypatch, assigned_clients=["client-alpha"], role="buyer")
    token = make_token(uid, role="buyer")
    client = TestClient(app)

    # 1. Dismiss finding find-1 on ad-alpha-1
    res_dismiss = client.post(
        "/ads/ad-alpha-1/findings/find-1/dismiss",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Approved exception from VP of Marketing."},
    )
    assert res_dismiss.status_code == 200
    assert res_dismiss.json()["new_verdict"] == "ready"

    # 2. Approve ad-alpha-1 using its exact row_hash
    target_ad = mock_db.ads["ad-alpha-1"]
    res_approve = client.post(
        "/ads/ad-alpha-1/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "content_hash": target_ad["row_hash"],
            "evidence_note": "Dismissed false alarm and approved for launch.",
        },
    )
    assert res_approve.status_code == 200, res_approve.text
    data = res_approve.json()
    assert data["state"] == "approved"
    assert data["content_hash"] == target_ad["row_hash"]
    assert data["approved_by"] == uid

    # Approval record is stored in database
    appr = mock_db.approvals["ad-alpha-1"]
    assert appr["state"] == "approved"
    assert appr["content_hash"] == target_ad["row_hash"]


# --- Tests: UI Components Render Dismiss Finding and False-Alarm Controls ---

def test_ad_card_component_renders_dismiss_controls():
    """Verify AdCard.jsx contains dismiss action button, dismissed badge, and dismissal reason display."""
    code = (Path(__file__).resolve().parents[1] / "apps" / "web" / "components" / "AdCard.jsx").read_text(encoding="utf-8")
    assert "onDismissFinding" in code
    assert "btn-dismiss-finding" in code
    assert "finding-dismissed-badge" in code or "finding-dismissed-pill" in code
    assert "dismissal-reason" in code
    assert "data-is-dismissed" in code


def test_batch_review_screen_component_renders_dismiss_modal():
    """Verify BatchReviewScreen.jsx wires onDismissFinding and dismiss modal with mandatory reason."""
    code = (Path(__file__).resolve().parents[1] / "apps" / "web" / "components" / "BatchReviewScreen.jsx").read_text(encoding="utf-8")
    assert "onDismissFinding" in code
    assert "handleOpenDismissFinding" in code
    assert 'activeModal === "dismiss"' in code
    assert "textarea-dismiss-reason" in code
    assert "Confirm Dismissal" in code
    assert "false_alarms_count" in code


def test_batch_summary_component_renders_false_alarms_metric():
    """Verify BatchSummary.jsx displays False Alarms count metric box."""
    code = (Path(__file__).resolve().parents[1] / "apps" / "web" / "components" / "BatchSummary.jsx").read_text(encoding="utf-8")
    assert "false-alarms-box" in code
    assert "metric-false-alarms" in code
    assert "False Alarms" in code
