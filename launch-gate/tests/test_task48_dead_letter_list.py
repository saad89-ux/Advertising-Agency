"""Tests for Task 48: Dead-letter list & resolution (PRD §4.6, Dataflow Architecture §3, Needs: 46).

Verifies:
1. Batch dead-letter listing: GET /batches/{batch_id}/dead-letter returns ads whose send retries
   have been exhausted (state = 'dead_letter'), enriched with attempts, failure reason, and alerts.
2. Global dead-letter listing: GET /dead-letter returns paginated dead-letter ads across assigned clients.
3. Multi-tenant client scoping: 403 Forbidden on unassigned client access for batch and global endpoints.
4. Return to review: POST /ads/{ad_id}/return-to-review transitions dead_letter -> pending_review,
   auto-resolving active dead_letter alerts.
5. Batch return to review: POST /batches/{batch_id}/return-dead-letter-to-review returns all batch
   dead-letter ads for assigned clients back to pending review.
6. Alert resolution: POST /alerts/{alert_id}/resolve marks send alerts as acknowledged/resolved.
7. Metrics integration: calculate_batch_metrics and calculate_global_metrics surface dead_letter count and rate.
8. End-to-end dead-letter lifecycle: send exhaustion -> dead-letter list -> alert -> return to review.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api.approvals import (
    ApprovalState,
    get_ad_record,
    get_or_create_ad_approval,
    transition_ad_state,
)
from apps.api.auth import create_access_token
from apps.api.database import get_session
from apps.api.dead_letter import (
    get_batch_dead_letter_ads,
    get_global_dead_letter_ads,
    resolve_send_alert,
    return_batch_dead_letter_to_review,
    return_dead_letter_ad_to_review,
)
from apps.api.main import app as main_api_app
from apps.api.metrics import calculate_batch_metrics, calculate_global_metrics
from apps.worker.sender import get_ad_alerts, get_ad_send_records


# --- Fixtures & In-Memory Test State ---

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


class MockDB:
    def __init__(self):
        self.batches = {
            "batch-dl-1": {
                "batch_id": "batch-dl-1",
                "filename": "aura_batch.csv",
                "sheet_hash": "hash_dl_batch_1",
                "uploaded_by": "buyer_1",
                "status": "ready",
                "created_at": datetime(2026, 10, 14, 8, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 8, 0, 0, tzinfo=timezone.utc),
            },
            "batch-dl-2": {
                "batch_id": "batch-dl-2",
                "filename": "lumina_batch.csv",
                "sheet_hash": "hash_dl_batch_2",
                "uploaded_by": "buyer_2",
                "status": "ready",
                "created_at": datetime(2026, 10, 14, 8, 30, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 8, 30, 0, tzinfo=timezone.utc),
            },
        }
        self.ads = {
            "ad-dl-1": {
                "ad_id": "ad-dl-1",
                "batch_id": "batch-dl-1",
                "client_id": "client_aura",
                "row_number": 1,
                "platform": "instagram",
                "ad_text": "Aura Glow serum for radiant morning hydration.",
                "headline": "Glow Every Morning",
                "destination_url": "https://aurabeauty.com/glow",
                "creative_file": "creatives/glow.jpg",
                "row_hash": "hash_dl_1",
                "sheet_hash": "hash_dl_batch_1",
            },
            "ad-dl-2": {
                "ad_id": "ad-dl-2",
                "batch_id": "batch-dl-1",
                "client_id": "client_aura",
                "row_number": 2,
                "platform": "meta",
                "ad_text": "Clean night cream for deep restorative recovery.",
                "headline": "Nightly Repair",
                "destination_url": "https://aurabeauty.com/night",
                "creative_file": "creatives/night.jpg",
                "row_hash": "hash_dl_2",
                "sheet_hash": "hash_dl_batch_1",
            },
            "ad-dl-3": {
                "ad_id": "ad-dl-3",
                "batch_id": "batch-dl-1",
                "client_id": "client_aura",
                "row_number": 3,
                "platform": "meta",
                "ad_text": "Hydrating mist for afternoon refreshes.",
                "headline": "Instant Hydration",
                "destination_url": "https://aurabeauty.com/mist",
                "creative_file": "creatives/mist.jpg",
                "row_hash": "hash_dl_3",
                "sheet_hash": "hash_dl_batch_1",
            },
            "ad-dl-lumina": {
                "ad_id": "ad-dl-lumina",
                "batch_id": "batch-dl-2",
                "client_id": "client_lumina",
                "row_number": 1,
                "platform": "google_search",
                "ad_text": "Lumina smart home ambient light fixtures.",
                "headline": "Smart Home Lighting",
                "destination_url": "https://luminahome.com/ambient",
                "creative_file": "creatives/lumina.jpg",
                "row_hash": "hash_dl_lumina",
                "sheet_hash": "hash_dl_batch_2",
            },
        }
        self.approvals = {
            "ad-dl-1": {
                "ad_id": "ad-dl-1",
                "client_id": "client_aura",
                "state": "dead_letter",
                "content_hash": "hash_dl_1",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc),
                "evidence_note": "Client lead approved",
                "evidence_file": None,
                "rejection_reason": "Max retries (3) exhausted: 503 Service Unavailable",
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-dl-1:hash_dl_1",
                "send_attempts": 3,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc),
            },
            "ad-dl-2": {
                "ad_id": "ad-dl-2",
                "client_id": "client_aura",
                "state": "approved",
                "content_hash": "hash_dl_2",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 9, 5, 0, tzinfo=timezone.utc),
                "evidence_note": "Client lead approved",
                "evidence_file": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-dl-2:hash_dl_2",
                "send_attempts": 0,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 5, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 9, 5, 0, tzinfo=timezone.utc),
            },
            "ad-dl-3": {
                "ad_id": "ad-dl-3",
                "client_id": "client_aura",
                "state": "dead_letter",
                "content_hash": "hash_dl_3",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 9, 10, 0, tzinfo=timezone.utc),
                "evidence_note": "Client lead approved",
                "evidence_file": None,
                "rejection_reason": "Max retries (3) exhausted: ReadTimeout",
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-dl-3:hash_dl_3",
                "send_attempts": 3,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 10, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 9, 45, 0, tzinfo=timezone.utc),
            },
            "ad-dl-lumina": {
                "ad_id": "ad-dl-lumina",
                "client_id": "client_lumina",
                "state": "dead_letter",
                "content_hash": "hash_dl_lumina",
                "approved_by": "buyer_2",
                "approved_at": datetime(2026, 10, 14, 9, 15, 0, tzinfo=timezone.utc),
                "evidence_note": "Lumina approved",
                "evidence_file": None,
                "rejection_reason": "Platform rate limit 429 retries exhausted",
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-dl-lumina:hash_dl_lumina",
                "send_attempts": 3,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 15, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 9, 50, 0, tzinfo=timezone.utc),
            },
        }
        self.sends = [
            {
                "send_id": "send_1",
                "ad_id": "ad-dl-1",
                "client_id": "client_aura",
                "batch_id": "batch-dl-1",
                "idempotency_key": "send:ad-dl-1:hash_dl_1",
                "content_hash": "hash_dl_1",
                "platform": "instagram",
                "status": "failed",
                "platform_ad_id": None,
                "platform_status": None,
                "rejection_reason": None,
                "is_dry_run": True,
                "idempotent_replay": False,
                "attempt_count": 3,
                "request_payload": {},
                "response_payload": {},
                "error_message": "503 Service Unavailable",
                "created_at": datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc),
            }
        ]
        self.alerts = [
            {
                "alert_id": "alert_dl_1",
                "ad_id": "ad-dl-1",
                "client_id": "client_aura",
                "batch_id": "batch-dl-1",
                "alert_type": "dead_letter",
                "severity": "critical",
                "message": "Ad send retries exhausted. Moved to dead letter.",
                "details": {"attempts": 3, "last_error": "503 Service Unavailable"},
                "resolved": False,
                "resolved_by": None,
                "resolved_at": None,
                "created_at": datetime(2026, 10, 14, 9, 30, 0, tzinfo=timezone.utc),
            },
            {
                "alert_id": "alert_dl_lumina",
                "ad_id": "ad-dl-lumina",
                "client_id": "client_lumina",
                "batch_id": "batch-dl-2",
                "alert_type": "dead_letter",
                "severity": "critical",
                "message": "Lumina ad send exhausted.",
                "details": {"attempts": 3},
                "resolved": False,
                "resolved_by": None,
                "resolved_at": None,
                "created_at": datetime(2026, 10, 14, 9, 50, 0, tzinfo=timezone.utc),
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

            # 3. Global dead-letter count
            if "SELECT COUNT(*)" in s and "JOIN ad_approvals" in s and "ap.state = 'dead_letter'" in s:
                clients = params_tuple[0]
                batch_id_filter = params_tuple[1] if len(params_tuple) > 1 else None
                count = 0
                for a in self.db.ads.values():
                    if a["client_id"] in clients:
                        if batch_id_filter and a["batch_id"] != batch_id_filter:
                            continue
                        appr = self.db.approvals.get(a["ad_id"])
                        if appr and appr["state"] == "dead_letter":
                            count += 1
                return FakeResult([(count,)])

            # 4. Batch dead letter ads list
            if "JOIN ad_approvals" in s and "a.batch_id = %s AND a.client_id = ANY(%s) AND ap.state = 'dead_letter'" in s:
                bid, clients = params_tuple[0], params_tuple[1]
                rows = []
                for a in self.db.ads.values():
                    if a["batch_id"] == bid and a["client_id"] in clients:
                        appr = self.db.approvals.get(a["ad_id"])
                        if appr and appr["state"] == "dead_letter":
                            rows.append((
                                a["ad_id"], a["batch_id"], a["client_id"], a["row_number"],
                                a["platform"], a["headline"], a["ad_text"], a["destination_url"],
                                a.get("creative_file"), a["row_hash"], appr["state"],
                                appr["content_hash"], appr["idempotency_key"], appr["send_attempts"],
                                appr["rejection_reason"], appr["updated_at"]
                            ))
                rows.sort(key=lambda r: r[3])  # sort by row_number
                return FakeResult(rows)

            # 5. Global dead-letter ads list (params: clients, batch_id, batch_id, limit, offset)
            if "JOIN ad_approvals" in s and "a.client_id = ANY(%s)" in s and "IS NULL OR a.batch_id = %s" in s and "ap.state = 'dead_letter'" in s:
                clients = params_tuple[0]
                batch_id_filter = params_tuple[1] if len(params_tuple) > 1 else None
                limit = params_tuple[3] if len(params_tuple) > 3 else 50
                offset = params_tuple[4] if len(params_tuple) > 4 else 0
                rows = []
                for a in self.db.ads.values():
                    if a["client_id"] in clients:
                        if batch_id_filter and a["batch_id"] != batch_id_filter:
                            continue
                        appr = self.db.approvals.get(a["ad_id"])
                        if appr and appr["state"] == "dead_letter":
                            rows.append((
                                a["ad_id"], a["batch_id"], a["client_id"], a["row_number"],
                                a["platform"], a["headline"], a["ad_text"], a["destination_url"],
                                a.get("creative_file"), a["row_hash"], appr["state"],
                                appr["content_hash"], appr["idempotency_key"], appr["send_attempts"],
                                appr["rejection_reason"], appr["updated_at"]
                            ))
                rows.sort(key=lambda r: r[15], reverse=True)
                paginated = rows[offset : offset + limit]
                return FakeResult(paginated)

            # 6. Return batch dead letter ads list (SELECT a.ad_id, a.client_id)
            if "SELECT a.ad_id, a.client_id" in s and "a.batch_id = %s AND a.client_id = ANY(%s) AND ap.state = 'dead_letter'" in s:
                bid, clients = params_tuple[0], params_tuple[1]
                rows = []
                for a in self.db.ads.values():
                    if a["batch_id"] == bid and a["client_id"] in clients:
                        appr = self.db.approvals.get(a["ad_id"])
                        if appr and appr["state"] == "dead_letter":
                            rows.append((a["ad_id"], a["client_id"]))
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

            # 11. Send records for ad
            if "FROM ad_sends" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                rows = [
                    (
                        s["send_id"], s["ad_id"], s["client_id"], s["batch_id"],
                        s["idempotency_key"], s["content_hash"], s["platform"], s["status"],
                        s["platform_ad_id"], s["platform_status"], s["rejection_reason"],
                        s["is_dry_run"], s["idempotent_replay"], s["attempt_count"],
                        s["request_payload"], s["response_payload"], s["error_message"],
                        s["created_at"]
                    )
                    for s in self.db.sends if s["ad_id"] == aid
                ]
                return FakeResult(rows)

            # 12. Send alerts for ad
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

            # 13. Auto-resolve send alerts for ad
            if "UPDATE send_alerts" in s and "WHERE ad_id = %s AND alert_type = 'dead_letter'" in s:
                user_id = params_tuple[0]
                aid = params_tuple[1]
                for a in self.db.alerts:
                    if a["ad_id"] == aid and a["alert_type"] == "dead_letter":
                        a["resolved"] = True
                        a["resolved_by"] = user_id
                        a["resolved_at"] = datetime.now(timezone.utc)
                return FakeResult([])

            # 14. Resolve single alert by alert_id
            if "SELECT client_id FROM send_alerts WHERE alert_id = %s" in s:
                alert_id = params_tuple[0]
                for a in self.db.alerts:
                    if a["alert_id"] == alert_id:
                        return FakeResult([(a["client_id"],)])
                return FakeResult([])

            if "UPDATE send_alerts" in s and "WHERE alert_id = %s" in s:
                user_id = params_tuple[0]
                resolved_at = params_tuple[1]
                alert_id = params_tuple[2]
                for a in self.db.alerts:
                    if a["alert_id"] == alert_id:
                        a["resolved"] = True
                        a["resolved_by"] = user_id
                        a["resolved_at"] = resolved_at
                        return FakeResult([(a["alert_id"], a["ad_id"], a["client_id"], True)])
                return FakeResult([])

            # 15. get_batch_ads query for metrics (12 columns)
            if "FROM ads" in s and "batch_id = %s AND client_id = ANY(%s)" in s:
                bid, clients = params_tuple[0], params_tuple[1]
                rows = [
                    (
                        a["ad_id"], a["batch_id"], a["client_id"], a["row_number"],
                        a["platform"], a.get("platform_raw", a["platform"]), a["ad_text"], a["headline"],
                        a["destination_url"], a.get("creative_file"), a["row_hash"], a["sheet_hash"]
                    )
                    for a in self.db.ads.values()
                    if a["batch_id"] == bid and a["client_id"] in clients
                ]
                rows.sort(key=lambda r: r[3])
                return FakeResult(rows)

            # 16. ad_approvals query for metrics
            if "FROM ad_approvals" in s and "ad_id = ANY(%s)" in s:
                aids = params_tuple[0]
                rows = []
                for aid in aids:
                    appr = self.db.approvals.get(aid)
                    if appr:
                        rows.append((
                            appr["ad_id"], appr["state"], appr["created_at"],
                            appr["approved_at"], appr["rejected_at"], appr["updated_at"]
                        ))
                return FakeResult(rows)

            # 17. global batches query for metrics
            if "SELECT DISTINCT b.batch_id" in s:
                clients = params_tuple[0]
                rows = []
                for b in self.db.batches.values():
                    if any(a["batch_id"] == b["batch_id"] and a["client_id"] in clients for a in self.db.ads.values()):
                        rows.append((b["batch_id"], b["filename"], b["created_at"]))
                return FakeResult(rows)

            # 18. worker traces query for cost metrics
            if "FROM worker_traces" in s:
                return FakeResult([(0.0, 0, 0)])

            # 19. platform sends query for simulated rejection rates
            if "FROM ad_sends" in s and "batch_id = %s" in s:
                return FakeResult([])

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
    monkeypatch.setattr("apps.api.dead_letter.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.sends.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.approvals.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.batches.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.metrics.get_session", get_fake_session)
    monkeypatch.setattr("apps.api.alerts.get_session", get_fake_session)

    return get_fake_session


# --- Unit & Data Access Tests ---

def test_get_batch_dead_letter_ads_filters_and_enriches(mock_db: MockDB, fake_session):
    """Verify get_batch_dead_letter_ads returns only dead_letter ads and enriches them with audit data."""
    with fake_session() as conn:
        ads = get_batch_dead_letter_ads(conn, "batch-dl-1", ["client_aura"])

    # Batch 1 has ad-dl-1 (dead_letter), ad-dl-2 (approved), ad-dl-3 (dead_letter)
    assert len(ads) == 2
    ad_ids = [a["ad_id"] for a in ads]
    assert "ad-dl-1" in ad_ids
    assert "ad-dl-3" in ad_ids
    assert "ad-dl-2" not in ad_ids

    # Enriched details verification
    ad1 = next(a for a in ads if a["ad_id"] == "ad-dl-1")
    assert ad1["approval_state"] == "dead_letter"
    assert ad1["send_attempts"] == 3
    assert "503 Service Unavailable" in ad1["dead_letter_reason"]
    assert ad1["latest_send"]["send_id"] == "send_1"
    assert ad1["has_alerts"] is True
    assert len(ad1["alerts"]) == 1
    assert ad1["alerts"][0]["alert_type"] == "dead_letter"


def test_get_batch_dead_letter_ads_respects_client_scoping(mock_db: MockDB, fake_session):
    """Verify unauthorized client ads are excluded from the batch dead-letter results."""
    with fake_session() as conn:
        # Request with only lumina access
        ads = get_batch_dead_letter_ads(conn, "batch-dl-1", ["client_lumina"])
        assert len(ads) == 0

        # Request batch 2 with lumina access
        ads_lumina = get_batch_dead_letter_ads(conn, "batch-dl-2", ["client_lumina"])
        assert len(ads_lumina) == 1
        assert ads_lumina[0]["ad_id"] == "ad-dl-lumina"


def test_get_global_dead_letter_ads_pagination_and_filter(mock_db: MockDB, fake_session):
    """Verify get_global_dead_letter_ads paginates and filters correctly across visible clients."""
    with fake_session() as conn:
        # All visible to admin (aura + lumina: 3 dead_letter ads total)
        res_all = get_global_dead_letter_ads(conn, ["client_aura", "client_lumina"], limit=2, offset=0)
        assert res_all["total"] == 3
        assert len(res_all["ads"]) == 2

        # Page 2
        res_page2 = get_global_dead_letter_ads(conn, ["client_aura", "client_lumina"], limit=2, offset=2)
        assert res_page2["total"] == 3
        assert len(res_page2["ads"]) == 1

        # Client-specific filter
        res_lumina = get_global_dead_letter_ads(
            conn, ["client_aura", "client_lumina"], client_id="client_lumina"
        )
        assert res_lumina["total"] == 1
        assert len(res_lumina["ads"]) == 1
        assert res_lumina["ads"][0]["ad_id"] == "ad-dl-lumina"


def test_return_dead_letter_ad_to_review_success(mock_db: MockDB, fake_session):
    """Verify return_dead_letter_ad_to_review transitions ad to pending_review and resolves alerts."""
    with fake_session() as conn:
        res = return_dead_letter_ad_to_review(
            conn,
            ad_id="ad-dl-1",
            user_id="buyer_1",
            reason="Fixing landing page URL and retrying",
        )

    assert res["status"] == "pending_review"
    assert res["ad_id"] == "ad-dl-1"

    # Approval state in DB is now pending_review
    assert mock_db.approvals["ad-dl-1"]["state"] == "pending_review"

    # Alert is auto-resolved
    alert = next(a for a in mock_db.alerts if a["ad_id"] == "ad-dl-1")
    assert alert["resolved"] is True
    assert alert["resolved_by"] == "buyer_1"


def test_return_dead_letter_ad_to_review_invalid_state(mock_db: MockDB, fake_session):
    """Verify returning an ad not in dead_letter state raises ValueError."""
    with fake_session() as conn:
        with pytest.raises(ValueError, match="Cannot return ad in state 'approved' to review"):
            return_dead_letter_ad_to_review(
                conn,
                ad_id="ad-dl-2",  # state is 'approved'
                user_id="buyer_1",
            )


def test_return_batch_dead_letter_to_review(mock_db: MockDB, fake_session):
    """Verify return_batch_dead_letter_to_review returns all dead-letter ads in the batch."""
    with fake_session() as conn:
        res = return_batch_dead_letter_to_review(
            conn,
            batch_id="batch-dl-1",
            allowed_clients=["client_aura"],
            user_id="buyer_1",
            reason="Batch return to review after outage resolved",
        )

    assert res["total_returned"] == 2
    assert mock_db.approvals["ad-dl-1"]["state"] == "pending_review"
    assert mock_db.approvals["ad-dl-3"]["state"] == "pending_review"
    assert mock_db.approvals["ad-dl-2"]["state"] == "approved"  # untouched


def test_resolve_send_alert(mock_db: MockDB, fake_session):
    """Verify resolve_send_alert updates the alert record to resolved."""
    with fake_session() as conn:
        res = resolve_send_alert(conn, alert_id="alert_dl_1", user_id="buyer_1")

    assert res["resolved"] is True
    assert res["resolved_by"] == "buyer_1"
    alert = next(a for a in mock_db.alerts if a["alert_id"] == "alert_dl_1")
    assert alert["resolved"] is True


# --- API Endpoint Integration Tests ---

def test_api_get_batch_dead_letter_success(mock_db: MockDB, fake_session):
    """GET /batches/{batch_id}/dead-letter returns 200 OK with dead-letter ads for assigned clients."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.get(
        "/batches/batch-dl-1/dead-letter",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["batch_id"] == "batch-dl-1"
    assert data["total_dead_letter"] == 2
    assert len(data["ads"]) == 2
    assert data["ads"][0]["approval_state"] == "dead_letter"


def test_api_get_batch_dead_letter_not_found(mock_db: MockDB, fake_session):
    """GET /batches/{batch_id}/dead-letter returns 404 if batch does not exist."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.get(
        "/batches/non-existent-batch/dead-letter",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert "batch not found" in resp.json()["detail"].lower()


def test_api_get_batch_dead_letter_forbidden_client(mock_db: MockDB, fake_session):
    """GET /batches/{batch_id}/dead-letter returns 403 Forbidden if user has no assigned clients in batch."""
    client = TestClient(main_api_app)
    # buyer_2 only has access to client_lumina, but batch-dl-1 has client_aura ads
    token = create_access_token("buyer_2", role="buyer")

    resp = client.get(
        "/batches/batch-dl-1/dead-letter",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_api_get_global_dead_letter(mock_db: MockDB, fake_session):
    """GET /dead-letter returns 200 OK with global dead-letter ads across assigned clients."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.get(
        "/dead-letter",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["ads"]) == 2


def test_api_get_global_dead_letter_forbidden_filter(mock_db: MockDB, fake_session):
    """GET /dead-letter with unassigned client_id returns 403 Forbidden."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.get(
        "/dead-letter?client_id=client_lumina",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_api_post_return_ad_to_review(mock_db: MockDB, fake_session):
    """POST /ads/{ad_id}/return-to-review transitions dead_letter -> pending_review."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.post(
        "/ads/ad-dl-1/return-to-review",
        json={"reason": "Updated ad text after platform timeout"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending_review"
    assert data["ad_id"] == "ad-dl-1"
    assert mock_db.approvals["ad-dl-1"]["state"] == "pending_review"


def test_api_post_return_ad_to_review_bad_request(mock_db: MockDB, fake_session):
    """POST /ads/{ad_id}/return-to-review returns 400 Bad Request if ad is not in dead_letter."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.post(
        "/ads/ad-dl-2/return-to-review",  # approved state
        json={"reason": "Cannot return approved ad"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "cannot return ad in state" in resp.json()["detail"].lower()


def test_api_post_return_ad_to_review_forbidden_client(mock_db: MockDB, fake_session):
    """POST /ads/{ad_id}/return-to-review returns 403 Forbidden if user lacks client assignment."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_2", role="buyer")  # has client_lumina, not aura

    resp = client.post(
        "/ads/ad-dl-1/return-to-review",
        json={"reason": "Attempting unauthorized return"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_api_post_return_batch_dead_letter(mock_db: MockDB, fake_session):
    """POST /batches/{batch_id}/return-dead-letter-to-review returns all dead-letter ads in batch."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.post(
        "/batches/batch-dl-1/return-dead-letter-to-review",
        json={"reason": "Mass return to review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_returned"] == 2
    assert mock_db.approvals["ad-dl-1"]["state"] == "pending_review"
    assert mock_db.approvals["ad-dl-3"]["state"] == "pending_review"


def test_api_post_resolve_alert(mock_db: MockDB, fake_session):
    """POST /alerts/{alert_id}/resolve acknowledges and marks the alert resolved."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    resp = client.post(
        "/alerts/alert_dl_1/resolve",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["resolved"] is True
    assert data["alert_id"] == "alert_dl_1"


def test_api_post_resolve_alert_forbidden(mock_db: MockDB, fake_session):
    """POST /alerts/{alert_id}/resolve returns 403 Forbidden for an alert on unassigned client."""
    client = TestClient(main_api_app)
    # buyer_1 only has client_aura; alert_dl_lumina belongs to client_lumina
    token = create_access_token("buyer_1", role="buyer")

    resp = client.post(
        "/alerts/alert_dl_lumina/resolve",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# --- Metrics Integration Tests ---

def test_metrics_calculation_includes_dead_letter(mock_db: MockDB, fake_session):
    """Verify calculate_batch_metrics and calculate_global_metrics track dead_letter counts and rates."""
    with fake_session() as conn:
        # Batch 1 metrics for client_aura:
        # ad-dl-1 (dead_letter), ad-dl-2 (approved), ad-dl-3 (dead_letter) => 2 dead_letter out of 3 ads
        batch_metrics = calculate_batch_metrics(conn, "batch-dl-1", ["client_aura"])
        assert "dead_letter" in batch_metrics
        assert batch_metrics["dead_letter"]["count"] == 2
        assert batch_metrics["dead_letter"]["rate"] == round(2 / 3, 4)
        assert batch_metrics["dead_letter"]["has_dead_letter"] is True

        # Global metrics for client_aura
        global_metrics = calculate_global_metrics(conn, ["client_aura"])
        assert "dead_letter" in global_metrics
        assert global_metrics["dead_letter"]["count"] == 2
        assert global_metrics["dead_letter"]["rate"] == round(2 / 3, 4)
        assert global_metrics["dead_letter"]["has_dead_letter"] is True


def test_metrics_no_dead_letter(mock_db: MockDB, fake_session):
    """Verify metrics properly report zero dead_letter when all ads are approved/sent."""
    mock_db.approvals["ad-dl-1"]["state"] = "approved"
    mock_db.approvals["ad-dl-3"]["state"] = "sent"

    with fake_session() as conn:
        batch_metrics = calculate_batch_metrics(conn, "batch-dl-1", ["client_aura"])
        assert batch_metrics["dead_letter"]["count"] == 0
        assert batch_metrics["dead_letter"]["rate"] == 0.0
        assert batch_metrics["dead_letter"]["has_dead_letter"] is False


def test_api_metrics_endpoints_surface_dead_letter(mock_db: MockDB, fake_session):
    """Verify GET /batches/{batch_id}/metrics and GET /metrics return dead_letter stats."""
    client = TestClient(main_api_app)
    token = create_access_token("buyer_1", role="buyer")

    # Batch metrics endpoint
    resp_batch = client.get(
        "/batches/batch-dl-1/metrics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_batch.status_code == 200
    bdata = resp_batch.json()
    assert "dead_letter" in bdata
    assert bdata["dead_letter"]["count"] == 2
    assert bdata["dead_letter"]["has_dead_letter"] is True

    # Global metrics endpoint
    resp_global = client.get(
        "/metrics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_global.status_code == 200
    gdata = resp_global.json()
    assert "dead_letter" in gdata
    assert gdata["dead_letter"]["count"] == 2
    assert gdata["dead_letter"]["has_dead_letter"] is True
