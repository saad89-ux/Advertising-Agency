from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from apps.api import campaign_flow
from apps.api.auth import CurrentUser


class ActivationCursor:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = ""
        self.fetchall_call = 0

    def execute(self, query, *_args, **_kwargs):
        self.last_query = query
        return self

    def fetchall(self):
        if "SELECT DISTINCT client_id" in self.last_query:
            return [("client-a",)]
        return self.rows

    def fetchone(self):
        if "workspace_meta_connections" in self.last_query:
            return ("123456", "HEALTHY")
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ActivationConnection:
    def __init__(self, rows):
        self.cursor_object = ActivationCursor(rows)

    def cursor(self):
        return self.cursor_object

    def execute(self, query, params=None):
        self.cursor_object.execute(query, params)
        return self.cursor_object

    def commit(self):
        return None


@contextmanager
def activation_session(rows):
    yield ActivationConnection(rows)


def manager_user():
    return CurrentUser("manager-1", "manager@example.com", "Manager", "account_manager", ("client-a",))


def activation_request(**overrides):
    values = {
        "batch_id": "batch-1",
        "hierarchy_hash": "a" * 64,
        "confirmed_budget_minor_units": 150000,
    }
    values.update(overrides)
    return campaign_flow.ActivateHierarchyRequest(**values)


def test_activation_requires_live_write_gates(monkeypatch):
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")

    with pytest.raises(HTTPException) as exc:
        campaign_flow.activate_hierarchy(activation_request(), None, manager_user())
    assert exc.value.status_code == 409


def test_activation_rejects_mismatched_confirmed_budget(monkeypatch):
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "true")
    monkeypatch.setattr(campaign_flow, "get_session", lambda: activation_session([
        ("campaign-a", "facebook", "meta-campaign-1", "a" * 64, 200000, "created_paused"),
    ]))

    with pytest.raises(HTTPException) as exc:
        campaign_flow.activate_hierarchy(activation_request(), None, manager_user())
    assert exc.value.status_code == 409
    assert "does not match planned budget" in exc.value.detail


def test_activation_rejects_campaigns_not_confirmed_paused():
    with pytest.raises(HTTPException) as exc:
        campaign_flow.validate_activation_campaigns([
            ("campaign-a", "facebook", "meta-campaign-1", "a" * 64, 200000, "active"),
        ])
    assert exc.value.status_code == 409
    assert "created_paused" in exc.value.detail


def test_activation_rejects_missing_meta_campaign_id():
    with pytest.raises(HTTPException) as exc:
        campaign_flow.validate_activation_campaigns([
            ("campaign-a", "facebook", None, "a" * 64, 200000, "created_paused"),
        ])
    assert exc.value.status_code == 409


def test_activation_requires_active_confirmation_not_just_publish_submission():
    assert campaign_flow.meta_activation_confirmed_active({"status": "ACTIVE"})
    assert not campaign_flow.meta_activation_confirmed_active({
        "status": "PUBLISHING",
        "publish_as_active_applied": True,
    })
    assert not campaign_flow.meta_activation_confirmed_active({})


def test_activation_publishes_each_reviewed_hierarchy_level_separately(monkeypatch):
    class HierarchyCursor(ActivationCursor):
        def fetchall(self):
            if "SELECT DISTINCT client_id" in self.last_query:
                return [("client-a",)]
            if "FROM meta_launch_campaigns" in self.last_query:
                return [("campaign-a", "facebook", "campaign-meta", "a" * 64, 150000, "created_paused")]
            if "FROM meta_ad_set_runs" in self.last_query:
                return [("ad-set-a", "adset-meta")]
            if "FROM meta_launch_objects" in self.last_query:
                return [("ad-meta",)]
            return []

    class HierarchyConnection(ActivationConnection):
        def __init__(self):
            self.cursor_object = HierarchyCursor([])

    calls = []

    class Dispatcher:
        def __init__(self, **_kwargs):
            pass

        def call_tool(self, platform, tool_name, arguments):
            calls.append((platform, tool_name, arguments))
            return campaign_flow.MCPResponse(True, platform, tool_name, {"status": "ACTIVE"})

    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "true")
    monkeypatch.setattr(campaign_flow, "get_session", lambda: activation_session_for(HierarchyConnection()))
    monkeypatch.setattr(campaign_flow, "MCPPlatformDispatcher", Dispatcher)
    monkeypatch.setattr(
        "apps.api.clients.resolve_client_meta_account",
        lambda _conn, _client_id: "123456",
    )

    result = campaign_flow.activate_hierarchy(activation_request(), None, manager_user())

    assert result["status"] == "active"
    assert result["activated_campaigns"] == ["campaign-a"]
    assert [call[2]["entity_type"] for call in calls] == ["campaign", "ad_set", "ad"]
    assert [call[2]["object_ids"] for call in calls] == [["campaign-meta"], ["adset-meta"], ["ad-meta"]]
    assert all(call[2]["publish_as_active"] is True for call in calls)


@contextmanager
def activation_session_for(connection):
    yield connection
