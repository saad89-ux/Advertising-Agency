from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from apps.api.clients import (
    WORKSPACE_META_CONNECTIONS_MIGRATION,
    resolve_client_meta_account,
    update_workspace_meta_health,
)


class AccountConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        for row in self.rows:
            return row
        return None


def test_resolves_only_the_requested_clients_ad_account():
    connection = AccountConnection((("123456",),))

    assert resolve_client_meta_account(connection, "client-a") == "123456"


def test_missing_client_ad_account_is_explicit_error():
    connection = AccountConnection((None,))

    with pytest.raises(ValueError, match="Meta ad account is not configured"):
        resolve_client_meta_account(connection, "client-a")


def test_missing_client_row_is_explicit_error():
    connection = AccountConnection(())

    with pytest.raises(ValueError, match="Meta ad account is not configured"):
        resolve_client_meta_account(connection, "unknown-client")


def test_workspace_connection_schema_has_secret_reference_but_no_raw_token():
    assert "secret_manager_ref" in WORKSPACE_META_CONNECTIONS_MIGRATION
    assert "access_token" not in WORKSPACE_META_CONNECTIONS_MIGRATION
    assert "workspace_meta_connections" in WORKSPACE_META_CONNECTIONS_MIGRATION


def test_health_update_sanitizes_error_and_validates_state():
    class RecordingConnection:
        def execute(self, query, params):
            self.query = query
            self.params = params

    connection = RecordingConnection()
    update_workspace_meta_health(
        connection,
        "client-a",
        health="DEGRADED",
        verified_scopes=["ads_read"],
        error_message="  Temporary\n Meta error  ",
    )

    assert connection.params == ("DEGRADED", ["ads_read"], "Temporary Meta error", "client-a")
    with pytest.raises(ValueError, match="Invalid Meta connection health"):
        update_workspace_meta_health(connection, "client-a", health="BROKEN")
