"""Comprehensive Unit Tests for Live Tracking Multi-Agent Specialist Nodes in Launch Gate.

Covers:
1. RevenueTrackingSpecialistNode:
   - Profitability categorization: UNPROFITABLE_BLEEDING, SCALING_CANDIDATE, MODERATE_PERFORMING, LEARNING_PHASE.
   - LLM compact context budget and scaling guidance.
   - Fail-closed fallback to deterministic calculation when LLM is unavailable or errors.
2. IssueTrackingSpecialistNode:
   - Detection of suspended and blocked states (CRITICAL_BLOCKER).
   - Immediate dispatch of SMS & Email alerts via NotificationDispatcher to campaign launcher.
   - Prevention of false-alarm alerts on active/clean campaigns.
   - AI root cause diagnostics and proposed copy fixes.
3. Campaign Flow API integration with Revenue & Issue Specialist Nodes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apps.api.campaign_flow import get_platform_performance, get_platform_violations
from apps.api.notifications import NotificationDispatcher
from apps.worker.agent_team import (
    IssueAuditResult,
    IssueTrackingSpecialistNode,
    RevenueAnalysisResult,
    RevenueTrackingSpecialistNode,
)


@pytest.fixture(autouse=True)
def disable_real_llm_calls_in_unit_tests(monkeypatch: pytest.MonkeyPatch):
    """Ensure unit tests do not attempt real external network/database calls unless explicitly mocked."""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# 1. Tests for RevenueTrackingSpecialistNode
# ---------------------------------------------------------------------------

class TestRevenueTrackingSpecialistNode:
    def setup_method(self):
        self.node = RevenueTrackingSpecialistNode()

    def test_unprofitable_bleeding_classification(self):
        # Spend $100, Revenue $90 -> ROAS 0.9x (< 1.2x)
        metrics = {
            "spend_usd": 100.0,
            "inbound_sales_usd": 90.0,
            "inbound_orders": 2,
            "impressions": 1200,
            "clicks": 30,
        }
        res = self.node.analyze_performance("ad_bleed_01", "meta", metrics)

        assert res.profitability_status == "UNPROFITABLE_BLEEDING"
        assert res.roas == 0.9
        assert "below the 1.2x breakeven threshold" in res.recommendation
        assert "Reduce ad budget" in res.recommendation

    def test_scaling_candidate_classification(self):
        # Spend $100, Revenue $320 -> ROAS 3.2x (>= 2.5x)
        metrics = {
            "spend_usd": 100.0,
            "inbound_sales_usd": 320.0,
            "inbound_orders": 8,
            "impressions": 2500,
            "clicks": 85,
        }
        res = self.node.analyze_performance("ad_scale_01", "tiktok", metrics)

        assert res.profitability_status == "SCALING_CANDIDATE"
        assert res.roas == 3.2
        assert "high profitability" in res.recommendation
        assert "Scale campaign budget by 20-30%" in res.recommendation

    def test_moderate_performing_classification(self):
        # Spend $100, Revenue $180 -> ROAS 1.8x (between 1.2 and 2.5)
        metrics = {
            "spend": 100.0,
            "revenue": 180.0,
            "orders": 4,
            "impressions": 1500,
            "clicks": 45,
        }
        res = self.node.analyze_performance("ad_mod_01", "instagram", metrics)

        assert res.profitability_status == "MODERATE_PERFORMING"
        assert res.roas == 1.8
        assert "Maintain current ad budget" in res.recommendation

    def test_learning_phase_classification(self):
        # Impressions < 50 or spend 0
        metrics = {
            "spend": 10.0,
            "revenue": 0.0,
            "orders": 0,
            "impressions": 30,
            "clicks": 1,
        }
        res = self.node.analyze_performance("ad_new_01", "snapchat", metrics)

        assert res.profitability_status == "LEARNING_PHASE"
        assert "initial learning phase" in res.recommendation

    def test_llm_budget_guidance_when_model_available(self):
        mock_model = MagicMock()
        mock_model.generate.return_value = (
            "ROAS of 3.8x demonstrates exceptional efficiency on Meta. "
            "Increase daily budget by 25% while maintaining current ad creative targeting."
        )

        metrics = {
            "spend_usd": 200.0,
            "inbound_sales_usd": 760.0,
            "inbound_orders": 15,
            "impressions": 5000,
            "clicks": 160,
        }
        res = self.node.analyze_performance("ad_scale_llm", "facebook", metrics, model_client=mock_model)

        assert res.profitability_status == "SCALING_CANDIDATE"
        assert res.ai_insights == (
            "ROAS of 3.8x demonstrates exceptional efficiency on Meta. "
            "Increase daily budget by 25% while maintaining current ad creative targeting."
        )
        assert res.recommendation == res.ai_insights

    def test_fail_closed_when_llm_errors(self):
        mock_model = MagicMock()
        mock_model.generate.side_effect = RuntimeError("API rate limit exceeded or network down")

        metrics = {
            "spend_usd": 150.0,
            "inbound_sales_usd": 100.0,
            "inbound_orders": 2,
            "impressions": 2000,
            "clicks": 40,
        }
        res = self.node.analyze_performance("ad_err_01", "x", metrics, model_client=mock_model)

        assert res.profitability_status == "UNPROFITABLE_BLEEDING"
        assert "Reduce ad budget" in res.recommendation
        assert res.ai_insights is None


# ---------------------------------------------------------------------------
# 2. Tests for IssueTrackingSpecialistNode
# ---------------------------------------------------------------------------

class TestIssueTrackingSpecialistNode:
    def setup_method(self):
        self.node = IssueTrackingSpecialistNode()

    def test_detects_suspended_state_and_triggers_alert(self):
        with patch.object(
            NotificationDispatcher,
            "dispatch_campaign_alert",
            return_value={"sms_status": "sent", "email_status": "sent"},
        ) as mock_dispatch:

            res = self.node.audit_live_status(
                ad_id="ad_susp_01",
                client_id="aurora_cosmetics",
                platform="instagram",
                dispatch_status="suspended",
                crawler_feedback="Ad account suspended: repeated policy violations in healthcare claims.",
                contact_email="lead_buyer@agency.com",
                contact_phone="+1-800-555-0199",
            )

            assert res.is_blocked is True
            assert res.severity == "CRITICAL_BLOCKER"
            assert res.alert_dispatched is True
            assert res.notification_result == {"sms_status": "sent", "email_status": "sent"}

            mock_dispatch.assert_called_once()
            kwargs = mock_dispatch.call_args[1]
            assert kwargs["client_id"] == "aurora_cosmetics"
            assert kwargs["recipient_email"] == "lead_buyer@agency.com"
            assert kwargs["recipient_phone"] == "+1-800-555-0199"
            assert kwargs["alert_type"] == "campaign_suspension"
            assert "CRITICAL ALERT" in kwargs["subject"]
            assert kwargs["ad_id"] == "ad_susp_01"

    def test_detects_platform_rejection_and_triggers_alert(self):
        with patch.object(
            NotificationDispatcher,
            "dispatch_campaign_alert",
            return_value={"sms_status": "sent", "email_status": "sent"},
        ) as mock_dispatch:

            res = self.node.audit_live_status(
                ad_id="ad_rej_02",
                client_id="brightleaf",
                platform="tiktok",
                dispatch_status="rejected",
                crawler_feedback="Creative rejected: landing page destination URL returned HTTP 404.",
                contact_email="buyer@brightleaf.com",
                contact_phone="+1-555-987-6543",
            )

            assert res.is_blocked is True
            assert res.severity == "CRITICAL_BLOCKER"
            assert res.alert_dispatched is True

            mock_dispatch.assert_called_once()
            kwargs = mock_dispatch.call_args[1]
            assert kwargs["alert_type"] == "platform_rejection"
            assert "brightleaf" in kwargs["client_id"]

    def test_active_live_ad_never_triggers_false_alarm(self):
        with patch.object(
            NotificationDispatcher,
            "dispatch_campaign_alert",
        ) as mock_dispatch:

            res = self.node.audit_live_status(
                ad_id="ad_clean_03",
                client_id="aurora_cosmetics",
                platform="facebook",
                dispatch_status="active_live",
                crawler_feedback="Ad review completed: passed all platform checks.",
                contact_email="buyer@agency.com",
                contact_phone="+1-555-111-2222",
            )

            assert res.is_blocked is False
            assert res.severity == "CLEAN"
            assert res.alert_dispatched is False
            assert res.notification_result is None
            mock_dispatch.assert_not_called()

    def test_llm_diagnostic_with_fail_closed_fallback(self):
        mock_model = MagicMock()
        mock_model.generate.return_value = "Root cause is unverified botanical efficacy claim. Proposed fix: State that product promotes general skin radiance."

        res = self.node.audit_live_status(
            ad_id="ad_botanical_04",
            client_id="aurora",
            platform="instagram",
            dispatch_status="flagged_by_platform",
            crawler_feedback="Disallowed medical claim detected.",
            contact_email="ops@launchgate.com",
            model_client=mock_model,
        )

        assert res.is_blocked is True
        assert res.proposed_fix == "Root cause is unverified botanical efficacy claim. Proposed fix: State that product promotes general skin radiance."


# ---------------------------------------------------------------------------
# 3. Campaign Flow Integration Tests
# ---------------------------------------------------------------------------

class SmartCursor:
    def __init__(self, tracking_data=None, violation_data=None, contact_data=None):
        self.tracking_data = tracking_data or []
        self.violation_data = violation_data or []
        self.contact_data = contact_data or []
        self.last_query = ""

    def execute(self, query: str, params: Any = None):
        self.last_query = query

    def fetchall(self):
        if "FROM ad_performance_tracking" in self.last_query:
            return self.tracking_data
        if "FROM ad_live_dispatches" in self.last_query:
            if "COUNT(*)" in self.last_query:
                return [("active_live", 2)]
            return self.violation_data
        if "FROM campaign_launch_contacts" in self.last_query:
            return self.contact_data
        return []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class SmartConnection:
    def __init__(self, cursor: SmartCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class FakeUser:
    user_id = "usr_analyst"
    email = "analyst@launchgate.com"
    role = "buyer"
    assigned_client_ids = ["aurora"]


class TestCampaignFlowSpecialistsIntegration:
    def test_platform_performance_enriched_by_revenue_specialist(self):
        # Tracking data: platform, sales, spend, orders, impressions, clicks
        cursor = SmartCursor(
            tracking_data=[("instagram", 400.0, 100.0, 10, 3000, 90)]  # ROAS 4.0 -> SCALING_CANDIDATE
        )
        conn = SmartConnection(cursor)

        with patch("apps.api.campaign_flow.init_flow_tables"), \
             patch("apps.api.campaign_flow.get_session", return_value=conn):

            res = get_platform_performance(user=FakeUser())

            assert "platform_breakdown" in res
            insta_stat = next(p for p in res["platform_breakdown"] if p["platform"] == "instagram")
            assert insta_stat["tracked"] is True
            assert insta_stat["profitability_status"] == "SCALING_CANDIDATE"
            assert "Scale" in insta_stat["ai_recommendation"]

            summary = res["summary"]
            assert summary["tracked"] is True
            assert summary["profitability_status"] == "SCALING_CANDIDATE"

    def test_platform_violations_audited_by_issue_specialist_and_triggers_alert(self):
        # Violation data: dispatch_id, ad_id, client_id, platform, handle, headline, ad_text, notes, created_at, status
        fake_violation = (
            "disp_001",
            "ad_viol_99",
            "aurora",
            "tiktok",
            "@aurora_tt",
            "Cures Eczema Overnight",
            "Clinical botanical miracle cure.",
            "Policy Violation: Prohibited disease cure claims",
            None,
            "rejected",
            "batch_aurora",
        )
        fake_contact = ("batch_aurora", "buyer@agency.com", "+1-800-555-0199")

        cursor = SmartCursor(
            violation_data=[fake_violation],
            contact_data=[fake_contact],
        )
        conn = SmartConnection(cursor)

        with patch("apps.api.campaign_flow.init_flow_tables"), \
             patch("apps.api.campaign_flow.get_session", return_value=conn), \
             patch.object(
                 NotificationDispatcher,
                 "dispatch_campaign_alert",
                 return_value={"sms_status": "sent", "email_status": "sent"},
             ) as mock_dispatch:

            violations = get_platform_violations(user=FakeUser(), trigger_alert=True)

            assert len(violations) == 1
            viol = violations[0]
            assert viol["ad_id"] == "ad_viol_99"
            assert viol["severity"] == "CRITICAL_BLOCKER"
            assert viol["alert_dispatched"] is True
            assert "Prohibited disease cure claims" in viol["llm_root_cause"]
            assert "revised_text" in viol["proposed_ai_fix"]
            mock_dispatch.assert_called_once()
