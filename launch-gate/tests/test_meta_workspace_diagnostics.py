from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.api.auth import CurrentUser
from apps.api.diagnostics import diagnose_meta_client


def authorized_user():
    return CurrentUser("manager-1", "manager@example.com", "Manager", "account_manager", ("client-a",))


def test_diagnostic_rejects_unassigned_client():
    result = diagnose_meta_client("client-b", authorized_user())
    assert result["status"] == "FORBIDDEN"


class MappingConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return ("123456",)


@contextmanager
def mapping_session():
    yield MappingConnection()


@pytest.mark.parametrize(
    "accounts_result,expected_message",
    [
        (SimpleNamespace(success=True, result={"ad_accounts": []}), "No ad accounts are accessible"),
        (SimpleNamespace(success=True, result={"ad_accounts": [{"ad_account_id": "999999", "ad_account_name": "Other"}]}), "was not returned"),
    ],
)
def test_diagnostic_classifies_account_visibility(monkeypatch, accounts_result, expected_message):
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "agency-token")
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    with patch("apps.api.diagnostics.get_session", mapping_session), patch(
        "apps.worker.mcp_client.MCPPlatformDispatcher.call_tool", return_value=accounts_result
    ):
        result = diagnose_meta_client("client-a", authorized_user())

    assert result["status"] == "UNAUTHORIZED"
    assert expected_message in result["message"]


def test_diagnostic_success_probe(monkeypatch):
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "agency-token")
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    account_response = SimpleNamespace(success=True, result={"ad_accounts": [{
        "ad_account_id": "123456", "ad_account_name": "Client Account", "is_queryable": True,
    }]})
    campaign_response = SimpleNamespace(success=True, result={"data": [{"id": "campaign-1"}]})

    with patch("apps.api.diagnostics.get_session", mapping_session), patch(
        "apps.worker.mcp_client.MCPPlatformDispatcher.call_tool", side_effect=[account_response, campaign_response]
    ):
        result = diagnose_meta_client("client-a", authorized_user())

    assert result["status"] == "HEALTHY"
    assert result["accessible_campaigns_found"] == 1
    assert "agency-token" not in str(result)
    assert "secret_manager_ref" not in result


def test_diagnostic_pages_account_listing_until_workspace_account_is_found(monkeypatch):
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "agency-token")
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    account_pages = [
        SimpleNamespace(success=True, result={"ad_accounts": [{"ad_account_id": "999999"}], "next_cursor": "page-2"}),
        SimpleNamespace(success=True, result={"ad_accounts": [{
            "ad_account_id": "123456", "ad_account_name": "Client Account", "is_queryable": True,
        }]}),
        SimpleNamespace(success=True, result={"data": []}),
    ]
    calls = []

    def fake_call(_self, _platform, _tool_name, arguments):
        calls.append(arguments)
        return account_pages.pop(0)

    with patch("apps.api.diagnostics.get_session", mapping_session), patch(
        "apps.worker.mcp_client.MCPPlatformDispatcher.call_tool", new=fake_call
    ):
        result = diagnose_meta_client("client-a", authorized_user())

    assert result["status"] == "HEALTHY"
    assert calls[1]["cursor"] == "page-2"
    assert calls[2]["ad_account_id"] == "123456"


def test_diagnostic_does_not_call_meta_when_live_reads_disabled(monkeypatch):
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "agency-token")
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "false")
    with patch("apps.api.diagnostics.get_session", mapping_session), patch(
        "apps.worker.mcp_client.MCPPlatformDispatcher.call_tool"
    ) as call_tool:
        result = diagnose_meta_client("client-a", authorized_user())

    assert result["status"] == "READS_DISABLED"
    call_tool.assert_not_called()
