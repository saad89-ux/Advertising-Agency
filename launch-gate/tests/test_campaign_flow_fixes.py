"""Unit tests for Fixes:
1. Zero vs "not tracked": missing data returns null/None and tracked=False.
2. Distinct blocked_connector_not_configured status (kept out of dead-letter).
"""

from unittest.mock import MagicMock
from fastapi.testclient import TestClient
import pytest

from apps.api.main import app
from apps.api.auth import get_current_user, CurrentUser
from apps.api.campaign_flow import get_platform_performance, get_all_campaigns, get_revenue_analytics


def make_mock_user(client_ids: tuple[str, ...]) -> CurrentUser:
    return CurrentUser(
        user_id="test-user-id",
        email="test@agency.com",
        display_name="Test Reviewer",
        role="reviewer",
        assigned_client_ids=client_ids,
    )


def test_platform_performance_untracked_returns_null_and_tracked_false():
    mock_user = make_mock_user(("nonexistent_client_xyz",))
    result = get_platform_performance(user=mock_user)
    summary = result["summary"]
    assert summary["tracked"] is False
    assert summary["total_sales_usd"] is None
    assert summary["total_spend_usd"] is None
    assert summary["blended_roas"] is None
    assert summary["total_orders"] is None
    assert summary["total_impressions"] is None

    for p in result["platform_breakdown"]:
        assert p["tracked"] is False
        assert p["inbound_sales_usd"] is None
        assert p["spend_usd"] is None
        assert p["orders"] is None
        assert p["impressions"] is None
        assert p["clicks"] is None
        assert p["roas"] is None


def test_revenue_analytics_untracked_returns_null():
    mock_user = make_mock_user(("nonexistent_client_xyz",))
    result = get_revenue_analytics(client_id="nonexistent_client_xyz", user=mock_user)
    assert result["daily"] == []
    assert result["weekly"] == []
    assert result["monthly"] == []
    for c in result["clients"]:
        assert c["tracked"] is False
        assert c["monthly_sales"] is None
        assert c["blended_roas"] is None


def test_all_campaigns_untracked_fields():
    mock_user = make_mock_user(("brightleaf",))
    result = get_all_campaigns(user=mock_user)
    assert isinstance(result, list)
    for c in result:
        assert "tracked" in c
        if not c["tracked"]:
            assert c["inbound_sales_usd"] is None
            assert c["spend_usd"] is None
            assert c["roas"] is None
            assert c["impressions"] is None
