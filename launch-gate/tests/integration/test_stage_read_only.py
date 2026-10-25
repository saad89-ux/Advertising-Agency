"""Opt-in real-account checks. These tests never perform Meta write operations."""

import pytest

from apps.api.clients import resolve_client_meta_account
from apps.api.database import get_session
from apps.api.platforms_schema import init_platform_tables
from apps.worker.meta_sync import create_sync_log, execute_workspace_sync
from tests.meta_test_config import MetaTestConfig


@pytest.mark.live_meta
@pytest.mark.skipif(not MetaTestConfig.RUN_LIVE, reason="Opt-in live Meta integration tests are disabled.")
def test_sandbox_account_sync_and_metrics_are_read_only():
    MetaTestConfig.require_read_only_live_configuration()
    init_platform_tables()

    with get_session() as connection:
        mapped_account = resolve_client_meta_account(connection, MetaTestConfig.TEST_CLIENT_ID)
        assert mapped_account.removeprefix("act_") == MetaTestConfig.TEST_AD_ACCOUNT_ID, (
            "META_SANDBOX_AD_ACCOUNT_ID must match the account mapped to META_SANDBOX_CLIENT_ID."
        )
        initial_sync_id = create_sync_log(connection, MetaTestConfig.TEST_CLIENT_ID, "INITIAL_IMPORT")
        connection.commit()

    initial_result = execute_workspace_sync(
        MetaTestConfig.TEST_CLIENT_ID,
        initial_sync_id,
        sync_type="INITIAL_IMPORT",
        lookback_days=7,
        max_attempts=2,
        max_pages=10,
    )
    assert initial_result["status"] == "COMPLETED", initial_result.get("error")
    assert initial_result["objects_synced"] >= 0
    assert initial_result["metric_snapshots"] >= 0

    with get_session() as connection:
        refresh_sync_id = create_sync_log(connection, MetaTestConfig.TEST_CLIENT_ID, "METRIC_REFRESH")
        connection.commit()

    refresh_result = execute_workspace_sync(
        MetaTestConfig.TEST_CLIENT_ID,
        refresh_sync_id,
        sync_type="METRIC_REFRESH",
        lookback_days=7,
        max_attempts=2,
        max_pages=10,
    )
    assert refresh_result["status"] == "COMPLETED", refresh_result.get("error")
    assert refresh_result["metric_snapshots"] >= 0
