"""Tests for Task 46: Metrics Aggregation & Reporting API (PRD §4.7, CLAUDE.md §4.7).

Verifies:
1. Problems by type: findings categorized into mechanical, page, wording, plus top rules.
2. Ads blocked: counts and rates of ads with blocked or fix needed verdicts.
3. Reviewer time: average, median, min, max seconds spent from creation to review action.
4. False alarms: count, false-alarm rate, and check_type breakdown of dismissed findings.
5. Cost per batch: total model/step cost in USD, step counts, and average cost per ad.
6. Simulated rejection rates: without gate (direct send failure rate) vs. with gate.
7. Scoping & permissions: strict isolation to assigned clients (Needs: 7, PRD §4.8).
8. Global cross-batch metrics aggregation over time.
9. Error handling: 404 for non-existent batch, 403 for unauthorized clients.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import create_access_token
from apps.api.database import get_session
from apps.api.main import app as main_api_app
from apps.api.metrics import (
    calculate_batch_metrics,
    calculate_global_metrics,
    calculate_reviewer_time_stats,
)


# --- Mock Database Setup ---

class MockDB:
    """Mock database state for comprehensive metrics testing."""

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
            "batch-metrics-1": {
                "batch_id": "batch-metrics-1",
                "uploaded_by": "buyer_1",
                "filename": "summer_campaign.xlsx",
                "sheet_hash": "hash_batch_1",
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
            },
            "batch-metrics-2": {
                "batch_id": "batch-metrics-2",
                "uploaded_by": "buyer_1",
                "filename": "autumn_campaign.csv",
                "sheet_hash": "hash_batch_2",
                "created_at": datetime(2026, 10, 14, 11, 0, 0, tzinfo=timezone.utc),
            },
        }
        self.rejected_rows = {
            "batch-metrics-1": [
                {
                    "data_row_index": 4,
                    "row_number": 4,
                    "reason_code": "malformed_row",
                    "reason": "Missing required platform column",
                    "raw_cells": ["", "Headline", "Text"],
                }
            ],
            "batch-metrics-2": [],
        }
        self.ads = {
            # Batch 1 Ads
            "ad-m1": {
                "ad_id": "ad-m1",
                "batch_id": "batch-metrics-1",
                "client_id": "client_aura",
                "row_number": 1,
                "platform": "facebook",
                "ad_text": "Clean hydrating cream.",
                "headline": "Aura Hydration",
                "destination_url": "https://auraglow.example.com/clean",
                "row_hash": "hash_m1",
                "sheet_hash": "hash_batch_1",
                "creative_file": "cream.png",
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
            },
            "ad-m2": {
                "ad_id": "ad-m2",
                "batch_id": "batch-metrics-1",
                "client_id": "client_aura",
                "row_number": 2,
                "platform": "instagram",
                "ad_text": "Clinically proven miracle cure for dark spots.",
                "headline": "Miracle Fix",
                "destination_url": "https://auraglow.example.com/cure",
                "row_hash": "hash_m2",
                "sheet_hash": "hash_batch_1",
                "creative_file": "fix.png",
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
            },
            "ad-m3": {
                "ad_id": "ad-m3",
                "batch_id": "batch-metrics-1",
                "client_id": "client_aura",
                "row_number": 3,
                "platform": "facebook",
                "ad_text": "Night recovery oil for smooth skin.",
                "headline": "Night Recovery",
                "destination_url": "https://auraglow.example.com/oil",
                "row_hash": "hash_m3",
                "sheet_hash": "hash_batch_1",
                "creative_file": "oil.png",
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
            },
            # Batch 2 Ads
            "ad-m4": {
                "ad_id": "ad-m4",
                "batch_id": "batch-metrics-2",
                "client_id": "client_aura",
                "row_number": 1,
                "platform": "facebook",
                "ad_text": "Autumn glow moisturizer.",
                "headline": "Autumn Glow",
                "destination_url": "https://auraglow.example.com/autumn",
                "row_hash": "hash_m4",
                "sheet_hash": "hash_batch_2",
                "creative_file": "autumn.png",
                "created_at": datetime(2026, 10, 14, 11, 0, 0, tzinfo=timezone.utc),
            },
        }
        self.findings = {
            "ad-m1": [],  # Clean ad -> ready
            "ad-m2": [
                {
                    "finding_id": "f-mech-1",
                    "ad_id": "ad-m2",
                    "row_number": 2,
                    "check_type": "mechanical",
                    "message": "Missing utm_campaign parameter",
                    "field": "destination_url",
                    "proposed_fix": "Add utm_campaign=aura_glow",
                    "rule_id": "UTM_CAMPAIGN_MISSING",
                    "file_ref": None,
                },
                {
                    "finding_id": "f-word-1",
                    "ad_id": "ad-m2",
                    "row_number": 2,
                    "check_type": "wording",
                    "message": "Unsubstantiated clinical claim 'Clinically proven'",
                    "field": "ad_text",
                    "proposed_fix": "Remove or substantiate clinical claim",
                    "rule_id": "CLAIM_SUBSTANTIATION",
                    "file_ref": None,
                },
            ],
            "ad-m3": [
                {
                    "finding_id": "f-page-1",
                    "ad_id": "ad-m3",
                    "row_number": 3,
                    "check_type": "page",
                    "message": "Price on landing page ($45) does not match ad ($35)",
                    "field": "ad_text",
                    "proposed_fix": "Align price with landing page",
                    "rule_id": "PAGE_PRICE_MISMATCH",
                    "file_ref": None,
                },
                {
                    "finding_id": "f-word-2",
                    "ad_id": "ad-m3",
                    "row_number": 3,
                    "check_type": "wording",
                    "message": "Advisory wording notice: 'smooth skin'",
                    "field": "ad_text",
                    "proposed_fix": None,
                    "rule_id": "WORDING_NOTICE",
                    "file_ref": None,
                },
            ],
            "ad-m4": [
                {
                    "finding_id": "f-mech-2",
                    "ad_id": "ad-m4",
                    "row_number": 1,
                    "check_type": "mechanical",
                    "message": "Headline exceeds 30 characters limit",
                    "field": "headline",
                    "proposed_fix": "Shorten headline",
                    "rule_id": "HEADLINE_TOO_LONG",
                    "file_ref": None,
                }
            ],
        }
        # Dismissed findings (False alarms): f-word-2 dismissed by reviewer
        self.dismissed_findings = {
            "f-word-2": {
                "finding_id": "f-word-2",
                "ad_id": "ad-m3",
                "batch_id": "batch-metrics-1",
                "client_id": "client_aura",
                "dismissed_by": "buyer_1",
                "reason": "Standard cosmetic puffery, approved in brand guidelines",
                "check_type": "wording",
                "rule_id": "WORDING_NOTICE",
                "created_at": datetime(2026, 10, 14, 10, 5, 0, tzinfo=timezone.utc),
            }
        }
        self.approvals = {
            "ad-m1": {
                "ad_id": "ad-m1",
                "client_id": "client_aura",
                "state": "sent",
                "content_hash": "hash_m1",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 10, 2, 0, tzinfo=timezone.utc),  # 120s review time
                "evidence_note": "Client confirmed via email",
                "evidence_file": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-m1:hash_m1",
                "send_attempts": 1,
                "sent_at": datetime(2026, 10, 14, 10, 2, 30, tzinfo=timezone.utc),
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 10, 2, 30, tzinfo=timezone.utc),
            },
            "ad-m2": {
                "ad_id": "ad-m2",
                "client_id": "client_aura",
                "state": "rejected",
                "content_hash": "hash_m2",
                "approved_by": None,
                "approved_at": None,
                "evidence_note": None,
                "evidence_file": None,
                "rejection_reason": "Policy violation and unsubstantiated claim",
                "rejected_by": "buyer_1",
                "rejected_at": datetime(2026, 10, 14, 10, 3, 0, tzinfo=timezone.utc),  # 180s review time
                "idempotency_key": None,
                "send_attempts": 0,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 10, 3, 0, tzinfo=timezone.utc),
            },
            "ad-m3": {
                "ad_id": "ad-m3",
                "client_id": "client_aura",
                "state": "pending_review",
                "content_hash": "hash_m3",
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
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
            },
            "ad-m4": {
                "ad_id": "ad-m4",
                "client_id": "client_aura",
                "state": "approved",
                "content_hash": "hash_m4",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 11, 1, 0, tzinfo=timezone.utc),  # 60s review time
                "evidence_note": "Approved by client lead",
                "evidence_file": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": None,
                "send_attempts": 0,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 11, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 11, 1, 0, tzinfo=timezone.utc),
            },
        }
        self.traces = [
            {
                "trace_id": "trace-1",
                "batch_id": "batch-metrics-1",
                "ad_id": "ad-m1",
                "client_id": "client_aura",
                "status": "completed",
                "verdict": "ready",
                "total_cost_usd": 0.001250,
                "step_count": 4,
            },
            {
                "trace_id": "trace-2",
                "batch_id": "batch-metrics-1",
                "ad_id": "ad-m2",
                "client_id": "client_aura",
                "status": "completed",
                "verdict": "blocked",
                "total_cost_usd": 0.002500,
                "step_count": 6,
            },
            {
                "trace_id": "trace-3",
                "batch_id": "batch-metrics-1",
                "ad_id": "ad-m3",
                "client_id": "client_aura",
                "status": "completed",
                "verdict": "fix needed",
                "total_cost_usd": 0.001800,
                "step_count": 5,
            },
            {
                "trace_id": "trace-4",
                "batch_id": "batch-metrics-2",
                "ad_id": "ad-m4",
                "client_id": "client_aura",
                "status": "completed",
                "verdict": "fix needed",
                "total_cost_usd": 0.001100,
                "step_count": 3,
            },
        ]
        self.sends = [
            {
                "send_id": "s-1",
                "ad_id": "ad-m1",
                "client_id": "client_aura",
                "batch_id": "batch-metrics-1",
                "platform_status": "accepted",
                "status": "accepted",
                "is_dry_run": True,
            }
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


@pytest.fixture
def fake_session(mock_db: MockDB, monkeypatch: pytest.MonkeyPatch):
    class FakeCursor:
        def __init__(self, db: MockDB):
            self.db = db

        def execute(self, statement: str, params: Any = None):
            s = statement.strip()
            params_tuple = tuple(params) if params is not None else ()

            # 1. Batches table
            if "FROM batches" in s and "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                b = self.db.batches.get(bid)
                if b:
                    return FakeResult([(
                        b["batch_id"], b["uploaded_by"], b["created_at"],
                        b["filename"], b["sheet_hash"]
                    )])
                return FakeResult([])

            # 2. Total ads count in batch
            if "SELECT COUNT(*) FROM ads WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                count = sum(1 for a in self.db.ads.values() if a["batch_id"] == bid)
                return FakeResult([(count,)])

            # 3. Visible ads query
            if "FROM ads" in s and "WHERE batch_id = %s" in s and "client_id = ANY" in s:
                bid = params_tuple[0]
                cids = params_tuple[1]
                rows = [
                    (
                        a["ad_id"],
                        a["batch_id"],
                        a["client_id"],
                        a["row_number"],
                        a["platform"],
                        a.get("platform_raw") or a["platform"],
                        a["ad_text"],
                        a["headline"],
                        a["destination_url"],
                        a.get("creative_file"),
                        a["row_hash"],
                        a["sheet_hash"],
                    )
                    for a in self.db.ads.values()
                    if a["batch_id"] == bid and a["client_id"] in cids
                ]
                return FakeResult(rows)

            # 4. Rejected rows query
            if "FROM batch_rejected_rows" in s and "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                rows = [
                    (r["data_row_index"], r["row_number"], r["reason_code"], r["reason"], r["raw_cells"])
                    for r in self.db.rejected_rows.get(bid, [])
                ]
                return FakeResult(rows)

            # 5. Dismissed findings query
            if "FROM dismissed_findings" in s:
                aids = params_tuple[0]
                rows = [
                    (
                        d.get("dismissal_id", 1),
                        d["finding_id"],
                        d["ad_id"],
                        d.get("batch_id", "batch-1"),
                        d.get("client_id", "client_aura"),
                        d["dismissed_by"],
                        d["reason"],
                        d["created_at"],
                    )
                    for d in self.db.dismissed_findings.values()
                    if d["ad_id"] in aids or d["finding_id"] in aids
                ]
                return FakeResult(rows)

            # 6. Findings query
            if "FROM findings" in s and "ad_id = ANY" in s:
                aids = params_tuple[0]
                rows = []
                for aid in aids:
                    for f in self.db.findings.get(aid, []):
                        rows.append((
                            f["finding_id"], f["ad_id"], f["row_number"], f["check_type"],
                            f["message"], f["field"], f["proposed_fix"], f["rule_id"], f["file_ref"]
                        ))
                return FakeResult(rows)

            # 7. Worker traces verdict query
            if "SELECT ad_id, verdict FROM worker_traces" in s:
                bid = params_tuple[0]
                aids = params_tuple[1]
                rows = [
                    (t["ad_id"], t["verdict"])
                    for t in self.db.traces
                    if t["batch_id"] == bid and t["ad_id"] in aids
                ]
                return FakeResult(rows)

            # 8. Worker traces cost query
            if "SELECT COALESCE(SUM(total_cost_usd)" in s:
                bid = params_tuple[0]
                cids = params_tuple[1]
                matching = [
                    t for t in self.db.traces
                    if t["batch_id"] == bid and t["client_id"] in cids
                ]
                cost = sum(t["total_cost_usd"] for t in matching)
                steps = sum(t["step_count"] for t in matching)
                return FakeResult([(cost, steps, len(matching))])

            # 9. Ad approvals query for reviewer time and states
            if "FROM ad_approvals" in s and "WHERE ad_id = ANY" in s:
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

            # 10. Platform sends query
            if "FROM ad_sends" in s and "WHERE batch_id = %s" in s:
                bid = params_tuple[0]
                cids = params_tuple[1]
                counts: dict[str, int] = {}
                for s_rec in self.db.sends:
                    if s_rec["batch_id"] == bid and s_rec["client_id"] in cids:
                        stat = s_rec["platform_status"]
                        counts[stat] = counts.get(stat, 0) + 1
                return FakeResult([(k, v) for k, v in counts.items()])

            # 11. Cross-batch distinct query
            if "SELECT DISTINCT b.batch_id, b.filename, b.created_at" in s:
                cids = params_tuple[0]
                bids = set()
                rows = []
                for ad in self.db.ads.values():
                    if ad["client_id"] in cids and ad["batch_id"] not in bids:
                        bids.add(ad["batch_id"])
                        b = self.db.batches[ad["batch_id"]]
                        rows.append((b["batch_id"], b["filename"], b["created_at"]))
                return FakeResult(rows)

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
    monkeypatch.setattr("apps.api.metrics.get_session", lambda: session)
    monkeypatch.setattr("apps.api.batches.get_session", lambda: session)
    return session


@pytest.fixture
def buyer_token():
    return create_access_token("buyer_1", role="buyer")


@pytest.fixture
def lumina_buyer_token():
    return create_access_token("buyer_2", role="buyer")


# --- Unit Tests: Metric Calculations ---

def test_reviewer_time_stats_calculation():
    """Verify average, median, min, max review duration calculation."""
    now = datetime.now(timezone.utc)
    approvals = [
        {"created_at": now - timedelta(seconds=60), "approved_at": now},  # 60s
        {"created_at": now - timedelta(seconds=120), "rejected_at": now},  # 120s
        {"created_at": now - timedelta(seconds=30), "approved_at": now},  # 30s
    ]

    stats = calculate_reviewer_time_stats(approvals)
    assert stats["reviewed_count"] == 3
    assert stats["min_seconds"] == 30.0
    assert stats["max_seconds"] == 120.0
    assert stats["avg_seconds"] == 70.0  # (60 + 120 + 30) / 3 = 70.0
    assert stats["median_seconds"] == 60.0
    assert stats["formatted_avg"] == "1m 10s"


def test_reviewer_time_stats_empty():
    """Verify reviewer time defaults cleanly when no ads have been reviewed."""
    stats = calculate_reviewer_time_stats([])
    assert stats["reviewed_count"] == 0
    assert stats["avg_seconds"] == 0.0
    assert stats["median_seconds"] == 0.0
    assert stats["formatted_avg"] == "0s"


def test_batch_metrics_problems_by_type(fake_session, mock_db):
    """Verify findings categorization into mechanical, page, and wording plus top rules."""
    m = calculate_batch_metrics(fake_session, "batch-metrics-1", ["client_aura"])
    probs = m["problems_by_type"]

    assert probs["total_findings"] == 4  # 0 on ad-m1, 2 on ad-m2, 2 on ad-m3
    assert probs["active_findings"] == 3
    assert probs["dismissed_findings"] == 1

    by_type = probs["by_check_type"]
    assert by_type["mechanical"] == 1  # UTM_CAMPAIGN_MISSING
    assert by_type["page"] == 1  # PAGE_PRICE_MISMATCH
    assert by_type["wording"] == 2  # CLAIM_SUBSTANTIATION + WORDING_NOTICE

    # Top rules check
    top_rule_ids = [r["rule_id"] for r in probs["top_rules"]]
    assert "UTM_CAMPAIGN_MISSING" in top_rule_ids
    assert "CLAIM_SUBSTANTIATION" in top_rule_ids


def test_batch_metrics_ads_blocked(fake_session, mock_db):
    """Verify counts and rates of blocked / fix needed ads."""
    m = calculate_batch_metrics(fake_session, "batch-metrics-1", ["client_aura"])
    blocked = m["ads_blocked"]

    # ad-m1 is ready, ad-m2 is blocked, ad-m3 is fix needed
    assert blocked["blocked_count"] == 1
    assert blocked["fix_needed_count"] == 1
    assert blocked["total_blocked"] == 2
    assert blocked["blocked_rate"] == round(2 / 3, 4)


def test_batch_metrics_false_alarms(fake_session, mock_db):
    """Verify false alarm calculation from dismissed findings."""
    m = calculate_batch_metrics(fake_session, "batch-metrics-1", ["client_aura"])
    fa = m["false_alarms"]

    assert fa["dismissed_count"] == 1
    assert fa["total_findings"] == 4
    assert fa["false_alarm_rate"] == round(1 / 4, 4)
    assert fa["by_check_type"]["wording"] == 1


def test_batch_metrics_cost(fake_session, mock_db):
    """Verify total cost in USD and average cost per ad."""
    m = calculate_batch_metrics(fake_session, "batch-metrics-1", ["client_aura"])
    cost = m["cost"]

    # Sum of traces 1, 2, 3: 0.00125 + 0.0025 + 0.0018 = 0.00555
    assert cost["total_cost_usd"] == 0.00555
    assert cost["traces_count"] == 3
    assert cost["avg_cost_per_ad_usd"] == round(0.00555 / 3, 6)
    assert cost["total_steps"] == 15  # 4 + 6 + 5


def test_batch_metrics_simulated_rejection_rates(fake_session, mock_db):
    """Verify simulated rejection rate without gate vs with gate."""
    m = calculate_batch_metrics(fake_session, "batch-metrics-1", ["client_aura"])
    rates = m["simulated_rejection_rates"]

    # Without gate: 2 blocked/fix needed ads + 1 malformed row = 3 rejections out of 4 total rows
    assert rates["without_gate_rejection_rate"] == round(3 / 4, 4)

    # With gate: ad-m1 was sent and accepted (0 rejected on platform)
    assert rates["with_gate_rejection_rate"] == 0.0
    assert rates["ads_prevented_from_rejection"] == 2


def test_batch_metrics_approval_states(fake_session, mock_db):
    """Verify distribution of approval states across batch ads."""
    m = calculate_batch_metrics(fake_session, "batch-metrics-1", ["client_aura"])
    states = m["approval_states"]

    assert states["sent"] == 1  # ad-m1
    assert states["rejected"] == 1  # ad-m2
    assert states["pending_review"] == 1  # ad-m3
    assert states["approved"] == 0


def test_global_metrics_cross_batch_aggregation(fake_session, mock_db):
    """Verify global aggregation across multiple batches for assigned clients."""
    g = calculate_global_metrics(fake_session, ["client_aura"])

    assert g["total_batches"] == 2
    assert g["total_ads"] == 4  # 3 in batch 1, 1 in batch 2
    assert len(g["batches"]) == 2
    assert g["total_cost_usd"] > 0
    assert g["overall_blocked_rate"] > 0


# --- API Endpoint Integration Tests ---

def test_api_get_batch_metrics_success(fake_session, mock_db, buyer_token):
    """Verify GET /batches/{batch_id}/metrics endpoint."""
    client = TestClient(main_api_app)
    resp = client.get(
        "/batches/batch-metrics-1/metrics",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )

    assert resp.status_code == 200
    data = resp.json()

    assert data["batch_id"] == "batch-metrics-1"
    assert "problems_by_type" in data
    assert "ads_blocked" in data
    assert "reviewer_time" in data
    assert "false_alarms" in data
    assert "cost" in data
    assert "simulated_rejection_rates" in data
    assert data["totals"]["total_ads"] == 3


def test_api_get_batch_metrics_404_not_found(fake_session, mock_db, buyer_token):
    """Verify HTTP 404 Not Found for non-existent batch."""
    client = TestClient(main_api_app)
    resp = client.get(
        "/batches/batch-does-not-exist/metrics",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert resp.status_code == 404


def test_api_get_batch_metrics_403_unauthorized_client(fake_session, mock_db, lumina_buyer_token):
    """Verify HTTP 403 Forbidden when requesting batch for unassigned client (Needs: 7)."""
    client = TestClient(main_api_app)
    # buyer_2 (Lumina) requests batch-metrics-1 (Aura Glow only)
    resp = client.get(
        "/batches/batch-metrics-1/metrics",
        headers={"Authorization": f"Bearer {lumina_buyer_token}"},
    )
    assert resp.status_code == 403


def test_api_get_global_metrics_success(fake_session, mock_db, buyer_token):
    """Verify GET /metrics endpoint returns cross-batch operational summary."""
    client = TestClient(main_api_app)
    resp = client.get(
        "/metrics",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_batches"] == 2
    assert data["total_ads"] == 4
    assert "problems_by_type" in data
    assert len(data["batches"]) == 2


def test_api_get_global_metrics_filtered_by_client(fake_session, mock_db, buyer_token):
    """Verify GET /metrics?client_id=client_aura filters by assigned client."""
    client = TestClient(main_api_app)
    resp = client.get(
        "/metrics?client_id=client_aura",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["clients"] == ["client_aura"]


def test_api_get_global_metrics_rejects_unassigned_client(fake_session, mock_db, buyer_token):
    """Verify GET /metrics?client_id=client_lumina returns HTTP 403 for unassigned client."""
    client = TestClient(main_api_app)
    # buyer_1 is assigned to client_aura, attempts to query client_lumina
    resp = client.get(
        "/metrics?client_id=client_lumina",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert resp.status_code == 403
