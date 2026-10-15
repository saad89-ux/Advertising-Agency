"""Unit and integration tests for Task 51: Metrics Computation.

Needs:
- 19: Findings persistence and check types (mechanical, page, wording, rule_id, field).
- 36: Worker traces, step counts, total cost in USD, and step cap detection.
- 43: False alarms, reviewer dismissals, rejection reasons, and cleaned ads count.
- 47: Platform sends, retry attempts, timeouts, dead-letter queue, and acceptance rates.
- PRD §4.7 / §4.8 & Proof Table: CI release gate evaluation (false alarms <= 5%, dead letter <= 5%).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import pytest

from apps.api import auth, main, metrics
from apps.api.auth import create_access_token
from apps.api.database import get_session
from apps.api.main import app
from apps.api.metrics import (
    calculate_batch_metrics,
    calculate_global_metrics,
)


class FakeMetricsComputationDB:
    """Mock database simulating all data sources needed for metrics computation."""

    def __init__(self):
        self.batches = {
            "batch-comp-1": {
                "batch_id": "batch-comp-1",
                "uploaded_by": "buyer-1",
                "filename": "q4_campaign.csv",
                "sheet_hash": "hash_comp_1",
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
            },
            "batch-comp-2": {
                "batch_id": "batch-comp-2",
                "uploaded_by": "buyer-1",
                "filename": "q4_campaign_2.xlsx",
                "sheet_hash": "hash_comp_2",
                "created_at": datetime(2026, 10, 14, 11, 0, 0, tzinfo=timezone.utc),
            },
        }

        self.rejected_rows = {
            "batch-comp-1": [
                {
                    "data_row_index": 5,
                    "row_number": 5,
                    "reason_code": "malformed_row",
                    "reason": "Missing required platform column",
                    "raw_cells": ["", "Headline", "Text"],
                }
            ],
            "batch-comp-2": [],
        }

        # 4 Ads in Batch 1:
        # ad-1: clean (verdict ready, sent, accepted, 1 attempt)
        # ad-2: blocked (verdict blocked, mechanical text limit + wording notice, rejected with reason)
        # ad-3: false-alarm cleaned (had wording notice, dismissed by reviewer -> became ready, sent, accepted, retried 2 attempts)
        # ad-4: dead-letter (verdict ready, sent but retries exhausted -> dead_letter, 3 attempts)
        self.ads = {
            "ad-1": {
                "ad_id": "ad-1",
                "batch_id": "batch-comp-1",
                "client_id": "brightleaf",
                "row_number": 1,
                "platform": "facebook",
                "ad_text": "Clean natural tea for relaxation.",
                "headline": "Brightleaf Tea",
                "destination_url": "https://brightleaf.example.com/tea",
                "row_hash": "hash_ad_1",
                "sheet_hash": "hash_comp_1",
                "creative_file": "tea.png",
            },
            "ad-2": {
                "ad_id": "ad-2",
                "batch_id": "batch-comp-1",
                "client_id": "brightleaf",
                "row_number": 2,
                "platform": "instagram",
                "ad_text": "This miraculous cure dissolves stress instantly without effort.",
                "headline": "Miracle Stress Cure Now",
                "destination_url": "https://brightleaf.example.com/cure",
                "row_hash": "hash_ad_2",
                "sheet_hash": "hash_comp_1",
                "creative_file": "cure.png",
            },
            "ad-3": {
                "ad_id": "ad-3",
                "batch_id": "batch-comp-1",
                "client_id": "brightleaf",
                "row_number": 3,
                "platform": "google",
                "ad_text": "Pure botanical bliss in every cup.",
                "headline": "Botanical Bliss",
                "destination_url": "https://brightleaf.example.com/bliss",
                "row_hash": "hash_ad_3",
                "sheet_hash": "hash_comp_1",
                "creative_file": "bliss.png",
            },
            "ad-4": {
                "ad_id": "ad-4",
                "batch_id": "batch-comp-1",
                "client_id": "brightleaf",
                "row_number": 4,
                "platform": "tiktok",
                "ad_text": "Enjoy fresh herbal infusions daily.",
                "headline": "Herbal Infusion",
                "destination_url": "https://brightleaf.example.com/herbal",
                "row_hash": "hash_ad_4",
                "sheet_hash": "hash_comp_1",
                "creative_file": "herbal.mp4",
            },
            # Batch 2 has ad-5 for calmleaf
            "ad-5": {
                "ad_id": "ad-5",
                "batch_id": "batch-comp-2",
                "client_id": "calmleaf",
                "row_number": 1,
                "platform": "meta",
                "ad_text": "Calmleaf organic tea blend.",
                "headline": "Calmleaf Organic",
                "destination_url": "https://calmleaf.example.com/blend",
                "row_hash": "hash_ad_5",
                "sheet_hash": "hash_comp_2",
                "creative_file": "blend.png",
            },
        }

        # Needs 19: Findings
        self.findings = {
            "ad-1": [],
            "ad-2": [
                {
                    "finding_id": "f-mech-1",
                    "ad_id": "ad-2",
                    "row_number": 2,
                    "check_type": "mechanical",
                    "message": "Headline exceeds platform character limit",
                    "field": "headline",
                    "proposed_fix": "Shorten headline under 25 chars",
                    "rule_id": "PLATFORM_HEADLINE_LIMIT",
                    "file_ref": None,
                },
                {
                    "finding_id": "f-page-1",
                    "ad_id": "ad-2",
                    "row_number": 2,
                    "check_type": "page",
                    "message": "Destination URL returned 404 page error",
                    "field": "destination_url",
                    "proposed_fix": "Fix broken landing page URL",
                    "rule_id": "PAGE_STATUS_OK",
                    "file_ref": "page_snapshot.html",
                },
                {
                    "finding_id": "f-word-1",
                    "ad_id": "ad-2",
                    "row_number": 2,
                    "check_type": "wording",
                    "message": "Substantiation required for word 'miraculous cure'",
                    "field": "ad_text",
                    "proposed_fix": "Remove unsupported health cure claim",
                    "rule_id": "UNSUBSTANTIATED_CLAIM",
                    "file_ref": None,
                },
            ],
            "ad-3": [
                {
                    "finding_id": "f-word-2",
                    "ad_id": "ad-3",
                    "row_number": 3,
                    "check_type": "wording",
                    "message": "Notice: 'botanical bliss' may be subjective puffery",
                    "field": "ad_text",
                    "proposed_fix": None,
                    "rule_id": "WORDING_PUFFERY_NOTICE",
                    "file_ref": None,
                }
            ],
            "ad-4": [],
        }

        # Needs 43: Dismissed findings (False Alarms)
        self.dismissed_findings = {
            "f-word-2": {
                "finding_id": "f-word-2",
                "ad_id": "ad-3",
                "batch_id": "batch-comp-1",
                "client_id": "brightleaf",
                "dismissed_by": "buyer-1",
                "reason": "Approved puffery per brand style guide",
                "check_type": "wording",
                "rule_id": "WORDING_PUFFERY_NOTICE",
                "created_at": datetime(2026, 10, 14, 10, 5, 0, tzinfo=timezone.utc),
            }
        }

        # Needs 36: Worker traces (steps, cost in USD)
        self.traces = [
            # ad-1: 3 steps, $0.0012
            {"batch_id": "batch-comp-1", "ad_id": "ad-1", "client_id": "brightleaf", "step_count": 3, "total_cost_usd": 0.0012, "verdict": "ready"},
            # ad-2: 5 steps (step capped), $0.0085
            {"batch_id": "batch-comp-1", "ad_id": "ad-2", "client_id": "brightleaf", "step_count": 5, "total_cost_usd": 0.0085, "verdict": "blocked"},
            # ad-3: 3 steps, $0.0020
            {"batch_id": "batch-comp-1", "ad_id": "ad-3", "client_id": "brightleaf", "step_count": 3, "total_cost_usd": 0.0020, "verdict": "ready"},
            # ad-4: 2 steps, $0.0008
            {"batch_id": "batch-comp-1", "ad_id": "ad-4", "client_id": "brightleaf", "step_count": 2, "total_cost_usd": 0.0008, "verdict": "ready"},
        ]

        # Approvals & Reviewer actions
        self.approvals = {
            "ad-1": {
                "ad_id": "ad-1",
                "client_id": "brightleaf",
                "state": "sent",
                "approved_by": "buyer-1",
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
                "approved_at": datetime(2026, 10, 14, 10, 1, 30, tzinfo=timezone.utc),  # 90s
                "rejected_at": None,
                "rejection_reason": None,
                "updated_at": datetime(2026, 10, 14, 10, 2, 0, tzinfo=timezone.utc),
            },
            "ad-2": {
                "ad_id": "ad-2",
                "client_id": "brightleaf",
                "state": "rejected",
                "approved_by": None,
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
                "approved_at": None,
                "rejected_at": datetime(2026, 10, 14, 10, 3, 0, tzinfo=timezone.utc),  # 180s
                "rejection_reason": "Severe policy violation: unverified health cure",
                "updated_at": datetime(2026, 10, 14, 10, 3, 0, tzinfo=timezone.utc),
            },
            "ad-3": {
                "ad_id": "ad-3",
                "client_id": "brightleaf",
                "state": "sent",
                "approved_by": "buyer-1",
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
                "approved_at": datetime(2026, 10, 14, 10, 2, 0, tzinfo=timezone.utc),  # 120s
                "rejected_at": None,
                "rejection_reason": None,
                "updated_at": datetime(2026, 10, 14, 10, 2, 30, tzinfo=timezone.utc),
            },
            "ad-4": {
                "ad_id": "ad-4",
                "client_id": "brightleaf",
                "state": "dead_letter",
                "approved_by": "buyer-1",
                "created_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
                "approved_at": datetime(2026, 10, 14, 10, 1, 0, tzinfo=timezone.utc),  # 60s
                "rejected_at": None,
                "rejection_reason": None,
                "updated_at": datetime(2026, 10, 14, 10, 5, 0, tzinfo=timezone.utc),
            },
        }

        # Needs 47: Platform sends, retry attempts, timeouts, dead-letter records
        self.sends = [
            # ad-1: sent accepted, 1 attempt
            {"ad_id": "ad-1", "batch_id": "batch-comp-1", "client_id": "brightleaf", "platform_status": "accepted", "attempt_count": 1, "status": "sent"},
            # ad-3: sent accepted, 2 attempts (retried after 503)
            {"ad_id": "ad-3", "batch_id": "batch-comp-1", "client_id": "brightleaf", "platform_status": "accepted", "attempt_count": 2, "status": "sent"},
            # ad-4: retries exhausted -> dead_letter, 3 attempts
            {"ad_id": "ad-4", "batch_id": "batch-comp-1", "client_id": "brightleaf", "platform_status": "rejected", "attempt_count": 3, "status": "dead_letter"},
        ]

    @contextmanager
    def session(self) -> Any:
        db = self

        class Connection:
            def execute(self, statement: str, params: Any = None) -> Any:
                s = " ".join(statement.strip().split())
                p = tuple(params) if params else ()

                # 1. Batches lookup
                if "FROM batches" in s and "WHERE batch_id = %s" in s:
                    bid = p[0]
                    b = db.batches.get(bid)
                    if b:
                        return FakeResult([(b["batch_id"], b["uploaded_by"], b["created_at"], b["filename"], b["sheet_hash"])])
                    return FakeResult([])

                # 2. Total ads count
                if "SELECT COUNT(*) FROM ads WHERE batch_id = %s" in s:
                    bid = p[0]
                    cnt = sum(1 for a in db.ads.values() if a["batch_id"] == bid)
                    return FakeResult([(cnt,)])

                # 3. Visible ads query
                if "FROM ads" in s and "batch_id = %s AND client_id = ANY" in s:
                    bid, cids = p
                    allowed = set(cids)
                    rows = [
                        (
                            a["ad_id"], a["batch_id"], a["client_id"], a["row_number"],
                            a["platform"], a["platform"], a["ad_text"], a["headline"],
                            a["destination_url"], a.get("creative_file"), a["row_hash"], a["sheet_hash"]
                        )
                        for a in db.ads.values()
                        if a["batch_id"] == bid and a["client_id"] in allowed
                    ]
                    rows.sort(key=lambda r: r[3])
                    return FakeResult(rows)

                # 4. Rejected rows query
                if "FROM batch_rejected_rows" in s and "WHERE batch_id = %s" in s:
                    bid = p[0]
                    rows = [
                        (r["data_row_index"], r["row_number"], r["reason_code"], r["reason"], r["raw_cells"])
                        for r in db.rejected_rows.get(bid, [])
                    ]
                    return FakeResult(rows)

                # 5. Dismissed findings query
                if "FROM dismissed_findings" in s:
                    aids = set(p[0])
                    rows = [
                        (
                            1, d["finding_id"], d["ad_id"], d["batch_id"], d["client_id"],
                            d["dismissed_by"], d["reason"], d["created_at"]
                        )
                        for d in db.dismissed_findings.values()
                        if d["ad_id"] in aids or d["finding_id"] in aids
                    ]
                    return FakeResult(rows)

                # 6. Findings query
                if "FROM findings" in s and "ad_id = ANY" in s:
                    aids = set(p[0])
                    rows = []
                    for aid in aids:
                        for f in db.findings.get(aid, []):
                            rows.append((
                                f["finding_id"], f["ad_id"], f["row_number"], f["check_type"],
                                f["message"], f["field"], f["proposed_fix"], f["rule_id"], f["file_ref"]
                            ))
                    return FakeResult(rows)

                # 7. Worker traces verdict query
                if "SELECT ad_id, verdict FROM worker_traces" in s:
                    bid, aids = p[0], set(p[1])
                    return FakeResult([
                        (t["ad_id"], t["verdict"])
                        for t in db.traces
                        if t["batch_id"] == bid and t["ad_id"] in aids
                    ])

                # 8. Worker traces cost query
                if "SELECT COALESCE(SUM(total_cost_usd)" in s:
                    bid, cids = p[0], set(p[1])
                    matching = [t for t in db.traces if t["batch_id"] == bid and t["client_id"] in cids]
                    cost = sum(t["total_cost_usd"] for t in matching)
                    steps = sum(t["step_count"] for t in matching)
                    return FakeResult([(cost, steps, len(matching))])

                # 9. Step capped ads count query
                if "SELECT COUNT(*) FROM worker_traces" in s and "step_count >= 5" in s:
                    bid, cids = p[0], set(p[1])
                    capped = sum(1 for t in db.traces if t["batch_id"] == bid and t["client_id"] in cids and t["step_count"] >= 5)
                    return FakeResult([(capped,)])

                # 10. Approvals query
                if "FROM ad_approvals" in s and "WHERE ad_id = ANY" in s and "SELECT rejection_reason" not in s:
                    aids = set(p[0])
                    rows = [
                        (
                            appr["ad_id"], appr["state"], appr["created_at"],
                            appr["approved_at"], appr["rejected_at"], appr["updated_at"]
                        )
                        for aid, appr in db.approvals.items()
                        if aid in aids
                    ]
                    return FakeResult(rows)

                # 11. Rejection reasons query
                if "SELECT rejection_reason, COUNT(*)" in s:
                    aids = set(p[0])
                    reasons: dict[str, int] = {}
                    for aid in aids:
                        appr = db.approvals.get(aid)
                        if appr and appr["state"] == "rejected" and appr.get("rejection_reason"):
                            r = appr["rejection_reason"]
                            reasons[r] = reasons.get(r, 0) + 1
                    return FakeResult([(k, v) for k, v in reasons.items()])

                # 12. Ad sends platform status query
                if "SELECT platform_status, COUNT(*)" in s:
                    bid, cids = p[0], set(p[1])
                    matching = [s_rec for s_rec in db.sends if s_rec["batch_id"] == bid and s_rec["client_id"] in cids]
                    stats: dict[str, int] = {}
                    for rec in matching:
                        pst = rec.get("platform_status", "")
                        stats[pst] = stats.get(pst, 0) + 1
                    return FakeResult([(k, v) for k, v in stats.items()])

                # 13. Ad sends attempts & retried query
                if "SELECT COALESCE(SUM(attempt_count)" in s:
                    bid, cids = p[0], set(p[1])
                    matching = [s_rec for s_rec in db.sends if s_rec["batch_id"] == bid and s_rec["client_id"] in cids]
                    tot_attempts = sum(s_rec["attempt_count"] for s_rec in matching)
                    retried_cnt = len(set(s_rec["ad_id"] for s_rec in matching if s_rec["attempt_count"] > 1))
                    return FakeResult([(tot_attempts, retried_cnt)])

                # 14. Global distinct batches query
                if "SELECT DISTINCT b.batch_id" in s:
                    cids = set(p[0])
                    matching_b = []
                    for b in db.batches.values():
                        if any(a["client_id"] in cids for a in db.ads.values() if a["batch_id"] == b["batch_id"]):
                            matching_b.append((b["batch_id"], b["filename"], b["created_at"]))
                    return FakeResult(matching_b)

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


def setup_users(monkeypatch: pytest.MonkeyPatch) -> None:
    users_db = {
        "buyer-1": {"user_id": "buyer-1", "email": "buyer1@agency.test", "display_name": "Buyer One", "role": "buyer", "is_active": True},
        "buyer-2": {"user_id": "buyer-2", "email": "buyer2@agency.test", "display_name": "Buyer Two", "role": "buyer", "is_active": True},
    }
    user_clients = {
        "buyer-1": ["brightleaf"],
        "buyer-2": ["calmleaf"],
    }
    monkeypatch.setattr(auth.users, "get_user_by_id", lambda uid: users_db.get(uid))
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda uid: user_clients.get(uid, []))


# --- Comprehensive Tests for Task 51 ---

def test_needs_19_problems_by_type_and_field_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Needs: 19 - Verify findings grouped by mechanical, page, wording, field, and top rules."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token = create_access_token("buyer-1", "buyer")
    with TestClient(app) as client:
        res = client.get("/batches/batch-comp-1/metrics", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()

        # Needs 19: Check type breakdown
        problems = data["problems_by_type"]
        assert problems["total_findings"] == 4
        assert problems["active_findings"] == 3
        assert problems["dismissed_findings"] == 1
        assert problems["by_check_type"]["mechanical"] == 1
        assert problems["by_check_type"]["page"] == 1
        assert problems["by_check_type"]["wording"] == 2

        # Needs 19: Field breakdown
        fields = problems["by_field"]
        assert fields["headline"] == 1
        assert fields["destination_url"] == 1
        assert fields["ad_text"] == 2

        # Top rules
        rule_ids = [r["rule_id"] for r in problems["top_rules"]]
        assert "PLATFORM_HEADLINE_LIMIT" in rule_ids
        assert "PAGE_STATUS_OK" in rule_ids
        assert "UNSUBSTANTIATED_CLAIM" in rule_ids
        assert "WORDING_PUFFERY_NOTICE" in rule_ids

        # Average findings per ad (4 findings / 4 ads = 1.0)
        assert problems["avg_findings_per_ad"] == 1.0


def test_needs_36_worker_traces_and_step_cost_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Needs: 36 - Verify worker traces execution cost in USD, step counts, and step cap detection."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token = create_access_token("buyer-1", "buyer")
    with TestClient(app) as client:
        res = client.get("/batches/batch-comp-1/metrics", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()

        cost = data["cost"]
        # Total cost: 0.0012 + 0.0085 + 0.0020 + 0.0008 = 0.0125 USD
        assert cost["total_cost_usd"] == pytest.approx(0.0125, rel=1e-4)
        assert cost["avg_cost_per_ad_usd"] == pytest.approx(0.0125 / 4.0, rel=1e-4)

        # Total steps: 3 + 5 + 3 + 2 = 13 steps
        assert cost["total_steps"] == 13
        assert cost["avg_steps_per_ad"] == round(13 / 4.0, 2)
        assert cost["traces_count"] == 4

        # Step-capped ads (ad-2 had step_count = 5)
        assert cost["step_capped_ads_count"] == 1


def test_needs_43_false_alarms_and_reviewer_rejection_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Needs: 43 - Verify false alarms (dismissed findings), top dismissed rules, and reviewer actions."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token = create_access_token("buyer-1", "buyer")
    with TestClient(app) as client:
        res = client.get("/batches/batch-comp-1/metrics", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()

        fa = data["false_alarms"]
        assert fa["dismissed_count"] == 1
        assert fa["total_findings"] == 4
        assert fa["false_alarm_rate"] == 0.25  # 1 / 4
        assert fa["by_check_type"]["wording"] == 1
        assert fa["by_check_type"]["mechanical"] == 0

        # Top dismissed rules
        assert len(fa["top_dismissed_rules"]) == 1
        assert fa["top_dismissed_rules"][0]["rule_id"] == "WORDING_PUFFERY_NOTICE"
        assert fa["top_dismissed_rules"][0]["count"] == 1

        # Cleaned ads count: ad-3 only had f-word-2, which was dismissed -> cleaned
        assert fa["cleaned_ads_count"] == 1

        # Reviewer actions & rejection reasons
        actions = data["reviewer_actions"]
        assert actions["rejected_count"] == 1
        assert "Severe policy violation: unverified health cure" in actions["rejection_reasons"]
        assert actions["rejection_reasons"]["Severe policy violation: unverified health cure"] == 1

        # Reviewer time stats
        rt = data["reviewer_time"]
        assert rt["reviewed_count"] == 4
        # Times: ad-1 (90s), ad-2 (180s), ad-3 (120s), ad-4 (60s)
        assert rt["min_seconds"] == 60.0
        assert rt["max_seconds"] == 180.0
        assert rt["median_seconds"] == 105.0
        assert rt["avg_seconds"] == 112.5


def test_needs_47_platform_sends_retries_and_dead_letter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Needs: 47 - Verify platform send attempts, backoff retries, acceptance rate, and dead-letter queue."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token = create_access_token("buyer-1", "buyer")
    with TestClient(app) as client:
        res = client.get("/batches/batch-comp-1/metrics", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()

        sends = data["platform_sends"]
        # 3 sent: ad-1 accepted, ad-3 accepted, ad-4 rejected (dead letter)
        assert sends["total_sends"] == 3
        assert sends["platform_accepted"] == 2
        assert sends["platform_rejected"] == 1
        assert sends["acceptance_rate"] == round(2 / 3, 4)

        # Attempts: ad-1 (1), ad-3 (2), ad-4 (3) = 6 attempts
        assert sends["total_attempts"] == 6
        # Retried ads count: ad-3 and ad-4 had > 1 attempt -> 2
        assert sends["retried_ads_count"] == 2
        assert sends["retry_rate"] == round(2 / 3, 4)

        # Dead-letter
        assert sends["dead_letter_count"] == 1
        assert sends["dead_letter_rate"] == 0.25  # 1 / 4 ads
        assert data["dead_letter"]["has_dead_letter"] is True


def test_simulated_rejection_rates_and_protection_effectiveness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify simulated rejection rates comparing without gate vs with gate."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token = create_access_token("buyer-1", "buyer")
    with TestClient(app) as client:
        res = client.get("/batches/batch-comp-1/metrics", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()

        sim = data["simulated_rejection_rates"]
        # Without gate: total_blocked (1: ad-2) + rejected_rows (1) = 2 fails out of 5 total sheet rows (4 ads + 1 malformed)
        assert sim["without_gate_rejection_rate"] == 0.4  # 2 / 5
        # With gate: platform_rejected (1: ad-4) / total_sent (3) = 0.3333
        assert sim["with_gate_rejection_rate"] == round(1 / 3, 4)
        # Avoided: 2 - 1 = 1
        assert sim["rejections_avoided_count"] == 1
        # Effectiveness percent: (1 / 2) * 100 = 50.0%
        assert sim["protection_effectiveness_percent"] == 50.0


def test_ci_release_gate_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD §4.7 / Line 27: Verify CI release gate evaluation (false alarm ceiling <= 5%, dead letter <= 5%)."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token = create_access_token("buyer-1", "buyer")
    with TestClient(app) as client:
        res = client.get("/batches/batch-comp-1/metrics", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()

        gate = data["ci_release_gate"]
        # In batch-1, false alarm rate is 25% (> 5%) and dead letter rate is 25% (> 5%) -> failed
        assert gate["status"] == "failed"
        assert gate["passed"] is False
        assert gate["false_alarm_passed"] is False
        assert gate["dead_letter_passed"] is False

        checks = {c["name"]: c for c in gate["checks"]}
        assert "False Alarm Rate <= 5%" in checks
        assert checks["False Alarm Rate <= 5%"]["passed"] is False
        assert "Dead-Letter Rate <= 5%" in checks
        assert checks["Dead-Letter Rate <= 5%"]["passed"] is False


def test_batch_metrics_summary_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /batches/{batch_id}/metrics/summary returns concise KPI summary."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token = create_access_token("buyer-1", "buyer")
    with TestClient(app) as client:
        res = client.get("/batches/batch-comp-1/metrics/summary", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        summary = res.json()

        assert summary["batch_id"] == "batch-comp-1"
        assert summary["total_ads"] == 4
        assert summary["blocked_count"] == 1
        assert summary["blocked_rate"] == 0.25
        assert summary["false_alarm_rate"] == 0.25
        assert summary["total_cost_usd"] == pytest.approx(0.0125, rel=1e-4)
        assert summary["reviewer_avg_seconds"] == 112.5
        assert summary["dead_letter_count"] == 1
        assert summary["release_gate_status"] == "failed"
        assert summary["release_gate_passed"] is False


def test_metrics_export_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /batches/{batch_id}/metrics/export returns full exportable JSON payload."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token = create_access_token("buyer-1", "buyer")
    with TestClient(app) as client:
        res = client.get("/batches/batch-comp-1/metrics/export", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        export_data = res.json()
        assert export_data["batch_id"] == "batch-comp-1"
        assert "problems_by_type" in export_data
        assert "cost" in export_data
        assert "false_alarms" in export_data
        assert "platform_sends" in export_data


def test_global_release_gate_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /metrics/release-gate returns global CI release gate verification report."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token = create_access_token("buyer-1", "buyer")
    with TestClient(app) as client:
        res = client.get("/metrics/release-gate", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        rg = res.json()

        assert "status" in rg
        assert "passed" in rg
        assert "false_alarm_rate" in rg
        assert "false_alarm_threshold" in rg
        assert rg["false_alarm_threshold"] == 0.05
        assert "dead_letter_threshold" in rg
        assert rg["dead_letter_threshold"] == 0.05
        assert "evaluated_batches_count" in rg
        assert "checks" in rg


def test_metrics_scoping_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Needs: 7 - Unauthorized client access returns 403 Forbidden; missing batch returns 404."""
    setup_users(monkeypatch)
    db = FakeMetricsComputationDB()
    monkeypatch.setattr(metrics, "get_session", db.session)

    token_buyer_2 = create_access_token("buyer-2", "buyer")  # Assigned only to calmleaf

    with TestClient(app) as client:
        # 1. 404 on missing batch
        res_404 = client.get("/batches/nonexistent-batch/metrics", headers={"Authorization": f"Bearer {token_buyer_2}"})
        assert res_404.status_code == 404

        # 2. 403 when user is not assigned to any client in batch-comp-1 (brightleaf)
        res_403 = client.get("/batches/batch-comp-1/metrics", headers={"Authorization": f"Bearer {token_buyer_2}"})
        assert res_403.status_code == 403
        assert res_403.json()["detail"] == "forbidden"

        # 3. 403 when requesting client_id unassigned to user on global release gate
        res_rg_403 = client.get("/metrics/release-gate?client_id=brightleaf", headers={"Authorization": f"Bearer {token_buyer_2}"})
        assert res_rg_403.status_code == 403
