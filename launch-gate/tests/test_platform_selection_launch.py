"""Unit tests for Dynamic Platform Selection in Pre-Launch Modal and Platform-Scoped Tracking Pipeline.

Covers:
1. Validation of target_platforms (empty or missing raises 422).
2. Validation of notification email; phone is optional and is no longer collected.
3. Platform alias normalization (e.g. 'fb', 'meta', 'ig', 'tt' normalize to canonical platform identifiers).
4. Unsupported platform names raise 400.
5. Strict platform dispatch scoping: launching with selected platforms dispatches ONLY to those platforms.
6. Sample data isolation (include_samples=False vs include_samples=True) on all-campaigns and platform-performance.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.auth import CurrentUser, get_current_user
from apps.api.campaign_flow import (
    LaunchBatchRequest,
    get_all_campaigns,
    get_platform_performance,
    launch_batch,
)
from apps.api.main import app
from apps.worker.agent_team import RevenueAnalysisResult
from packages.rules.platform_normalization import normalize_platform


# ---------------------------------------------------------------------------
# Test Auth Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_buyer_user():
    return CurrentUser(
        user_id="usr_buyer_123",
        email="buyer@launchgate.com",
        display_name="Sarah Buyer",
        role="buyer",
        assigned_client_ids={"glow_skin", "test_client"},
    )


# ---------------------------------------------------------------------------
# 1. Platform Normalization Unit Tests
# ---------------------------------------------------------------------------

def test_platform_normalization_supported_aliases():
    """Verify supported platforms normalize correctly to canonical names."""
    assert normalize_platform("facebook").canonical_value == "facebook"
    assert normalize_platform("fb").canonical_value == "facebook"
    assert normalize_platform("meta").canonical_value == "facebook"
    assert normalize_platform("instagram").canonical_value == "instagram"
    assert normalize_platform("ig").canonical_value == "instagram"
    assert normalize_platform("tiktok").canonical_value == "tiktok"
    assert normalize_platform("tt").canonical_value == "tiktok"
    assert normalize_platform("x").canonical_value == "x"
    assert normalize_platform("twitter").canonical_value == "x"
    assert normalize_platform("snapchat").canonical_value == "snapchat"
    assert normalize_platform("snap").canonical_value == "snapchat"


def test_platform_normalization_unsupported():
    """Verify unknown or unconfigured platforms flag as uncanonical."""
    norm = normalize_platform("myspace")
    assert norm.flagged is True
    assert norm.canonical_value is None

    norm_empty = normalize_platform("")
    assert norm_empty.flagged is True
    assert norm_empty.canonical_value is None


# ---------------------------------------------------------------------------
# 2. FastAPI Endpoint Validation (HTTP Status Codes 422 & 400)
# ---------------------------------------------------------------------------

def test_launch_batch_empty_target_platforms_returns_422(mock_buyer_user):
    """Empty target_platforms must fail Pydantic validation with 422 Unprocessable Entity."""
    app.dependency_overrides[get_current_user] = lambda: mock_buyer_user
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/campaign-flow/launch-batch",
                json={
                    "client_id": "glow_skin",
                    "campaign_name": "Test Burst",
                    "employee_email": "sarah.ops@launchgate.com",
                    "employee_phone": "+15553829910",
                    "target_platforms": [],  # Empty!
                },
            )
            assert resp.status_code == 422
            errors = resp.json().get("detail", [])
            assert isinstance(errors, str) and "target_platforms" in errors
    finally:
        app.dependency_overrides.clear()


def test_launch_batch_missing_target_platforms_returns_422(mock_buyer_user):
    """Omitting target_platforms must return 422 since target_platforms is required without default."""
    app.dependency_overrides[get_current_user] = lambda: mock_buyer_user
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/campaign-flow/launch-batch",
                json={
                    "client_id": "glow_skin",
                    "campaign_name": "Test Burst",
                    "employee_email": "sarah.ops@launchgate.com",
                    "employee_phone": "+15553829910",
                    # target_platforms omitted
                },
            )
            assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_launch_batch_invalid_employee_email_returns_400(mock_buyer_user):
    """Invalid email without '@' must return 400 Bad Request."""
    app.dependency_overrides[get_current_user] = lambda: mock_buyer_user
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/campaign-flow/launch-batch",
                json={
                    "client_id": "glow_skin",
                    "campaign_name": "Test Burst",
                    "employee_email": "invalid-email-no-at-sign",
                    "employee_phone": "+15553829910",
                    "target_platforms": ["instagram"],
                },
            )
            assert resp.status_code == 400
            assert "valid employee email" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_launch_batch_phone_is_optional(mock_buyer_user):
    """Notification email is required; phone collection is no longer part of launch."""
    req = LaunchBatchRequest(client_id="glow_skin", employee_email="alerts@example.com", target_platforms=["facebook"])
    assert req.employee_phone == ""


def test_launch_batch_unsupported_platform_returns_400(mock_buyer_user):
    """Passing an unknown platform in target_platforms must raise 400."""
    app.dependency_overrides[get_current_user] = lambda: mock_buyer_user
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/campaign-flow/launch-batch",
                json={
                    "client_id": "glow_skin",
                    "campaign_name": "Test Burst",
                    "employee_email": "sarah.ops@launchgate.com",
                    "employee_phone": "+15553829910",
                    "target_platforms": ["instagram", "unsupported_xyz"],
                },
            )
            assert resp.status_code == 400
            assert "unsupported platform" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 3. Strict Platform Isolation & Dispatch Scoping Tests
# ---------------------------------------------------------------------------

class MockCursor:
    def __init__(self, fetchone_val=None, fetchall_val=None):
        self.fetchone_val = fetchone_val
        self.fetchall_val = fetchall_val or []
        self.queries: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.fetchone_val

    def fetchall(self):
        last_query = self.queries[-1][0] if self.queries else ""
        if "COUNT(*)" in last_query:
            return [("active_live", 5)]
        return self.fetchall_val

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class MockConnection:
    def __init__(self, cursor: MockCursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_launch_batch_strict_platform_isolation(mock_buyer_user):
    """Verify that non-Meta targets are rejected before dispatch."""
    req = LaunchBatchRequest(
        client_id="glow_skin",
        batch_id="batch_custom_456",
        campaign_name="Strict Isolation Test",
        employee_email="media.buyer@launchgate.com",
        employee_phone="+1 (555) 382-9910",
        target_platforms=["facebook", "tiktok"],
    )

    with patch("apps.api.campaign_flow.init_flow_tables"):
        with pytest.raises(HTTPException, match="Meta only"):
            launch_batch(req, user=mock_buyer_user)


def test_launch_batch_normalizes_aliases(mock_buyer_user, monkeypatch):
    """Verify that Meta aliases are deduplicated and normalized."""
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "false")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    from apps.api.ingest import hash_normalized_row
    canonical = {"platform": "facebook", "headline": "Glow Offer", "ad_text": "Buy now", "destination_url": "https://example.com/offer", "creative_file": ""}
    row_hash = hash_normalized_row(canonical)
    cursor = MockCursor(fetchall_val=[("ad_001", "Glow Offer", "Buy now", canonical["destination_url"], row_hash, None, "facebook")])
    def fetchone():
        query = cursor.queries[-1][0]
        if "SELECT uploaded_by" in query:
            return (mock_buyer_user.user_id,)
        if "SELECT state, content_hash" in query:
            return ("approved", row_hash)
        return None
    cursor.fetchone = fetchone
    mock_conn = MockConnection(cursor)

    req = LaunchBatchRequest(
        client_id="glow_skin",
        campaign_name="Alias Normalization Test",
        employee_email="buyer@launchgate.com",
        employee_phone="+1 (555) 382-9910",
        target_platforms=["fb", "meta", "ig"],
    )

    with patch("apps.api.campaign_flow.get_session", return_value=mock_conn), \
         patch("apps.api.campaign_flow.init_flow_tables"), \
         patch("apps.api.campaign_flow.MCPPlatformDispatcher") as MockMCPClass, \
         patch("apps.api.campaign_flow.NotificationDispatcher.dispatch_campaign_alert", return_value={"email_status": "unconfigured_credentials"}), \
         patch("apps.api.uploads._dispatch_pending_jobs"):

        mock_mcp = MockMCPClass.return_value
        mock_resp = MagicMock()
        mock_resp.success = False
        mock_resp.error = "connector_not_configured"
        mock_mcp.dispatch_paused_ad.return_value = mock_resp

        res = launch_batch(req, user=mock_buyer_user)

    # fb and meta both normalize to facebook; ig -> instagram.
    assert res["target_platforms"] == ["facebook", "instagram"]
    assert len(res["target_platforms"]) == 2
    assert [call.kwargs["platform"] for call in mock_mcp.dispatch_paused_ad.call_args_list] == ["facebook", "instagram"]


# ---------------------------------------------------------------------------
# 4. Sample vs Real Data Filtering Tests
# ---------------------------------------------------------------------------

def test_all_campaigns_sample_filtering(mock_buyer_user):
    """Verify that include_samples=False filters out sample ads in get_all_campaigns."""
    sample_row = (
        "ad_sample_1", "glow_skin", "instagram", "Sample Headline", "https://glowskin.com",
        "@glow_skin", "active_live", "Approved", 500.0, 100.0, 5.0, 1000, False, True, True
    )
    real_row = (
        "ad_real_1", "glow_skin", "facebook", "Real Headline", "https://glowskin.com",
        "@glow_skin", "active_live", "Approved", 800.0, 200.0, 4.0, 2000, False, True, False
    )

    # When include_samples=False, query contains AND a.is_sample = FALSE
    cursor_real_only = MockCursor(fetchall_val=[real_row])
    conn_real = MockConnection(cursor_real_only)

    with patch("apps.api.campaign_flow.get_session", return_value=conn_real), \
         patch("apps.api.campaign_flow.init_flow_tables"):
        camps = get_all_campaigns(include_samples=False, user=mock_buyer_user)

    assert len(camps) == 1
    assert camps[0]["ad_id"] == "ad_real_1"
    assert camps[0]["is_sample"] is False
    # Check that sample filter was applied in the SQL query
    assert any("AND a.is_sample = FALSE" in q[0] for q in cursor_real_only.queries)

    # When include_samples=True, query does not filter out sample ads
    cursor_all = MockCursor(fetchall_val=[sample_row, real_row])
    conn_all = MockConnection(cursor_all)

    with patch("apps.api.campaign_flow.get_session", return_value=conn_all), \
         patch("apps.api.campaign_flow.init_flow_tables"):
        camps_all = get_all_campaigns(include_samples=True, user=mock_buyer_user)

    assert len(camps_all) == 2
    assert any(c["is_sample"] is True for c in camps_all)
    assert not any("AND a.is_sample = FALSE" in q[0] for q in cursor_all.queries)


def test_platform_performance_sample_filtering(mock_buyer_user):
    """Verify that get_platform_performance filters out sample ads when include_samples=False."""
    cursor = MockCursor(
        fetchall_val=[("facebook", 1500.0, 500.0, 25, 5000, 200)],
    )
    conn = MockConnection(cursor)

    mock_analysis = RevenueAnalysisResult(
        ad_id="test",
        platform="facebook",
        spend=500.0,
        revenue=1500.0,
        roas=3.0,
        orders=25,
        impressions=5000,
        ctr=4.0,
        profitability_status="PROFITABLE_HEALTHY",
        recommendation="Scale budget +20%",
        ai_insights="Solid ROAS",
    )

    with patch("apps.api.campaign_flow.get_session", return_value=conn), \
         patch("apps.api.campaign_flow.init_flow_tables"), \
         patch("apps.worker.agent_team.RevenueTrackingSpecialistNode.analyze_performance", return_value=mock_analysis):
        perf = get_platform_performance(include_samples=False, user=mock_buyer_user)

    assert "platform_breakdown" in perf
    assert "summary" in perf
    # Ensure JOIN ads a ... AND a.is_sample = FALSE was present in the query
    assert any("a.is_sample = FALSE" in q[0] for q in cursor.queries)


def test_platform_performance_api_query_param(mock_buyer_user):
    """Test calling /campaign-flow/platform-performance?include_samples=false via FastAPI TestClient."""
    cursor = MockCursor(
        fetchall_val=[("tiktok", 2000.0, 400.0, 40, 8000, 320)],
    )
    conn = MockConnection(cursor)

    mock_analysis = RevenueAnalysisResult(
        ad_id="test",
        platform="tiktok",
        spend=400.0,
        revenue=2000.0,
        roas=5.0,
        orders=40,
        impressions=8000,
        ctr=4.0,
        profitability_status="SCALING_CANDIDATE",
        recommendation="Scale aggressive",
        ai_insights="Great metrics",
    )

    app.dependency_overrides[get_current_user] = lambda: mock_buyer_user
    try:
        with patch("apps.api.campaign_flow.get_session", return_value=conn), \
             patch("apps.api.campaign_flow.init_flow_tables"), \
             patch("apps.worker.agent_team.RevenueTrackingSpecialistNode.analyze_performance", return_value=mock_analysis), \
             TestClient(app) as client:

            resp = client.get("/campaign-flow/platform-performance?include_samples=false")
            assert resp.status_code == 200
            data = resp.json()
            assert "platform_breakdown" in data
            assert any(p["platform"] == "tiktok" and p["tracked"] is True for p in data["platform_breakdown"])
    finally:
        app.dependency_overrides.clear()


def test_all_campaigns_api_query_param(mock_buyer_user):
    """Test calling /campaign-flow/all-campaigns?include_samples=false via FastAPI TestClient."""
    real_row = (
        "ad_real_2", "glow_skin", "tiktok", "TikTok Video", "https://glowskin.com",
        "@glow_skin", "active_live", "Approved", 1200.0, 300.0, 4.0, 3000, False, True, False
    )
    cursor = MockCursor(fetchall_val=[real_row])
    conn = MockConnection(cursor)

    app.dependency_overrides[get_current_user] = lambda: mock_buyer_user
    try:
        with patch("apps.api.campaign_flow.get_session", return_value=conn), \
             patch("apps.api.campaign_flow.init_flow_tables"), \
             TestClient(app) as client:

            resp = client.get("/campaign-flow/all-campaigns?include_samples=false")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["ad_id"] == "ad_real_2"
            assert data[0]["is_sample"] is False
    finally:
        app.dependency_overrides.clear()
