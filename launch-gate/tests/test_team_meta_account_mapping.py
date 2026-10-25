from unittest.mock import patch

import pytest
from fastapi import HTTPException

from apps.api.auth import CurrentUser
from apps.api.team import MetaAccountMappingRequest, configure_client_meta_account


def test_account_manager_can_map_assigned_client_account():
    user = CurrentUser("manager-1", "manager@example.com", "Manager", "account_manager", ("client-a",))
    class Result:
        def fetchone(self):
            return ("client-a",)

    class Connection:
        def execute(self, *_args, **_kwargs):
            return Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    connection = Connection()

    with patch("apps.api.team.get_session", return_value=connection):
        result = configure_client_meta_account(MetaAccountMappingRequest(client_id="client-a", meta_ad_account_id="act_123456"), user)

    assert result == {"client_id": "client-a", "meta_ad_account_id": "123456"}


def test_account_manager_cannot_map_unassigned_client_account():
    user = CurrentUser("manager-1", "manager@example.com", "Manager", "account_manager", ("client-a",))

    with pytest.raises(HTTPException) as exc:
        configure_client_meta_account(MetaAccountMappingRequest(client_id="client-b", meta_ad_account_id="123456"), user)
    assert exc.value.status_code == 403


def test_meta_account_mapping_rejects_malformed_id():
    user = CurrentUser("manager-1", "manager@example.com", "Manager", "account_manager", ("client-a",))

    with pytest.raises(HTTPException) as exc:
        configure_client_meta_account(MetaAccountMappingRequest(client_id="client-a", meta_ad_account_id="act_invalid"), user)
    assert exc.value.status_code == 422
