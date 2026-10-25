import pytest
from fastapi import HTTPException

from apps.api import campaign_flow
from apps.api.auth import CurrentUser
from apps.worker.mcp_client import MCPPlatformDispatcher, MCPResponse, validate_meta_arguments
from tests.meta_test_config import MetaTestConfig


def test_live_test_config_requires_dedicated_read_only_account(monkeypatch):
    monkeypatch.setattr(MetaTestConfig, "RUN_LIVE", True)
    monkeypatch.setattr(MetaTestConfig, "TEST_CLIENT_ID", "sandbox-client")
    monkeypatch.setattr(MetaTestConfig, "TEST_AD_ACCOUNT_ID", "123456")
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "test-token")
    MetaTestConfig.require_read_only_live_configuration()

    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "true")
    with pytest.raises(RuntimeError, match="META_MCP_ALLOW_WRITES must remain false"):
        MetaTestConfig.require_read_only_live_configuration()


def test_creation_chain_uses_only_paused_create_tools(monkeypatch):
    from apps.worker import mcp_client

    monkeypatch.setattr(mcp_client, "meta_live_writes_enabled", lambda: True)
    calls = []
    ids = {
        "ads_create_campaign": ("campaign_id", "camp-1"),
        "ads_create_ad_set": ("ad_set_id", "adset-1"),
        "ads_create_creative": ("creative_id", "creative-1"),
        "ads_create_ad": ("ad_id", "ad-1"),
    }

    def mocked_call(_self, platform, tool_name, arguments):
        calls.append((tool_name, arguments))
        if tool_name in {"ads_get_ad_entities", "ads_get_creatives"}:
            return MCPResponse(True, platform, tool_name, {"data": []})
        field, object_id = ids[tool_name]
        return MCPResponse(True, platform, tool_name, {field: object_id})

    monkeypatch.setattr(MCPPlatformDispatcher, "call_tool", mocked_call)
    result = MCPPlatformDispatcher().dispatch_paused_ad(
        platform="facebook",
        ad_id="source-ad-1",
        campaign_name="Reviewed campaign",
        headline="Reviewed headline",
        ad_text="Reviewed text",
        destination_url="https://shop.example.test/",
        ad_account_id="sandbox-account",
        page_id="page-1",
        instagram_user_id=None,
        targeting_country="US",
        daily_budget_minor_units=5000,
        image_url="https://cdn.example.test/creative.jpg",
    )

    create_calls = [(name, args) for name, args in calls if name.startswith("ads_create_")]
    assert [name for name, _ in create_calls] == list(ids)
    for name, arguments in create_calls:
        validate_meta_arguments(name, arguments)
        assert "status" not in arguments
        assert "publish_as_active" not in arguments
    assert not any(name == "ads_activate_entity" for name, _ in calls)
    assert result.success
    assert result.result["status"] == "created_paused"


def test_activation_fails_closed_when_write_opt_in_is_missing(monkeypatch):
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    user = CurrentUser("buyer-1", "buyer@example.com", "Buyer", "buyer", ("client-a",))
    request = campaign_flow.ActivateHierarchyRequest(
        batch_id="batch-local",
        hierarchy_hash="a" * 64,
        confirmed_budget_minor_units=5000,
    )

    with pytest.raises(HTTPException) as exc:
        campaign_flow.activate_hierarchy(request, None, user)
    assert exc.value.status_code == 409
    assert "META_MCP_ALLOW_WRITES=true" in exc.value.detail
