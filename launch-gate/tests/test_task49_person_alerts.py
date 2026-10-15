"""Tests for Task 49: Person alerts for rejected/uncertain results (PRD §4.6, Dataflow Architecture §1, §3, Needs: 47).

Verifies:
1. Platform rejections trigger person alerts: stand-in platform rejection creates an alert (alert_type="platform_rejection")
   and transitions ad state to 'rejected' with reason.
2. Uncertain send states trigger person alerts: network/timeout exceptions create alerts (alert_type="uncertain_send_state")
   and deferred retry releases ad back to 'approved' for human review.
3. Dead-letter queue exhaustion triggers person alerts: retries exhausted after N attempts creates alert (alert_type="dead_letter").
4. Never-break Rule 6 (No automatic relaunch):
   - Ads in 'rejected' state cannot be sent via send_ad_to_stand_in without human intervention.
   - Batch sender (send_batch_approved_ads) strictly skips ads with active unresolved rejection or uncertain alerts.
   - Ads in 'dead_letter' require explicit reviewer retry or return-to-review.
5. Stuck sending detection & reconciliation:
   - reconcile_stuck_sends flags ads stuck in 'sending' state beyond timeout, transitions to dead_letter,
     and records an alert (alert_type="stuck_sending").
   - Fresh sending ads (< timeout) are left untouched.
6. API Endpoints:
   - GET /batches/{batch_id}/alerts: lists batch alerts with ad context, filters by resolved, alert_type, severity.
   - GET /alerts: global paginated listing of person alerts scoped to assigned clients.
   - GET /alerts/summary: high-level counts of active rejections, uncertain states, and affected batches.
   - POST /alerts/{alert_id}/resolve: marks alert resolved with reviewer user_id and note.
   - POST /batches/{batch_id}/reconcile-stuck-sends: endpoint to sweep and reconcile stuck sends.
   - POST /ads/{ad_id}/alerts: manually raise a person alert on an ad.
7. Multi-tenant client scoping (Needs: 7, 47): 403 Forbidden on unassigned client access across all alert endpoints.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.alerts import (
    get_alerts_summary,
    get_batch_alerts,
    get_global_alerts,
    reconcile_stuck_sends,
    resolve_alert,
)
from apps.api.approvals import (
    ApprovalState,
    get_ad_record,
    get_or_create_ad_approval,
    transition_ad_state,
)
from apps.api.auth import create_access_token
from apps.api.database import get_session
from apps.api.main import app as main_api_app
from apps.worker.platforms import BasePlatformAdapter
from apps.worker.sender import (
    BackoffPolicy,
    UnapprovedAdError,
    get_ad_alerts,
    record_send_alert,
    send_ad_to_stand_in,
    send_batch_approved_ads,
)
from data.stand_in_platform.app import (
    STATE as STAND_IN_STATE,
    app as stand_in_app,
    reset as stand_in_reset,
)


# --- Fixtures and Mocks ---

@pytest.fixture(autouse=True)
def setup_auth(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")

    def mock_get_user_by_id(uid: str):
        if uid in ("buyer_1", "buyer_2", "admin_1"):
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
        if uid == "admin_1":
            return ["client_aura", "client_lumina"]
        return []

    monkeypatch.setattr("apps.api.users.get_user_by_id", mock_get_user_by_id)
    monkeypatch.setattr("apps.api.users.get_user_client_ids", mock_get_user_client_ids)


@pytest.fixture(autouse=True)
def reset_stand_in_platform():
    stand_in_reset()
    STAND_IN_STATE["cfg"] = {
        "fail_before_commit_rate": 0.0,
        "fail_after_commit_rate": 0.0,
        "timeout_rate": 0.0,
        "timeout_seconds": 15,
        "seed": 1,
    }


class MockDB:
    def __init__(self):
        self.batches = {
            "batch-al-1": {
                "batch_id": "batch-al-1",
                "filename": "aura_batch_alerts.csv",
                "sheet_hash": "hash_al_batch_1",
                "uploaded_by": "buyer_1",
                "status": "ready",
                "created_at": datetime(2026, 10, 14, 8, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 8, 0, 0, tzinfo=timezone.utc),
            },
            "batch-al-2": {
                "batch_id": "batch-al-2",
                "filename": "lumina_batch_alerts.csv",
                "sheet_hash": "hash_al_batch_2",
                "uploaded_by": "buyer_2",
                "status": "ready",
                "created_at": datetime(2026, 10, 14, 8, 30, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 8, 30, 0, tzinfo=timezone.utc),
            },
        }
        self.ads = {
            "ad-al-1": {
                "ad_id": "ad-al-1",
                "batch_id": "batch-al-1",
                "client_id": "client_aura",
                "row_number": 1,
                "platform": "instagram",
                "ad_text": "Aura Glow serum for radiant morning hydration.",
                "headline": "Glow Every Morning",
                "destination_url": "https://aurabeauty.com/glow",
                "creative_file": "creatives/glow.jpg",
                "row_hash": "hash_al_1",
                "sheet_hash": "hash_al_batch_1",
            },
            "ad-al-2": {
                "ad_id": "ad-al-2",
                "batch_id": "batch-al-1",
                "client_id": "client_aura",
                "row_number": 2,
                "platform": "meta",
                "ad_text": "Restorative night repair formula.",
                "headline": "Nightly Repair",
                "destination_url": "https://aurabeauty.com/night",
                "creative_file": "creatives/night.jpg",
                "row_hash": "hash_al_2",
                "sheet_hash": "hash_al_batch_1",
            },
            "ad-al-3": {
                "ad_id": "ad-al-3",
                "batch_id": "batch-al-1",
                "client_id": "client_aura",
                "row_number": 3,
                "platform": "meta",
                "ad_text": "Hydrating mist bottle.",
                "headline": "Fresh Mist",
                "destination_url": "https://aurabeauty.com/mist",
                "creative_file": "creatives/mist.jpg",
                "row_hash": "hash_al_3",
                "sheet_hash": "hash_al_batch_1",
            },
            "ad-al-lumina": {
                "ad_id": "ad-al-lumina",
                "batch_id": "batch-al-2",
                "client_id": "client_lumina",
                "row_number": 1,
                "platform": "google_search",
                "ad_text": "Lumina smart ambient fixtures.",
                "headline": "Smart Fixtures",
                "destination_url": "https://luminahome.com/ambient",
                "creative_file": "creatives/lumina.jpg",
                "row_hash": "hash_al_lumina",
                "sheet_hash": "hash_al_batch_2",
            },
        }
        self.approvals = {
            "ad-al-1": {
                "ad_id": "ad-al-1",
                "client_id": "client_aura",
                "state": "rejected",
                "content_hash": "hash_al_1",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc),
                "evidence_note": "Client lead approved",
                "evidence_file": None,
                "rejection_reason": "Platform rejection: Prohibited medical claim",
                "rejected_by": None,
                "rejected_at": datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc),
                "idempotency_key": "send:ad-al-1:hash_al_1",
                "send_attempts": 1,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc),
            },
            "ad-al-2": {
                "ad_id": "ad-al-2",
                "client_id": "client_aura",
                "state": "approved",
                "content_hash": "hash_al_2",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 9, 5, 0, tzinfo=timezone.utc),
                "evidence_note": "Client lead approved",
                "evidence_file": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-al-2:hash_al_2",
                "send_attempts": 1,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 5, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 9, 20, 0, tzinfo=timezone.utc),
            },
            "ad-al-3": {
                "ad_id": "ad-al-3",
                "client_id": "client_aura",
                "state": "sending",
                "content_hash": "hash_al_3",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 9, 10, 0, tzinfo=timezone.utc),
                "evidence_note": "Client lead approved",
                "evidence_file": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-al-3:hash_al_3",
                "send_attempts": 1,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 10, 0, tzinfo=timezone.utc),
                "updated_at": datetime.now(timezone.utc) - timedelta(minutes=15),  # 15 minutes ago (stuck)
            },
            "ad-al-lumina": {
                "ad_id": "ad-al-lumina",
                "client_id": "client_lumina",
                "state": "dead_letter",
                "content_hash": "hash_al_lumina",
                "approved_by": "buyer_2",
                "approved_at": datetime(2026, 10, 14, 9, 15, 0, tzinfo=timezone.utc),
                "evidence_note": "Lumina approved",
                "evidence_file": None,
                "rejection_reason": "Max retries (3) exhausted",
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-al-lumina:hash_al_lumina",
                "send_attempts": 3,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 15, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 9, 45, 0, tzinfo=timezone.utc),
            },
        }
        self.alerts = [
            {
                "alert_id": "alert_al_1",
                "ad_id": "ad-al-1",
                "client_id": "client_aura",
                "batch_id": "batch-al-1",
                "alert_type": "platform_rejection",
                "severity": "warning",
                "message": "Ad #1 rejected by stand-in platform: Prohibited medical claim",
                "details": {"reason": "Prohibited medical claim", "platform_ad_id": "std_rej_1"},
                "resolved": False,
                "resolved_by": None,
                "resolved_at": None,
                "created_at": datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc),
            },
            {
                "alert_id": "alert_al_2",
                "ad_id": "ad-al-2",
                "client_id": "client_aura",
                "batch_id": "batch-al-1",
                "alert_type": "uncertain_send_state",
                "severity": "warning",
                "message": "Ad #2 timed out; send outcome uncertain. Released back to approved state.",
                "details": {"attempts": 1, "sub_type": "timeout_retry_deferred"},
                "resolved": False,
                "resolved_by": None,
                "resolved_at": None,
                "created_at": datetime(2026, 10, 14, 9, 20, 0, tzinfo=timezone.utc),
            },
            {
                "alert_id": "alert_al_lumina",
                "ad_id": "ad-al-lumina",
                "client_id": "client_lumina",
                "batch_id": "batch-al-2",
                "alert_type": "dead_letter",
                "severity": "error",
                "message": "Send retries exhausted for Ad #1: 503 Service Unavailable",
                "details": {"attempts": 3},
                "resolved": False,
                "resolved_by": None,
                "resolved_at": None,
                "created_at": datetime(2026, 10, 14, 9, 45, 0, tzinfo=timezone.utc),
            },
        ]


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

            # 1. Batches lookup
            if "FROM batches" in s and "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                b = self.db.batches.get(bid)
                if b:
                    return FakeResult([(
                        b["batch_id"], b["filename"], b["sheet_hash"],
                        b["uploaded_by"], b["status"], b["created_at"], b["updated_at"]
                    )])
                return FakeResult([])

            # 2. Ads count in batch
            if "SELECT COUNT(*) FROM ads WHERE batch_id = %s AND client_id = ANY(%s)" in s:
                bid, clients = params_tuple[0], params_tuple[1]
                count = sum(1 for a in self.db.ads.values() if a["batch_id"] == bid and a["client_id"] in clients)
                return FakeResult([(count,)])

            if "SELECT COUNT(*) FROM ads WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                count = sum(1 for a in self.db.ads.values() if a["batch_id"] == bid)
                return FakeResult([(count,)])

            # 3. Batch alerts query
            if "FROM send_alerts al" in s and "al.batch_id = %s AND al.client_id = ANY(%s)" in s:
                bid, clients = params_tuple[0], params_tuple[1]
                rows = []
                for al in self.db.alerts:
                    if al["batch_id"] == bid and al["client_id"] in clients:
                        if "al.resolved = %s" in s:
                            # check resolved param
                            res_idx = [i for i, p in enumerate(params_tuple) if isinstance(p, bool)]
                            if res_idx and al["resolved"] != params_tuple[res_idx[0]]:
                                continue
                        if "al.alert_type = %s" in s:
                            at_params = [p for p in params_tuple[2:] if isinstance(p, str) and not isinstance(p, bool)]
                            if at_params and al["alert_type"] != at_params[0]:
                                continue
                        ad = self.db.ads.get(al["ad_id"], {})
                        appr = self.db.approvals.get(al["ad_id"], {})
                        rows.append((
                            al["alert_id"], al["ad_id"], al["client_id"], al["batch_id"],
                            al["alert_type"], al["severity"], al["message"], al["details"],
                            al["resolved"], al["resolved_by"], al["resolved_at"], al["created_at"],
                            ad.get("row_number"), ad.get("platform"), ad.get("headline"),
                            ad.get("ad_text"), ad.get("destination_url"), appr.get("state")
                        ))
                rows.sort(key=lambda r: r[11], reverse=True)
                return FakeResult(rows)

            # 4. Global alerts count
            if "SELECT COUNT(*)" in s and "FROM send_alerts al" in s:
                clients = params_tuple[0]
                count = 0
                for al in self.db.alerts:
                    if al["client_id"] in clients:
                        if "al.resolved = %s" in s:
                            res_idx = [i for i, p in enumerate(params_tuple) if isinstance(p, bool)]
                            if res_idx and al["resolved"] != params_tuple[res_idx[0]]:
                                continue
                        count += 1
                return FakeResult([(count,)])

            # 5. Global alerts list
            if "FROM send_alerts al" in s and "al.client_id = ANY(%s)" in s and "ORDER BY al.created_at DESC" in s:
                clients = params_tuple[0]
                rows = []
                for al in self.db.alerts:
                    if al["client_id"] in clients:
                        if "al.resolved = %s" in s:
                            res_idx = [i for i, p in enumerate(params_tuple) if isinstance(p, bool)]
                            if res_idx and al["resolved"] != params_tuple[res_idx[0]]:
                                continue
                        ad = self.db.ads.get(al["ad_id"], {})
                        appr = self.db.approvals.get(al["ad_id"], {})
                        rows.append((
                            al["alert_id"], al["ad_id"], al["client_id"], al["batch_id"],
                            al["alert_type"], al["severity"], al["message"], al["details"],
                            al["resolved"], al["resolved_by"], al["resolved_at"], al["created_at"],
                            ad.get("row_number"), ad.get("platform"), ad.get("headline"),
                            ad.get("ad_text"), ad.get("destination_url"), appr.get("state")
                        ))
                rows.sort(key=lambda r: r[11], reverse=True)
                # Apply pagination
                limit = params_tuple[-2] if len(params_tuple) >= 2 and isinstance(params_tuple[-2], int) else 50
                offset = params_tuple[-1] if len(params_tuple) >= 1 and isinstance(params_tuple[-1], int) else 0
                return FakeResult(rows[offset : offset + limit])

            # 6. Alerts summary
            if "SELECT al.alert_type, al.batch_id, al.client_id" in s and "al.resolved = FALSE" in s:
                clients = params_tuple[0]
                rows = [
                    (al["alert_type"], al["batch_id"], al["client_id"])
                    for al in self.db.alerts
                    if al["client_id"] in clients and not al["resolved"]
                ]
                return FakeResult(rows)

            # 7. Single ad lookup (11 columns)
            if "FROM ads" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                ad = self.db.ads.get(aid)
                if ad:
                    return FakeResult([(
                        ad["ad_id"], ad["batch_id"], ad["client_id"], ad["row_number"],
                        ad["platform"], ad["ad_text"], ad["headline"], ad["destination_url"],
                        ad["row_hash"], ad["sheet_hash"], ad.get("creative_file")
                    )])
                return FakeResult([])

            # 8. Single approval lookup (16 columns)
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

            # 9. Update approval state
            if "UPDATE ad_approvals" in s:
                aid = params_tuple[-1]
                appr = self.db.approvals.get(aid)
                if appr:
                    if "state = %s" in s:
                        appr["state"] = params_tuple[0]
                    if "rejection_reason = %s" in s:
                        appr["rejection_reason"] = params_tuple[1]
                    appr["updated_at"] = datetime.now(timezone.utc)
                return FakeResult([])

            # 10. Audit log insert
            if "INSERT INTO approval_audit_log" in s:
                return FakeResult([])

            # 11. Alerts for ad
            if "FROM send_alerts" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                rows = [
                    (
                        al["alert_id"], al["ad_id"], al["client_id"], al["batch_id"],
                        al["alert_type"], al["severity"], al["message"], al["details"],
                        al["resolved"], al["created_at"]
                    )
                    for al in self.db.alerts if al["ad_id"] == aid
                ]
                return FakeResult(rows)

            # 12. Insert send alert
            if "INSERT INTO send_alerts" in s:
                al = {
                    "alert_id": params_tuple[0],
                    "ad_id": params_tuple[1],
                    "client_id": params_tuple[2],
                    "batch_id": params_tuple[3],
                    "alert_type": params_tuple[4],
                    "severity": params_tuple[5],
                    "message": params_tuple[6],
                    "details": params_tuple[7],
                    "resolved": False,
                    "resolved_by": None,
                    "resolved_at": None,
                    "created_at": params_tuple[8] if len(params_tuple) > 8 else datetime.now(timezone.utc),
                }
                self.db.alerts.append(al)
                return FakeResult([])

            # 13. Select client_id from send_alerts
            if "SELECT client_id FROM send_alerts WHERE alert_id = %s" in s:
                alert_id = params_tuple[0]
                for al in self.db.alerts:
                    if al["alert_id"] == alert_id:
                        return FakeResult([(al["client_id"],)])
                return FakeResult([])

            # 14. Update send alert (resolve)
            if "UPDATE send_alerts" in s and "WHERE alert_id = %s" in s:
                user_id = params_tuple[0]
                resolved_at = params_tuple[1]
                alert_id = params_tuple[2]
                for al in self.db.alerts:
                    if al["alert_id"] == alert_id:
                        al["resolved"] = True
                        al["resolved_by"] = user_id
                        al["resolved_at"] = resolved_at
                        return FakeResult([(al["alert_id"], al["ad_id"], al["client_id"], al["batch_id"], True)])
                return FakeResult([])

            # 15. Stuck sending ads lookup
            if "FROM ads a" in s and "JOIN ad_approvals ap" in s and "ap.state = 'sending'" in s:
                bid = params_tuple[0]
                clients = params_tuple[1]
                rows = []
                for a in self.db.ads.values():
                    if a["batch_id"] == bid and a["client_id"] in clients:
                        appr = self.db.approvals.get(a["ad_id"])
                        if appr and appr["state"] == "sending":
                            rows.append((a["ad_id"], a["client_id"], a["row_number"], appr["idempotency_key"], appr["updated_at"]))
                return FakeResult(rows)

            # 16. Ads in batch for send_batch_approved_ads
            if "SELECT a.ad_id, a.client_id, ap.state FROM ads a JOIN ad_approvals ap" in s:
                bid = params_tuple[0]
                rows = []
                for a in self.db.ads.values():
                    if a["batch_id"] == bid:
                        appr = self.db.approvals.get(a["ad_id"])
                        st = appr["state"] if appr else "pending_review"
                        rows.append((a["ad_id"], a["client_id"], st))
                return FakeResult(rows)

            return FakeResult([])

    class FakeSession:
        def __init__(self, db: MockDB):
            self.cursor = FakeCursor(db)

        def execute(self, statement: str, params: Any = None):
            return self.cursor.execute(statement, params)

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def get_fake_session():
        return FakeSession(mock_db)

    monkeypatch.setattr("apps.api.database.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.alerts.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.dead_letter.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.sends.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.approvals.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.batches.get_session", get_fake_session)

    return get_fake_session


# --- Unit Tests: Alert Mechanisms & Never-break Rule 6 ---

def test_platform_rejection_creates_person_alert(mock_db: MockDB, fake_session):
    """Verify that a stand-in platform rejection immediately records an alert and transitions ad to 'rejected'."""
    # Custom adapter simulating platform rejection
    class RejectingAdapter(BasePlatformAdapter):
        platform_name = "stand_in"
        def send_ad(self, ad_payload, idempotency_key=None, timeout=None):
            class FakeResp:
                status_code = 200
                headers = {}
                def json(self):
                    return {"status": "rejected", "reason": "Headline exceeds platform limits", "platform_ad_id": "rej_123"}
            return FakeResp()

    with fake_session() as conn:
        res = send_ad_to_stand_in("ad-al-2", connection=conn, adapter=RejectingAdapter())

    assert res["status"] == "rejected"
    assert res["reason"] == "Headline exceeds platform limits"
    assert mock_db.approvals["ad-al-2"]["state"] == "rejected"

    # Alert must be present
    alerts = [a for a in mock_db.alerts if a["ad_id"] == "ad-al-2" and a["alert_type"] == "platform_rejection"]
    assert len(alerts) == 1
    assert "Headline exceeds platform limits" in alerts[0]["message"]
    assert alerts[0]["resolved"] is False


def test_rejected_ad_cannot_be_auto_relaunched(mock_db: MockDB, fake_session):
    """Verify that an ad in 'rejected' state cannot be sent (Never-break Rule 6: no automatic relaunch)."""
    with fake_session() as conn:
        with pytest.raises(UnapprovedAdError, match="Cannot send ad with approval state 'rejected'"):
            send_ad_to_stand_in("ad-al-1", connection=conn)


def test_batch_send_skips_ads_with_active_alerts(mock_db: MockDB, fake_session):
    """Verify send_batch_approved_ads strictly skips approved ads with unresolved person alerts."""
    # ad-al-2 is in 'approved' state, but has an active unresolved alert (alert_al_2: uncertain_send_state)
    with fake_session() as conn:
        res = send_batch_approved_ads("batch-al-1", connection=conn, client_ids=["client_aura"])

    # ad-al-2 was skipped due to the active unresolved alert
    assert res["sent"] == 0
    skipped_results = [r for r in res["results"] if r.get("status") == "skipped_uncertain_alert"]
    assert len(skipped_results) == 1
    assert skipped_results[0]["ad_id"] == "ad-al-2"
    assert "Human review required before relaunch" in skipped_results[0]["message"]


def test_timeout_creates_uncertain_send_state_alert(mock_db: MockDB, fake_session):
    """Verify that a timeout during send creates an 'uncertain_send_state' alert for reviewer review."""
    class TimeoutAdapter(BasePlatformAdapter):
        platform_name = "stand_in"
        def send_ad(self, ad_payload, idempotency_key=None, timeout=None):
            raise httpx.ReadTimeout("Read timed out after 5.0 seconds")

    with fake_session() as conn:
        res = send_ad_to_stand_in(
            "ad-al-2",
            connection=conn,
            adapter=TimeoutAdapter(),
            max_retries=1,
            release_to_approved_on_timeout=True,
            sleep_fn=lambda _: None,
        )

    assert res["status"] == "approved"
    assert res["released_for_retry"] is True

    # Alert of type uncertain_send_state recorded
    alerts = [a for a in mock_db.alerts if a["ad_id"] == "ad-al-2" and a["alert_type"] == "uncertain_send_state"]
    assert len(alerts) >= 1
    assert any("timed out" in a["message"] for a in alerts)


def test_reconcile_stuck_sends_detects_and_alerts(mock_db: MockDB, fake_session):
    """Verify reconcile_stuck_sends detects ads stuck in 'sending' state, transitions them, and alerts."""
    # ad-al-3 is stuck in 'sending' state (15 minutes ago)
    with fake_session() as conn:
        res = reconcile_stuck_sends(conn, "batch-al-1", ["client_aura"], timeout_seconds=300)

    assert res["total_reconciled"] == 1
    reconciled = res["reconciled_ads"][0]
    assert reconciled["ad_id"] == "ad-al-3"
    assert reconciled["reconciled_state"] == "dead_letter"

    # State in DB transitioned to dead_letter
    assert mock_db.approvals["ad-al-3"]["state"] == "dead_letter"

    # An alert for stuck_sending is recorded
    stuck_alerts = [a for a in mock_db.alerts if a["ad_id"] == "ad-al-3" and a["alert_type"] == "stuck_sending"]
    assert len(stuck_alerts) == 1
    assert "stuck in 'sending' state" in stuck_alerts[0]["message"]
    assert stuck_alerts[0]["severity"] == "error"


def test_reconcile_stuck_sends_ignores_fresh_sends(mock_db: MockDB, fake_session):
    """Verify reconcile_stuck_sends leaves fresh sending ads alone."""
    # Update ad-al-3 to be updated just 5 seconds ago
    mock_db.approvals["ad-al-3"]["updated_at"] = datetime.now(timezone.utc) - timedelta(seconds=5)

    with fake_session() as conn:
        res = reconcile_stuck_sends(conn, "batch-al-1", ["client_aura"], timeout_seconds=300)

    assert res["total_reconciled"] == 0
    assert mock_db.approvals["ad-al-3"]["state"] == "sending"


# --- API Endpoint Tests ---

def test_api_get_batch_alerts_success(mock_db: MockDB, fake_session):
    """GET /batches/{batch_id}/alerts returns 200 OK with list of alerts and ad details."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.get(
        "/batches/batch-al-1/alerts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["batch_id"] == "batch-al-1"
    assert data["total_alerts"] == 2
    assert data["unresolved_alerts"] == 2
    assert len(data["alerts"]) == 2
    assert data["alerts"][0]["headline"] is not None


def test_api_get_batch_alerts_filtering(mock_db: MockDB, fake_session):
    """GET /batches/{batch_id}/alerts filters correctly by alert_type."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.get(
        "/batches/batch-al-1/alerts?alert_type=platform_rejection",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["alerts"]) == 1
    assert data["alerts"][0]["alert_type"] == "platform_rejection"


def test_api_get_batch_alerts_forbidden_client(mock_db: MockDB, fake_session):
    """GET /batches/{batch_id}/alerts returns 403 Forbidden for unauthorized user."""
    client = TestClient(main_api_app)
    # buyer_2 only has client_lumina, but batch-al-1 belongs to client_aura
    token = create_access_token("buyer_2", role="buyer")

    resp = client.get(
        "/batches/batch-al-1/alerts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_api_get_global_alerts(mock_db: MockDB, fake_session):
    """GET /alerts returns 200 OK with global alerts across assigned clients."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.get(
        "/alerts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["alerts"]) == 2


def test_api_get_global_alerts_forbidden_client(mock_db: MockDB, fake_session):
    """GET /alerts?client_id=unassigned returns 403 Forbidden."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.get(
        "/alerts?client_id=client_lumina",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_api_get_alerts_summary(mock_db: MockDB, fake_session):
    """GET /alerts/summary returns high-level breakdown of active alerts."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.get(
        "/alerts/summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_active_alerts"] == 2
    assert data["rejections_count"] == 1
    assert data["uncertain_send_count"] == 1
    assert data["has_alerts"] is True
    assert "batch-al-1" in data["affected_batches"]


def test_api_post_resolve_alert_with_note(mock_db: MockDB, fake_session):
    """POST /alerts/{alert_id}/resolve marks alert resolved with reviewer note."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.post(
        "/alerts/alert_al_1/resolve",
        json={"resolution_note": "Reviewed policy guidelines, adjusted claim copy"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["resolved"] is True
    assert data["resolution_note"] == "Reviewed policy guidelines, adjusted claim copy"

    # DB alert is resolved
    alert = next(a for a in mock_db.alerts if a["alert_id"] == "alert_al_1")
    assert alert["resolved"] is True
    assert alert["resolved_by"] == "buyer_1"


def test_api_post_resolve_alert_forbidden(mock_db: MockDB, fake_session):
    """POST /alerts/{alert_id}/resolve returns 403 Forbidden if alert belongs to unassigned client."""
    client = TestClient(main_api_app)
    # buyer_1 has client_aura; alert_al_lumina belongs to client_lumina
    token = create_access_token("buyer_1", role="buyer")

    resp = client.post(
        "/alerts/alert_al_lumina/resolve",
        json={"resolution_note": "Attempting unauthorized resolution"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_api_post_reconcile_stuck_sends(mock_db: MockDB, fake_session):
    """POST /batches/{batch_id}/reconcile-stuck-sends sweeps and reconciles stuck ads."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.post(
        "/batches/batch-al-1/reconcile-stuck-sends?timeout_seconds=300",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_reconciled"] == 1
    assert data["reconciled_ads"][0]["ad_id"] == "ad-al-3"
    assert mock_db.approvals["ad-al-3"]["state"] == "dead_letter"


def test_api_post_create_ad_alert(mock_db: MockDB, fake_session):
    """POST /ads/{ad_id}/alerts allows reviewer to manually raise a person alert."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.post(
        "/ads/ad-al-2/alerts",
        json={
            "alert_type": "uncertain_verdict",
            "severity": "warning",
            "message": "Flagged for person review: ambiguous brand naming preference",
            "details": {"citation": "Brand Book §2.1"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["alert_type"] == "uncertain_verdict"
    assert data["ad_id"] == "ad-al-2"
    assert data["client_id"] == "client_aura"
