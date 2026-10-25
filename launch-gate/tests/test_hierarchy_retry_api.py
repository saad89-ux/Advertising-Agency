from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from apps.api.auth import CurrentUser
from apps.api.campaign_flow import RetryHierarchyEntityRequest, retry_hierarchy_entity


class RetryCursor:
    def __init__(self):
        self.calls = []
        self.phase = 0

    def execute(self, query, params=None):
        self.calls.append((query, params))
        self.phase += 1
        return self

    def fetchone(self):
        if self.phase == 1:
            return ("run-id", "client-a", "meta-campaign-1", "CREATED")
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class RetryConnection:
    def __init__(self):
        self.retry_cursor = RetryCursor()

    def cursor(self):
        return self.retry_cursor

    def commit(self):
        return None


@contextmanager
def retry_session():
    yield RetryConnection()


def test_retry_successful_campaign_is_not_queued(monkeypatch):
    from apps.api import campaign_flow
    monkeypatch.setattr(campaign_flow, "get_session", retry_session)
    user = CurrentUser("manager-1", "manager@example.com", "Manager", "account_manager", ("client-a",))

    result = retry_hierarchy_entity(RetryHierarchyEntityRequest(batch_id="batch-1", entity_key="campaign-1", entity_type="campaign"), user)

    assert result["status"] == "not_required"
    assert result["meta_object_id"] == "meta-campaign-1"


def test_retry_rejects_unsupported_entity_type():
    user = CurrentUser("manager-1", "manager@example.com", "Manager", "account_manager", ("client-a",))

    with pytest.raises(HTTPException) as exc:
        retry_hierarchy_entity(RetryHierarchyEntityRequest(batch_id="batch-1", entity_key="ad-1", entity_type="ad"), user)
    assert exc.value.status_code == 422
