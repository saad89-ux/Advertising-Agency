"""Tests for Task 45: Idempotent send to stand-in dry-run (Needs: 40).

Verifies PRD §4.6, Dataflow Architecture §3, and Never-break Rules 1, 2, 7:
1. Table DDL and indexes for ad_sends and send_alerts.
2. Payload construction matching stand-in ad platform schema.
3. Deterministic idempotency key generation.
4. Rule 1: No ad sent without human approval (HTTP 400 UnapprovedAdError).
5. Rule 2 / PRD D13: Approved content hash validation (HTTP 409 ContentHashMismatchError).
6. Successful stand-in dry-run send (status: accepted -> sent state).
7. Rule 2: Idempotent replay of already-sent ad (0 duplicate platform calls).
8. Idempotent-Replay header detection from stand-in platform.
9. Chaos recovery: fail_after_commit 503 followed by replay recovery without duplicates.
10. Stand-in policy rejection alerts a person and returns to review (no auto-relaunch).
11. Bounded retries exhausted moves to dead_letter queue with reviewer alert.
12. Client scoping & permissions: unassigned client send rejected with HTTP 403.
13. Rule 7: Real platform calls are strictly switched OFF (RealPlatformCallDisabledError).
14. Batch send endpoint (POST /batches/{batch_id}/send-approved).
15. Send status and alert query endpoints (GET /ads/{ad_id}/send, GET /ads/{ad_id}/alerts).
16. Frontend component contracts (AdCard.jsx and BatchReviewScreen.jsx).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.approvals import (
    ApprovalState,
    ContentHashMismatchError,
    UnapprovedAdError,
    create_approval_tables,
    is_valid_transition,
    transition_ad_state,
)
from apps.api.auth import create_access_token
from apps.api.database import get_session
from apps.api.main import app as main_api_app
from apps.worker.platforms import (
    GoogleAdsApiAdapter,
    MetaMarketingApiAdapter,
    RealPlatformCallDisabledError,
    StandInPlatformAdapter,
    TikTokAdsApiAdapter,
    get_platform_adapter,
)
from apps.worker.sender import (
    CREATE_AD_SENDS_TABLE,
    CREATE_SEND_ALERTS_TABLE,
    build_stand_in_ad_payload,
    create_send_tables,
    generate_idempotency_key,
    get_ad_alerts,
    get_ad_send_records,
    send_ad_to_stand_in,
    send_batch_approved_ads,
)
from data.stand_in_platform.app import STATE as STAND_IN_STATE, app as stand_in_app, reset as stand_in_reset


# --- Fixtures and Mocks ---

@pytest.fixture(autouse=True)
def setup_auth(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")

    def mock_get_user_by_id(uid: str):
        if uid in ("buyer_1", "buyer_2"):
            return {
                "user_id": uid,
                "email": f"{uid}@example.com",
                "display_name": uid,
                "role": "buyer",
                "is_active": True,
            }
        return None

    def mock_get_user_client_ids(uid: str):
        if uid == "buyer_1":
            return ["client_aura"]
        if uid == "buyer_2":
            return ["client_lumina"]
        return []

    monkeypatch.setattr("apps.api.users.get_user_by_id", mock_get_user_by_id)
    monkeypatch.setattr("apps.api.users.get_user_client_ids", mock_get_user_client_ids)


@pytest.fixture(autouse=True)
def reset_stand_in_platform():
    """Reset the stand-in synthetic platform before each test."""
    stand_in_reset()
    STAND_IN_STATE["cfg"] = {
        "fail_before_commit_rate": 0.0,
        "fail_after_commit_rate": 0.0,
        "timeout_rate": 0.0,
        "timeout_seconds": 15,
        "seed": 1,
    }


@pytest.fixture
def stand_in_client():
    """In-memory TestClient pointing to the synthetic stand-in platform."""
    return TestClient(stand_in_app)


class MockDB:
    """Mock storage simulating PostgreSQL tables for sender testing."""

    def __init__(self):
        self.clients = {
            "client_aura": {"client_id": "client_aura", "name": "Aura Glow Skincare"},
            "client_lumina": {"client_id": "client_lumina", "name": "Lumina Sleep"},
        }
        self.user_assignments = [
            ("buyer_1", "client_aura"),
            ("buyer_2", "client_lumina"),
        ]
        self.batches = {
            "batch-100": {
                "batch_id": "batch-100",
                "uploaded_by": "buyer_1",
                "filename": "sheet.csv",
                "sheet_hash": "sheethash123",
            }
        }
        self.ads = {
            "ad-clean-1": {
                "ad_id": "ad-clean-1",
                "batch_id": "batch-100",
                "client_id": "client_aura",
                "row_number": 1,
                "platform": "facebook",
                "ad_text": "Hydrating face cream for glowing skin.",
                "headline": "Aura Pure Glow",
                "destination_url": "https://auraglow.example.com/cream",
                "row_hash": "hash_clean_1",
                "sheet_hash": "sheethash123",
                "creative_file": "cream.png",
            },
            "ad-policy-fail": {
                "ad_id": "ad-policy-fail",
                "batch_id": "batch-100",
                "client_id": "client_aura",
                "row_number": 2,
                "platform": "instagram",
                "ad_text": "Clinically proven miracle cure for flawless skin.",  # matches stand-in rule
                "headline": "Miracle Cream",
                "destination_url": "https://auraglow.example.com/miracle",
                "row_hash": "hash_policy_fail",
                "sheet_hash": "sheethash123",
                "creative_file": "miracle.png",
            },
            "ad-other-client": {
                "ad_id": "ad-other-client",
                "batch_id": "batch-100",
                "client_id": "client_lumina",
                "row_number": 3,
                "platform": "facebook",
                "ad_text": "Sleep deep with Lumina natural tea.",
                "headline": "Deep Sleep",
                "destination_url": "https://luminasleep.example.com/tea",
                "row_hash": "hash_lumina_3",
                "sheet_hash": "sheethash123",
                "creative_file": "tea.png",
            },
        }
        self.approvals = {
            "ad-clean-1": {
                "ad_id": "ad-clean-1",
                "client_id": "client_aura",
                "state": "approved",
                "content_hash": "hash_clean_1",
                "approved_by": "buyer_1",
                "approved_at": datetime.now(timezone.utc),
                "evidence_note": "Client approved via Slack note",
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
            "ad-policy-fail": {
                "ad_id": "ad-policy-fail",
                "client_id": "client_aura",
                "state": "approved",
                "content_hash": "hash_policy_fail",
                "approved_by": "buyer_1",
                "approved_at": datetime.now(timezone.utc),
                "evidence_note": "Reviewed copy",
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
            "ad-other-client": {
                "ad_id": "ad-other-client",
                "client_id": "client_lumina",
                "state": "approved",
                "content_hash": "hash_lumina_3",
                "approved_by": "buyer_2",
                "approved_at": datetime.now(timezone.utc),
                "evidence_note": "Approved by client lead",
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
        self.sends: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []


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

            # 2. Query ad approvals
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

            # 3. Update ad_approvals
            if s.startswith("UPDATE ad_approvals"):
                to_state = params_tuple[0]
                aid = params_tuple[-1]
                if aid in self.db.approvals:
                    appr = self.db.approvals[aid]
                    appr["state"] = to_state
                    appr["updated_at"] = datetime.now(timezone.utc)
                    if to_state == "sending":
                        appr["idempotency_key"] = params_tuple[1]
                        appr["send_attempts"] += 1
                    elif to_state == "sent":
                        appr["sent_at"] = params_tuple[1]
                    elif to_state == "rejected":
                        appr["rejection_reason"] = params_tuple[1]
                    elif to_state == "dead_letter":
                        appr["rejection_reason"] = params_tuple[1]
                return FakeResult([])

            # 4. Insert into ad_sends
            if s.startswith("INSERT INTO ad_sends"):
                (
                    sid, aid, cid, bid, ikey, chash, plat, stat, pad_id,
                    p_stat, r_reason, is_dr, is_rep, att_count, req_p,
                    res_p, err_m, created
                ) = params_tuple
                rec = {
                    "send_id": sid, "ad_id": aid, "client_id": cid, "batch_id": bid,
                    "idempotency_key": ikey, "content_hash": chash, "platform": plat,
                    "status": stat, "platform_ad_id": pad_id, "platform_status": p_stat,
                    "rejection_reason": r_reason, "is_dry_run": is_dr,
                    "idempotent_replay": is_rep, "attempt_count": att_count,
                    "request_payload": req_p, "response_payload": res_p,
                    "error_message": err_m, "created_at": created
                }
                self.db.sends.append(rec)
                return FakeResult([])

            # 5. Query ad_sends
            if "FROM ad_sends" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                rows = [
                    (
                        r["send_id"], r["ad_id"], r["client_id"], r["batch_id"],
                        r["idempotency_key"], r["content_hash"], r["platform"],
                        r["status"], r["platform_ad_id"], r["platform_status"],
                        r["rejection_reason"], r["is_dry_run"], r["idempotent_replay"],
                        r["attempt_count"], r["request_payload"], r["response_payload"],
                        r["error_message"], r["created_at"]
                    )
                    for r in self.db.sends if r["ad_id"] == aid
                ]
                return FakeResult(rows)

            # 6. Insert into send_alerts
            if s.startswith("INSERT INTO send_alerts"):
                alid, aid, cid, bid, atype, sev, msg, det, created = params_tuple
                self.db.alerts.append({
                    "alert_id": alid, "ad_id": aid, "client_id": cid, "batch_id": bid,
                    "alert_type": atype, "severity": sev, "message": msg,
                    "details": det, "resolved": False, "created_at": created
                })
                return FakeResult([])

            # 7. Query send_alerts
            if "FROM send_alerts" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                rows = [
                    (
                        a["alert_id"], a["ad_id"], a["client_id"], a["batch_id"],
                        a["alert_type"], a["severity"], a["message"], a["details"],
                        a["resolved"], a["created_at"]
                    )
                    for a in self.db.alerts if a["ad_id"] == aid
                ]
                return FakeResult(rows)

            # 8. User client assignments
            if "FROM user_client_assignments" in s:
                uid = params_tuple[0]
                rows = [(cid,) for (u, cid) in self.db.user_assignments if u == uid]
                return FakeResult(rows)

            # 9. Query batch ads with approvals for batch send
            if "FROM ads a JOIN ad_approvals ap ON a.ad_id = ap.ad_id" in s:
                bid = params_tuple[0]
                cids = params_tuple[1] if len(params_tuple) > 1 else None
                rows = []
                for aid, ad in self.db.ads.items():
                    if ad["batch_id"] == bid:
                        if cids is None or ad["client_id"] in cids:
                            appr = self.db.approvals.get(aid, {})
                            rows.append((aid, ad["client_id"], appr.get("state", "pending_review")))
                return FakeResult(rows)

            # 10. Query batches table
            if "FROM batches" in s and "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                batch = self.db.batches.get(bid)
                if batch:
                    return FakeResult([(
                        batch["batch_id"], batch["uploaded_by"], batch["filename"], batch["sheet_hash"],
                        "completed", 1, 0, datetime.now(timezone.utc), datetime.now(timezone.utc)
                    )])
                return FakeResult([])

            # 11. Count ads in batch
            if "SELECT COUNT(*) FROM ads WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                if len(params_tuple) > 1:
                    cids = params_tuple[1]
                    count = sum(1 for ad in self.db.ads.values() if ad["batch_id"] == bid and ad["client_id"] in cids)
                else:
                    count = sum(1 for ad in self.db.ads.values() if ad["batch_id"] == bid)
                return FakeResult([(count,)])

            # Fallback
            return FakeResult([])

    class FakeSession:
        def __init__(self, db: MockDB):
            self.cursor = FakeCursor(db)

        def __enter__(self):
            return self.cursor

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def execute(self, statement: str, params: Any = None):
            return self.cursor.execute(statement, params)

    session = FakeSession(mock_db)
    monkeypatch.setattr("apps.api.database.get_session", lambda: session)
    monkeypatch.setattr("apps.worker.sender.get_session", lambda: session)
    monkeypatch.setattr("apps.api.approvals.get_session", lambda: session)
    monkeypatch.setattr("apps.api.sends.get_session", lambda: session)
    return session


@pytest.fixture
def buyer_token():
    return create_access_token("buyer_1", role="buyer")


@pytest.fixture
def other_buyer_token():
    return create_access_token("buyer_2", role="buyer")


# --- Unit Tests ---

def test_create_send_tables_ddl(fake_session):
    """Verify DDL creation for ad_sends and send_alerts tables."""
    create_send_tables()
    # Ensure SQL strings contain expected columns
    assert "idempotency_key TEXT NOT NULL" in CREATE_AD_SENDS_TABLE
    assert "platform_status TEXT" in CREATE_AD_SENDS_TABLE
    assert "is_dry_run BOOLEAN NOT NULL DEFAULT TRUE" in CREATE_AD_SENDS_TABLE
    assert "alert_type TEXT NOT NULL" in CREATE_SEND_ALERTS_TABLE


def test_build_stand_in_ad_payload():
    """Verify payload construction conforms strictly to stand-in platform schema."""
    ad = {
        "ad_id": "ad-123",
        "client_id": "client_aura",
        "platform": "facebook",
        "ad_text": "Glow with natural rosehip oil.",
        "headline": "Aura Glow",
        "destination_url": "https://auraglow.example.com/oil",
        "creative_file": "rosehip.jpg",
    }
    payload = build_stand_in_ad_payload(ad, content_hash="hash_abc_123")
    assert payload == {
        "ad_id": "ad-123",
        "client": "client_aura",
        "platform": "facebook",
        "text": "Glow with natural rosehip oil.",
        "headline": "Aura Glow",
        "link": "https://auraglow.example.com/oil",
        "creative": "rosehip.jpg",
        "content_hash": "hash_abc_123",
    }


def test_generate_idempotency_key():
    """Verify deterministic idempotency key format."""
    key1 = generate_idempotency_key("ad-1", "hash-123")
    key2 = generate_idempotency_key("ad-1", "hash-123")
    assert key1 == "send:ad-1:hash-123"
    assert key1 == key2


def test_send_fails_if_unapproved(fake_session, mock_db, stand_in_client):
    """Never-break Rule 1: No ad sent without human approval."""
    # Set ad to pending_review
    mock_db.approvals["ad-clean-1"]["state"] = ApprovalState.PENDING_REVIEW.value

    with pytest.raises(UnapprovedAdError) as exc_info:
        send_ad_to_stand_in("ad-clean-1", connection=fake_session, client=stand_in_client)
    assert "approval is required" in str(exc_info.value)

    # Set ad to rejected
    mock_db.approvals["ad-clean-1"]["state"] = ApprovalState.REJECTED.value
    with pytest.raises(UnapprovedAdError):
        send_ad_to_stand_in("ad-clean-1", connection=fake_session, client=stand_in_client)


def test_send_fails_on_content_hash_mismatch(fake_session, mock_db, stand_in_client):
    """Never-break Rule 2 & PRD D13: Tampered or edited copy cannot be sent without re-approval."""
    # Ad row_hash changed from 'hash_clean_1' to 'tampered_hash_999'
    mock_db.ads["ad-clean-1"]["row_hash"] = "tampered_hash_999"

    with pytest.raises(ContentHashMismatchError) as exc_info:
        send_ad_to_stand_in("ad-clean-1", connection=fake_session, client=stand_in_client)
    assert "content hash mismatch" in str(exc_info.value).lower()
    assert "only the approved version can be sent" in str(exc_info.value).lower()


def test_successful_stand_in_send(fake_session, mock_db, stand_in_client):
    """Verify clean send to stand-in dry-run platform: approved -> sending -> sent."""
    res = send_ad_to_stand_in("ad-clean-1", connection=fake_session, client=stand_in_client)

    assert res["status"] == "sent"
    assert res["is_dry_run"] is True
    assert res["idempotent_replay"] is False
    assert res["platform_status"] == "accepted"
    assert res["platform_ad_id"] is not None
    assert res["attempts"] == 1

    # Check DB state
    assert mock_db.approvals["ad-clean-1"]["state"] == ApprovalState.SENT.value
    assert mock_db.approvals["ad-clean-1"]["sent_at"] is not None

    # Check ad_sends record
    assert len(mock_db.sends) == 1
    assert mock_db.sends[0]["status"] == "accepted"
    assert mock_db.sends[0]["is_dry_run"] is True


def test_idempotent_replay_already_sent_ad(fake_session, mock_db, stand_in_client):
    """Never-break Rule 2: Replaying send on already-sent ad is a zero-network no-op."""
    # First send
    res1 = send_ad_to_stand_in("ad-clean-1", connection=fake_session, client=stand_in_client)
    assert res1["status"] == "sent"
    assert len(STAND_IN_STATE["ads"]) == 1

    # Second send (replay)
    res2 = send_ad_to_stand_in("ad-clean-1", connection=fake_session, client=stand_in_client)
    assert res2["status"] == "sent"
    assert res2["already_sent"] is True
    assert res2["idempotent_replay"] is True

    # Stand-in platform received ZERO additional calls
    assert len(STAND_IN_STATE["ads"]) == 1


def test_stand_in_platform_replay_header_detected(fake_session, mock_db, stand_in_client):
    """Verify Idempotent-Replay header is parsed when stand-in platform replays from same key."""
    # Pre-populate stand-in platform with same key
    key = generate_idempotency_key("ad-clean-1", "hash_clean_1")
    payload = build_stand_in_ad_payload(mock_db.ads["ad-clean-1"], "hash_clean_1")

    # Manually send first to platform
    r1 = stand_in_client.post("http://stand-in/v1/ads", json=payload, headers={"Idempotency-Key": key})
    assert r1.status_code == 201

    # Now launch gate sender sends with same key
    res = send_ad_to_stand_in("ad-clean-1", connection=fake_session, client=stand_in_client)
    assert res["status"] == "sent"
    assert res["idempotent_replay"] is True
    assert len(STAND_IN_STATE["ads"]) == 1


def test_fail_after_commit_chaos_recovery(fake_session, mock_db, stand_in_client):
    """Verify recovery when platform saves ad but returns 503 (fail_after_commit)."""
    # Configure stand-in platform chaos: attempt 1 commits and returns 503
    STAND_IN_STATE["cfg"]["fail_after_commit_rate"] = 1.0

    # Sender retries with same idempotency key and recovers
    res = send_ad_to_stand_in(
        "ad-clean-1",
        connection=fake_session,
        client=stand_in_client,
        max_retries=3,
        initial_backoff=0.01,
    )

    assert res["status"] == "sent"
    # Even with 503 on first try, exactly 1 ad exists in platform DB (no duplicate!)
    assert len(STAND_IN_STATE["ads"]) == 1
    assert mock_db.approvals["ad-clean-1"]["state"] == "sent"


def test_stand_in_policy_rejection_alerts_and_returns_to_review(fake_session, mock_db, stand_in_client):
    """PRD §4.6: Platform rejection alerts a person and returns to review (no auto-relaunch)."""
    # 'ad-policy-fail' has 'Clinically proven miracle cure' matching Policy 4.2
    res = send_ad_to_stand_in("ad-policy-fail", connection=fake_session, client=stand_in_client)

    assert res["status"] == "rejected"
    assert res["platform_status"] == "rejected"
    assert "Policy" in res["reason"]

    # State updated to rejected
    assert mock_db.approvals["ad-policy-fail"]["state"] == ApprovalState.REJECTED.value
    assert "Platform rejection" in mock_db.approvals["ad-policy-fail"]["rejection_reason"]

    # Alert recorded for reviewer
    assert len(mock_db.alerts) == 1
    assert mock_db.alerts[0]["alert_type"] == "platform_rejection"
    assert "rejected by stand-in platform" in mock_db.alerts[0]["message"]


def test_send_retries_exhausted_moves_to_dead_letter(fake_session, mock_db, stand_in_client):
    """PRD §4.6 / CLAUDE.md line 69: Exhausted retries move to dead_letter with reviewer alert."""
    # Always fail before commit
    STAND_IN_STATE["cfg"]["fail_before_commit_rate"] = 1.0

    res = send_ad_to_stand_in(
        "ad-clean-1",
        connection=fake_session,
        client=stand_in_client,
        max_retries=2,
        initial_backoff=0.01,
    )

    assert res["status"] == "dead_letter"
    assert mock_db.approvals["ad-clean-1"]["state"] == ApprovalState.DEAD_LETTER.value

    # Alert recorded
    assert len(mock_db.alerts) == 1
    assert mock_db.alerts[0]["alert_type"] == "dead_letter"
    assert "Send retries exhausted" in mock_db.alerts[0]["message"]


def test_never_break_rule7_real_platform_calls_switched_off(fake_session, stand_in_client):
    """Never-break Rule 7: Real platform calls stay strictly switched off in v1."""
    # 1. Non-dry-run flag directly rejected
    with pytest.raises(RealPlatformCallDisabledError):
        send_ad_to_stand_in("ad-clean-1", connection=fake_session, client=stand_in_client, enforce_dry_run=False)

    # 2. get_platform_adapter with dry_run=False raises
    with pytest.raises(RealPlatformCallDisabledError):
        get_platform_adapter("meta", dry_run=False)

    # 3. Designed real platform adapters raise on send
    meta_adapter = MetaMarketingApiAdapter()
    with pytest.raises(RealPlatformCallDisabledError):
        meta_adapter.send_ad({}, "key")

    google_adapter = GoogleAdsApiAdapter()
    with pytest.raises(RealPlatformCallDisabledError):
        google_adapter.send_ad({}, "key")

    tiktok_adapter = TikTokAdsApiAdapter()
    with pytest.raises(RealPlatformCallDisabledError):
        tiktok_adapter.send_ad({}, "key")


# --- API Endpoint Integration Tests ---

def test_api_send_endpoint_success(fake_session, mock_db, buyer_token, stand_in_client, monkeypatch):
    """Verify POST /ads/{ad_id}/send API endpoint."""
    monkeypatch.setattr(
        "apps.worker.sender.get_platform_adapter",
        lambda *args, **kwargs: StandInPlatformAdapter(client=stand_in_client),
    )

    client = TestClient(main_api_app)
    resp = client.post(
        "/ads/ad-clean-1/send",
        headers={"Authorization": f"Bearer {buyer_token}"},
        json={"dry_run": True},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "sent"
    assert data["is_dry_run"] is True


def test_api_send_endpoint_rejects_unassigned_client(fake_session, mock_db, buyer_token):
    """Verify HTTP 403 Forbidden when buyer attempts to send ad for unassigned client (Needs: 7)."""
    client = TestClient(main_api_app)
    # buyer_1 is assigned to client_aura, ad-other-client is client_lumina
    resp = client.post(
        "/ads/ad-other-client/send",
        headers={"Authorization": f"Bearer {buyer_token}"},
        json={"dry_run": True},
    )
    assert resp.status_code == 403


def test_api_send_endpoint_rejects_non_dry_run(fake_session, mock_db, buyer_token):
    """Verify HTTP 400 Bad Request when dry_run=False is requested."""
    client = TestClient(main_api_app)
    resp = client.post(
        "/ads/ad-clean-1/send",
        headers={"Authorization": f"Bearer {buyer_token}"},
        json={"dry_run": False},
    )
    assert resp.status_code == 400
    assert "real platform calls are switched off" in resp.json()["detail"].lower()


def test_api_get_send_status_and_alerts_endpoints(fake_session, mock_db, buyer_token, stand_in_client, monkeypatch):
    """Verify GET /ads/{ad_id}/send and GET /ads/{ad_id}/alerts endpoints."""
    monkeypatch.setattr(
        "apps.worker.sender.get_platform_adapter",
        lambda *args, **kwargs: StandInPlatformAdapter(client=stand_in_client),
    )

    client = TestClient(main_api_app)

    # 1. Send ad
    client.post(
        "/ads/ad-clean-1/send",
        headers={"Authorization": f"Bearer {buyer_token}"},
        json={"dry_run": True},
    )

    # 2. Query send status
    resp_send = client.get(
        "/ads/ad-clean-1/send",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert resp_send.status_code == 200
    data_send = resp_send.json()
    assert data_send["approval_state"] == "sent"
    assert data_send["latest_send"]["status"] == "accepted"

    # 3. Query alerts
    resp_alerts = client.get(
        "/ads/ad-clean-1/alerts",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert resp_alerts.status_code == 200
    assert "alerts" in resp_alerts.json()


def test_api_send_batch_approved_endpoint(fake_session, mock_db, buyer_token, stand_in_client, monkeypatch):
    """Verify POST /batches/{batch_id}/send-approved batch send."""
    monkeypatch.setattr(
        "apps.worker.sender.get_platform_adapter",
        lambda *args, **kwargs: StandInPlatformAdapter(client=stand_in_client),
    )

    client = TestClient(main_api_app)
    resp = client.post(
        "/batches/batch-100/send-approved",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["batch_id"] == "batch-100"
    assert data["total_eligible"] >= 1
    assert data["sent"] >= 1


# --- Frontend Component Contract Tests ---

def test_ad_card_and_batch_review_screen_send_contracts():
    """Verify React components contain approval/send badges, send buttons, and terminal state checks."""
    ad_card_file = Path("apps/web/components/AdCard.jsx")
    review_screen_file = Path("apps/web/components/BatchReviewScreen.jsx")
    css_file = Path("apps/web/app/review.css")

    assert ad_card_file.exists()
    assert review_screen_file.exists()
    assert css_file.exists()

    ad_card_src = ad_card_file.read_text(encoding="utf-8")
    review_screen_src = review_screen_file.read_text(encoding="utf-8")
    css_src = css_file.read_text(encoding="utf-8")

    # AdCard assertions
    assert "badge-sent" in ad_card_src
    assert "badge-sending" in ad_card_src
    assert "badge-dead-letter" in ad_card_src
    assert "btn-send" in ad_card_src
    assert "onSend" in ad_card_src

    # BatchReviewScreen assertions
    assert "onSendAd" in review_screen_src
    assert "onSendBatch" in review_screen_src
    assert "handleSendAd" in review_screen_src
    assert "handleSendBatch" in review_screen_src
    assert "/send-approved" in review_screen_src

    # CSS styles
    assert ".badge-sent" in css_src
    assert ".badge-dead-letter" in css_src
    assert ".btn-action.btn-send" in css_src
    assert ".btn-send-batch" in css_src
