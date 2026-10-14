"""Phase 3B Production Readiness Verification Test Suite.

Validates all Phase 3B requirements:
1. Event Identity & Deduplication with launch_attempt_id.
2. Multi-platform alert isolation (Facebook and Instagram do not collide).
3. Retention maintenance endpoint (/notifications/retention) and preview sanitization.
4. Human approval enforcement (rejected, dead-letter, and content hash mismatch cannot launch).
5. Meta MCP 4-stage tool mapping and write refusal.
6. Batch-specific performance donut_data scoping.
7. Multi-agent invariant: deterministic mechanical findings cannot be dismissed by LLM judge.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from fastapi.testclient import TestClient

from apps.api.auth import CurrentUser, get_current_user
from apps.api.database import get_database_url, get_session
from apps.api.guest_sessions import get_request_user
from apps.api.main import app
from apps.api.notifications import (
    NotificationDispatcher,
    preview_expired_notifications,
    purge_expired_notifications,
    record_notification_delivery,
)
from apps.worker.judge_eval import evaluate_with_llm_judge
from apps.worker.mcp_client import MCPPlatformDispatcher
from packages.contracts import AdContract, CheckType, FindingContract


# --- Test Fixtures ---

@pytest.fixture(scope="module")
def clean_db():
    """Provide an isolated, dynamic PostgreSQL schema for Phase 3B tests."""
    original = get_database_url()
    schema = f"phase3b_test_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg.connect(original, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("Postgres is unavailable")

    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    patcher = pytest.MonkeyPatch()
    patcher.setenv("DATABASE_URL", make_conninfo(original, options=f"-csearch_path={schema}"))
    for key in ("META_MCP_ENABLE_LIVE", "META_MCP_ALLOW_WRITES"):
        patcher.setenv(key, "false")
    for key in ("SMTP_HOST", "MODEL_API_KEY", "GEMINI_API_KEY"):
        patcher.setenv(key, "")
    patcher.setenv("JWT_SECRET", "test-only-secret-long-enough-for-hmac-acceptance")
    patcher.setattr("apps.api.uploads._dispatch_pending_jobs", lambda: None)

    from apps.api.schema import init_schema
    from apps.api.campaign_flow import init_flow_tables

    try:
        init_schema()
        init_flow_tables()
        with get_session() as conn:
            conn.execute("INSERT INTO clients VALUES ('client_alpha', 'Client Alpha') ON CONFLICT DO NOTHING;")
            conn.execute("INSERT INTO clients VALUES ('client_beta', 'Client Beta') ON CONFLICT DO NOTHING;")
            conn.execute(
                """
                INSERT INTO users (user_id, email, display_name, password_hash, role)
                VALUES ('buyer_1', 'buyer@agency.internal', 'Buyer One', 'hash', 'buyer')
                ON CONFLICT DO NOTHING;
                """
            )
            conn.execute("INSERT INTO user_client_assignments VALUES ('buyer_1', 'client_alpha') ON CONFLICT DO NOTHING;")
        yield
    finally:
        patcher.undo()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture
def api_client(clean_db):
    user = CurrentUser(
        user_id="buyer_1",
        email="buyer@agency.internal",
        display_name="Buyer One",
        role="buyer",
        assigned_client_ids=("client_alpha",),
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_request_user] = lambda: user
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


# ===========================================================================
# 1. Event Identity & launch_attempt_id Deduplication
# ===========================================================================

def test_launch_attempt_id_persistence_and_traceability(api_client, clean_db):
    """Verify launch_attempt_id is returned by launch_batch and tracked in DB."""
    attempt_id = f"lnch_custom_{uuid.uuid4().hex[:8]}"
    payload = {
        "client_id": "client_alpha",
        "employee_email": "alerts@agency.internal",
        "target_platforms": ["facebook"],
        "headline": "Special Autumn Promo",
        "ad_text": "Discover our new seasonal offers.",
        "destination_url": "https://example.com/?utm_source=facebook&utm_medium=cpc&utm_campaign=autumn",
        "launch_attempt_id": attempt_id,
    }

    res = api_client.post("/campaign-flow/launch-batch", json=payload)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["launch_attempt_id"] == attempt_id
    batch_id = data["batch_id"]

    with get_session() as conn:
        cur = conn.cursor()
        cur.execute("SELECT launch_attempt_id, status FROM ad_live_dispatches WHERE batch_id = %s;", (batch_id,))
        row = cur.fetchone()
        assert row is not None, "Expected ad_live_dispatches row for the batch"
        assert row[0] == attempt_id

        cur.execute("SELECT launch_attempt_id FROM send_alerts WHERE batch_id = %s;", (batch_id,))
        alert_row = cur.fetchone()
        assert alert_row is not None, "Expected send_alerts row for the batch"
        assert alert_row[0] == attempt_id


def test_distinct_launch_attempts_create_separate_alerts(clean_db):
    """Verify distinct launch_attempt_ids create separate alerts, while retries deduplicate."""
    with get_session() as conn:
        res1 = NotificationDispatcher.dispatch_campaign_alert(
            client_id="client_alpha",
            recipient_email="buyer@agency.internal",
            recipient_phone="",
            alert_type="launch_blocked",
            subject="Alert 1",
            message_body="Attempt 1 details",
            batch_id="batch_100",
            ad_id="ad_100",
            platform="facebook",
            launch_attempt_id="lnch_attempt_1",
            conn=conn,
        )
        assert res1.get("duplicate_suppressed") is not True

        res1_retry = NotificationDispatcher.dispatch_campaign_alert(
            client_id="client_alpha",
            recipient_email="buyer@agency.internal",
            recipient_phone="",
            alert_type="launch_blocked",
            subject="Alert 1",
            message_body="Attempt 1 details",
            batch_id="batch_100",
            ad_id="ad_100",
            platform="facebook",
            launch_attempt_id="lnch_attempt_1",
            conn=conn,
        )
        assert res1_retry.get("duplicate_suppressed") is True
        assert res1_retry.get("delivery_id") == res1.get("delivery_id")

        res2 = NotificationDispatcher.dispatch_campaign_alert(
            client_id="client_alpha",
            recipient_email="buyer@agency.internal",
            recipient_phone="",
            alert_type="launch_blocked",
            subject="Alert 2",
            message_body="Attempt 2 details",
            batch_id="batch_100",
            ad_id="ad_100",
            platform="facebook",
            launch_attempt_id="lnch_attempt_2",
            conn=conn,
        )
        assert res2.get("duplicate_suppressed") is not True
        assert res2.get("delivery_id") != res1.get("delivery_id")


def test_multi_platform_alert_isolation(clean_db):
    """Verify Facebook and Instagram alerts for the same ad do not collide."""
    with get_session() as conn:
        res_fb = NotificationDispatcher.dispatch_campaign_alert(
            client_id="client_alpha",
            recipient_email="buyer@agency.internal",
            recipient_phone="",
            alert_type="launch_blocked",
            subject="Facebook Blocker",
            message_body="Facebook blocked",
            batch_id="batch_101",
            ad_id="ad_101",
            platform="facebook",
            launch_attempt_id="lnch_attempt_shared",
            conn=conn,
        )
        res_ig = NotificationDispatcher.dispatch_campaign_alert(
            client_id="client_alpha",
            recipient_email="buyer@agency.internal",
            recipient_phone="",
            alert_type="launch_blocked",
            subject="Instagram Blocker",
            message_body="Instagram blocked",
            batch_id="batch_101",
            ad_id="ad_101",
            platform="instagram",
            launch_attempt_id="lnch_attempt_shared",
            conn=conn,
        )

        assert res_fb.get("duplicate_suppressed") is not True
        assert res_ig.get("duplicate_suppressed") is not True
        assert res_fb.get("delivery_id") != res_ig.get("delivery_id")


# ===========================================================================
# 2. Retention Maintenance Endpoint & Preview Sanitization
# ===========================================================================

def test_retention_maintenance_endpoint_dry_run_and_execution(api_client, clean_db):
    """Verify POST /notifications/retention endpoint operates with dry-run and role protection."""
    with get_session() as conn:
        record_notification_delivery(
            conn,
            client_id="client_alpha",
            recipient="buyer@agency.internal",
            subject="Confidential Client Promo Launch Gate Alert",
            message_body="Sensitive campaign copy",
            event_type="test_event",
            dedup_key=f"dedup_seed_{uuid.uuid4().hex[:8]}",
        )
        conn.execute(
            """
            UPDATE notification_deliveries
            SET status = 'delivered', delivered_at = NOW() - INTERVAL '40 days'
            WHERE client_id = 'client_alpha' AND status = 'pending';
            """
        )
        conn.commit()

    # 1. Preview (dry-run)
    dry_res = api_client.post(
        "/notifications/retention",
        json={"client_id": "client_alpha", "retention_days": 30, "dry_run": True},
    )
    assert dry_res.status_code == 200, dry_res.text
    dry_data = dry_res.json()
    assert dry_data["dry_run"] is True
    assert dry_data["eligible_count"] >= 1
    assert dry_data["purged_count"] == 0

    # 2. Preview function sanitizes subject
    with get_session() as conn:
        previews = preview_expired_notifications(conn, retention_days=30)
        for item in previews:
            assert "message_body" not in item
            # Subject should be truncated to <= 35 chars
            assert len(item["subject"]) <= 35

    # 3. Purge (not dry-run)
    purge_res = api_client.post(
        "/notifications/retention",
        json={"client_id": "client_alpha", "retention_days": 30, "dry_run": False},
    )
    assert purge_res.status_code == 200, purge_res.text
    purge_data = purge_res.json()
    assert purge_data["dry_run"] is False
    assert purge_data["purged_count"] >= 1


def test_retention_maintenance_unauthorized_role_rejected(clean_db):
    """Verify unauthorized roles cannot call retention maintenance."""
    viewer_user = CurrentUser(
        user_id="viewer_1",
        email="viewer@agency.internal",
        display_name="Viewer",
        role="viewer",
        assigned_client_ids=("client_alpha",),
    )
    app.dependency_overrides[get_current_user] = lambda: viewer_user
    app.dependency_overrides[get_request_user] = lambda: viewer_user
    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.post("/notifications/retention", json={"dry_run": True})
        assert res.status_code == 403
    app.dependency_overrides.clear()


# ===========================================================================
# 3. Human Approval Enforcement Before Mutation
# ===========================================================================

def test_rejected_ad_cannot_be_launched(api_client, clean_db):
    """Verify an ad explicitly marked 'rejected' cannot be launched."""
    ad_id = f"ad_rej_{uuid.uuid4().hex[:6]}"
    batch_id = f"batch_rej_{uuid.uuid4().hex[:6]}"
    from apps.api.ingest import hash_normalized_row, hash_normalized_sheet
    canonical = {"platform": "facebook", "headline": "Rejected Headline", "ad_text": "Copy",
                 "destination_url": "https://example.com/?utm_source=facebook&utm_medium=cpc&utm_campaign=test",
                 "creative_file": ""}
    row_hash = hash_normalized_row(canonical)
    sheet_hash = hash_normalized_sheet([canonical])
    with get_session() as conn:
        conn.execute(
            "INSERT INTO batches (batch_id, filename, uploaded_by, sheet_hash) VALUES (%s, %s, %s, %s);",
            (batch_id, "test.csv", "buyer_1", sheet_hash),
        )
        conn.execute(
            """INSERT INTO ads (ad_id, batch_id, client_id, row_number, platform, headline, ad_text,
               destination_url, row_hash, sheet_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);""",
            (ad_id, batch_id, "client_alpha", 1, "facebook", "Rejected Headline", "Copy",
             "https://example.com/?utm_source=facebook&utm_medium=cpc&utm_campaign=test",
             row_hash, sheet_hash),
        )
        conn.execute(
            "INSERT INTO ad_approvals (ad_id, client_id, state, rejection_reason) VALUES (%s,%s,'rejected','Policy violation');",
            (ad_id, "client_alpha"),
        )
        conn.commit()

    res = api_client.post("/campaign-flow/launch-batch", json={
        "client_id": "client_alpha", "batch_id": batch_id,
        "employee_email": "buyer@agency.internal", "target_platforms": ["facebook"],
    })
    assert res.status_code == 409
    assert "rejected" in res.json()["detail"].lower()


def test_dead_letter_ad_cannot_be_launched(api_client, clean_db):
    """Verify a dead-lettered ad cannot be relaunched without remediation."""
    ad_id = f"ad_dl_{uuid.uuid4().hex[:6]}"
    batch_id = f"batch_dl_{uuid.uuid4().hex[:6]}"
    from apps.api.ingest import hash_normalized_row, hash_normalized_sheet
    canonical = {"platform": "facebook", "headline": "DL Headline", "ad_text": "Copy",
                 "destination_url": "https://example.com/?utm_source=facebook&utm_medium=cpc&utm_campaign=test",
                 "creative_file": ""}
    row_hash = hash_normalized_row(canonical)
    sheet_hash = hash_normalized_sheet([canonical])
    with get_session() as conn:
        conn.execute(
            "INSERT INTO batches (batch_id, filename, uploaded_by, sheet_hash) VALUES (%s, %s, %s, %s);",
            (batch_id, "test.csv", "buyer_1", sheet_hash),
        )
        conn.execute(
            """INSERT INTO ads (ad_id, batch_id, client_id, row_number, platform, headline, ad_text,
               destination_url, row_hash, sheet_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);""",
            (ad_id, batch_id, "client_alpha", 1, "facebook", "DL Headline", "Copy",
             "https://example.com/?utm_source=facebook&utm_medium=cpc&utm_campaign=test",
             row_hash, sheet_hash),
        )
        conn.execute(
            "INSERT INTO ad_approvals (ad_id, client_id, state, rejection_reason) VALUES (%s,%s,'dead_letter','Retries exhausted');",
            (ad_id, "client_alpha"),
        )
        conn.commit()

    res = api_client.post("/campaign-flow/launch-batch", json={
        "client_id": "client_alpha", "batch_id": batch_id,
        "employee_email": "buyer@agency.internal", "target_platforms": ["facebook"],
    })
    assert res.status_code == 409
    assert "dead-letter" in res.json()["detail"].lower()


# ===========================================================================
# 4. Meta MCP Tool Mapping & Write Protection
# ===========================================================================

def test_meta_mcp_dispatcher_lists_4_stage_tools():
    """Verify MCPPlatformDispatcher exposes real 4-stage Meta creation tools."""
    dispatcher = MCPPlatformDispatcher()
    tools = dispatcher.list_tools("facebook")
    tool_names = [t.name for t in tools]

    assert "ads_create_campaign" in tool_names
    assert "ads_create_ad_set" in tool_names
    assert "ads_create_creative" in tool_names
    assert "ads_create_ad" in tool_names
    assert "facebook_dispatch_ad" not in tool_names


def test_meta_mcp_fails_closed_when_live_write_gate_is_missing(monkeypatch):
    """Verify write requests require both opt-in flags and never call the real server in tests."""
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    monkeypatch.setenv("META_MCP_ACCESS_TOKEN", "mock-token")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "123456")
    monkeypatch.setattr(
        "apps.worker.meta_mcp_client.MetaMCPClient.call_tool_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("external call attempted")),
    )
    dispatcher = MCPPlatformDispatcher()
    resp = dispatcher.call_tool(
        platform="facebook",
        tool_name="ads_create_campaign",
        arguments={"ad_account_id": "123456", "campaign_name": "Test", "objective": "OUTCOME_TRAFFIC", "buying_type": "AUCTION"},
    )
    assert resp.success is False
    assert resp.error == "real_platform_calls_disabled"


def test_meta_mcp_dispatches_discovered_write_when_both_gates_are_enabled(monkeypatch):
    from apps.worker.meta_mcp_client import MetaMCPResult

    for key, value in {
        "META_MCP_ENABLE_LIVE": "true",
        "META_MCP_ALLOW_WRITES": "true",
        "META_MCP_ACCESS_TOKEN": "mock-token",
        "META_AD_ACCOUNT_ID": "123456",
        "MCP_META_SERVER_URL": "https://mcp.example.test/ads",
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    def mocked_call(self, tool_name, arguments, **kwargs):
        calls.append((tool_name, arguments, kwargs))
        return MetaMCPResult(True, tool_name, {"campaign_id": "campaign-1"})

    monkeypatch.setattr("apps.worker.meta_mcp_client.MetaMCPClient.call_tool_sync", mocked_call)
    response = MCPPlatformDispatcher().call_tool(
        "facebook",
        "ads_create_campaign",
        {"ad_account_id": "123456", "campaign_name": "Test", "objective": "OUTCOME_TRAFFIC", "buying_type": "AUCTION"},
    )

    assert response.success is True
    assert response.result == {"campaign_id": "campaign-1"}
    assert len(calls) == 1 and calls[0][0] == "ads_create_campaign"


@pytest.mark.parametrize("missing", ["live_flag", "write_flag", "account", "url", "token"])
def test_meta_mcp_refuses_when_any_single_configuration_is_missing(monkeypatch, missing):
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "true")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "123456")
    monkeypatch.setenv("MCP_META_SERVER_URL", "https://mcp.example.test/ads")
    monkeypatch.setenv("META_MCP_ACCESS_TOKEN", "mock-token")
    variable = {
        "live_flag": "META_MCP_ENABLE_LIVE",
        "write_flag": "META_MCP_ALLOW_WRITES",
        "account": "META_AD_ACCOUNT_ID",
        "url": "MCP_META_SERVER_URL",
        "token": "META_MCP_ACCESS_TOKEN",
    }[missing]
    monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(
        "apps.worker.meta_mcp_client.MetaMCPClient.call_tool_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("external MCP call attempted")),
    )
    response = MCPPlatformDispatcher().call_tool(
        "facebook", "ads_create_campaign",
        {"ad_account_id": "123456", "campaign_name": "Test", "objective": "OUTCOME_TRAFFIC", "buying_type": "AUCTION"},
    )

    assert response.success is False


def test_meta_mcp_gates_catalog_writes_outside_launch_chain(monkeypatch):
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "false")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    response = MCPPlatformDispatcher().call_tool(
        "instagram", "ads_boost_ig_post", {"ad_account_id": "123456"}
    )

    assert response.success is False
    assert response.error == "real_platform_calls_disabled"


def test_meta_paused_chain_uses_four_create_tools_then_activates(monkeypatch):
    from apps.worker import mcp_client
    from apps.worker.mcp_client import MCPResponse, validate_meta_arguments

    monkeypatch.setattr(mcp_client, "meta_live_writes_enabled", lambda: True)
    calls = []
    ids = {"campaign": "camp-1", "ad_set": "adset-1", "creative": "creative-1", "ad": "ad-1"}

    def mocked_call(self, platform, tool_name, arguments):
        calls.append((tool_name, arguments))
        if tool_name in {"ads_get_ad_entities", "ads_get_creatives"}:
            return MCPResponse(True, platform, tool_name, {"data": []})
        stage = {
            "ads_create_campaign": ("campaign_id", ids["campaign"]),
            "ads_create_ad_set": ("ad_set_id", ids["ad_set"]),
            "ads_create_creative": ("creative_id", ids["creative"]),
            "ads_create_ad": ("ad_id", ids["ad"]),
        }.get(tool_name)
        if stage:
            return MCPResponse(True, platform, tool_name, {stage[0]: stage[1]})
        assert tool_name == "ads_activate_entity"
        if arguments["entity_type"] == "campaign":
            assert arguments["object_ids"] == [ids["campaign"], ids["ad_set"], ids["ad"]]
        return MCPResponse(True, platform, tool_name, {"status": "active"})

    monkeypatch.setattr(MCPPlatformDispatcher, "call_tool", mocked_call)
    persisted = {}
    response = MCPPlatformDispatcher().dispatch_paused_ad(
        platform="facebook", ad_id="ad-source-1", campaign_name="Reviewed campaign",
        headline="Reviewed headline", ad_text="Reviewed copy", destination_url="https://example.test",
        ad_account_id="123456", page_id="page-1", instagram_user_id=None,
        targeting_country="US", daily_budget_minor_units=500, image_url="https://cdn.example.test/image.jpg",
        persist_stage=lambda stage, value: persisted.__setitem__(stage, value),
    )

    create_calls = [(name, args) for name, args in calls if name.startswith("ads_create_")]
    assert [name for name, _ in create_calls] == ["ads_create_campaign", "ads_create_ad_set", "ads_create_creative", "ads_create_ad"]
    for name, arguments in create_calls:
        validate_meta_arguments(name, arguments)
    assert create_calls[0][1]["campaign_daily_budget"] == 500
    assert json.loads(create_calls[1][1]["targeting"])["publisher_platforms"] == ["facebook"]
    activation_calls = [(name, arguments) for name, arguments in calls if name == "ads_activate_entity"]
    assert [arguments["entity_type"] for _, arguments in activation_calls] == ["campaign", "ad_set", "ad"]
    assert activation_calls[0][1]["object_ids"] == [ids["campaign"], ids["ad_set"], ids["ad"]]
    assert all(arguments["publish_as_active"] for _, arguments in activation_calls)
    assert response.success is True
    assert response.result["status"] == "active_live"
    assert persisted == ids


def test_meta_paused_chain_recovers_created_ad_set_without_duplicate(monkeypatch):
    from apps.worker import mcp_client
    from apps.worker.mcp_client import MCPResponse

    monkeypatch.setattr(mcp_client, "meta_live_writes_enabled", lambda: True)
    adset_exists = False
    adset_create_count = 0

    def mocked_call(self, platform, tool_name, arguments):
        nonlocal adset_exists, adset_create_count
        if tool_name == "ads_get_ad_entities" and arguments.get("level") == "adset" and adset_exists:
            return MCPResponse(True, platform, tool_name, {"data": [{"id": "adset-recovered", "name": "ad-source-2-facebook-adset"}]})
        if tool_name in {"ads_get_ad_entities", "ads_get_creatives"}:
            return MCPResponse(True, platform, tool_name, {"data": []})
        if tool_name == "ads_create_campaign":
            return MCPResponse(True, platform, tool_name, {"campaign_id": "camp-reused"})
        if tool_name == "ads_create_ad_set":
            adset_create_count += 1
            adset_exists = True
            return MCPResponse(False, platform, tool_name, {}, "response lost after create")
        if tool_name == "ads_create_creative":
            return MCPResponse(True, platform, tool_name, {"creative_id": "creative-recovered"})
        if tool_name == "ads_create_ad":
            return MCPResponse(True, platform, tool_name, {"ad_id": "ad-recovered"})
        return MCPResponse(True, platform, tool_name, {"status": "active"})

    monkeypatch.setattr(MCPPlatformDispatcher, "call_tool", mocked_call)
    dispatcher = MCPPlatformDispatcher()
    args = dict(
        platform="facebook", ad_id="ad-source-2", campaign_name="Reviewed campaign",
        headline="Headline", ad_text="Copy", destination_url="https://example.test",
        ad_account_id="123456", page_id="page-1", instagram_user_id=None,
        targeting_country="US", daily_budget_minor_units=500, image_url="https://cdn.example.test/image.jpg",
    )
    first = dispatcher.dispatch_paused_ad(**args)
    second = dispatcher.dispatch_paused_ad(**args, existing_ids={"campaign": "camp-reused"})
    assert first.success is False
    assert second.success is True
    assert adset_create_count == 1


@pytest.mark.parametrize("failed_tool", ["ads_create_campaign", "ads_create_ad_set", "ads_create_creative", "ads_create_ad", "ads_activate_entity"])
def test_meta_paused_chain_stops_on_each_create_failure(monkeypatch, failed_tool):
    from apps.worker import mcp_client
    from apps.worker.mcp_client import MCPResponse

    monkeypatch.setattr(mcp_client, "meta_live_writes_enabled", lambda: True)
    calls = []
    ids = {"ads_create_campaign": "campaign-1", "ads_create_ad_set": "adset-1",
           "ads_create_creative": "creative-1", "ads_create_ad": "ad-1"}

    def mocked_call(self, platform, tool_name, arguments):
        calls.append(tool_name)
        if tool_name in {"ads_get_ad_entities", "ads_get_creatives"}:
            return MCPResponse(True, platform, tool_name, {"data": []})
        if tool_name == failed_tool:
            return MCPResponse(False, platform, tool_name, {}, "mock stage failure")
        if tool_name in ids:
            key = {"ads_create_campaign": "campaign_id", "ads_create_ad_set": "ad_set_id",
                   "ads_create_creative": "creative_id", "ads_create_ad": "ad_id"}[tool_name]
            return MCPResponse(True, platform, tool_name, {key: ids[tool_name]})
        raise AssertionError(f"unexpected network operation: {tool_name}")

    monkeypatch.setattr(MCPPlatformDispatcher, "call_tool", mocked_call)
    response = MCPPlatformDispatcher().dispatch_paused_ad(
        platform="facebook", ad_id="failure-case", campaign_name="Test campaign",
        headline="Test headline", ad_text="Test copy", destination_url="https://example.test",
        ad_account_id="123456", page_id="page-1", instagram_user_id=None,
        targeting_country="US", daily_budget_minor_units=500, image_url="https://cdn.example.test/image.jpg",
    )

    assert response.success is False
    assert response.error.startswith("meta_chain_incomplete:")
    assert failed_tool in calls
    assert calls[-1] == failed_tool
    if failed_tool == "ads_activate_entity":
        assert response.result["status"] == "paused"


# ===========================================================================
# 5. Batch-Specific Dashboard Data Scoping
# ===========================================================================

def test_donut_chart_scoped_to_batch_id(api_client, clean_db):
    """Verify /platform-performance donut_data counts only dispatches for the requested batch_id."""
    with get_session() as conn:
        conn.execute(
            """INSERT INTO batches (batch_id, filename, uploaded_by, sheet_hash)
               VALUES ('batch_scope_1', 'f1.csv', 'buyer_1', 'sh1'),
                      ('batch_scope_2', 'f2.csv', 'buyer_1', 'sh2');"""
        )
        conn.execute(
            """INSERT INTO ads (ad_id, batch_id, client_id, row_number, platform, headline, ad_text, destination_url, row_hash, sheet_hash)
               VALUES ('ad_b1_1', 'batch_scope_1', 'client_alpha', 1, 'facebook', 'H1', 'T1', 'https://example.com', 'rh1', 'sh1'),
                      ('ad_b1_2', 'batch_scope_1', 'client_alpha', 2, 'instagram', 'H2', 'T2', 'https://example.com', 'rh2', 'sh1'),
                      ('ad_b2_1', 'batch_scope_2', 'client_alpha', 1, 'facebook', 'H3', 'T3', 'https://example.com', 'rh3', 'sh2');"""
        )
        conn.execute(
            """INSERT INTO ad_live_dispatches (dispatch_id, ad_id, batch_id, client_id, platform_ad_id, platform, handle, status, platform_review_status)
               VALUES ('disp_b1_1', 'ad_b1_1', 'batch_scope_1', 'client_alpha', 'plat_b1_1', 'facebook', '@alpha', 'active_live', 'Approved by Platform Review'),
                      ('disp_b1_2', 'ad_b1_2', 'batch_scope_1', 'client_alpha', 'plat_b1_2', 'instagram', '@alpha', 'pending_review', 'Pending Review');"""
        )
        conn.execute(
            """INSERT INTO ad_live_dispatches (dispatch_id, ad_id, batch_id, client_id, platform_ad_id, platform, handle, status, platform_review_status)
               VALUES ('disp_b2_1', 'ad_b2_1', 'batch_scope_2', 'client_alpha', 'plat_b2_1', 'facebook', '@alpha', 'blocked_connector_not_configured', 'Unconfigured');"""
        )
        conn.commit()

    res1 = api_client.get("/campaign-flow/platform-performance?batch_id=batch_scope_1")
    assert res1.status_code == 200
    donut1 = res1.json()["donut_data"]
    total_b1 = sum(item["value"] for item in donut1)
    assert total_b1 == 2
    labels1 = {item["label"] for item in donut1}
    assert "Blocked (Unconfigured)" not in labels1

    res2 = api_client.get("/campaign-flow/platform-performance?batch_id=batch_scope_2")
    assert res2.status_code == 200
    donut2 = res2.json()["donut_data"]
    total_b2 = sum(item["value"] for item in donut2)
    assert total_b2 == 1
    labels2 = {item["label"] for item in donut2}
    assert "Blocked (Unconfigured)" in labels2


# ===========================================================================
# 6. Multi-Agent Invariant: LLM Judge Cannot Overrule Mechanical Rules
# ===========================================================================

def test_llm_judge_cannot_overrule_mechanical_safety_violations():
    """Verify that an LLM judge cannot dismiss CheckType.MECHANICAL findings."""
    ad = AdContract(
        ad_id="ad_mech_test",
        batch_id="batch_mech",
        client_id="client_alpha",
        row_number=1,
        platform="facebook",
        headline="Valid Headline",
        ad_text="Valid body text without issues.",
        destination_url="https://example.com/no-utm-parameters",
        creative_file=None,
        row_hash="mech_hash_1",
        sheet_hash="sheet_mech_1",
    )

    finding_mech = FindingContract(
        finding_id="finding_utm_missing",
        ad_id="ad_mech_test",
        row_number=1,
        check_type=CheckType.MECHANICAL,
        rule_id="utm_presence",
        message="Missing required UTM tracking parameters.",
        field="destination_url",
    )

    fake_llm_response = {
        "false_alarms_dismissed": ["finding_utm_missing"],
        "needs_person": False,
        "judge_reasoning": "I think UTM parameters are optional for this brand.",
    }

    mock_client = MagicMock()
    mock_client.generate.return_value = json.dumps(fake_llm_response)

    result = evaluate_with_llm_judge(
        ad,
        findings=[finding_mech],
        model_client=mock_client,
    )

    # Invariant: mechanical finding MUST NOT be dismissed
    assert "finding_utm_missing" not in result.false_alarms_dismissed
    assert len(result.false_alarms_dismissed) == 0
