from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
from uuid import UUID, uuid4

from apps.api.diagnostics import build_meta_sync_dashboard_state
from apps.api import queue
from apps.api import outbox
from apps.worker import meta_sync
from apps.worker.mcp_client import MCPResponse


class FakeConnection:
    def __init__(self):
        self.executed = []
        self.existing_objects = []

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if "SELECT meta_ad_account_id, connection_health" in query:
            return FakeResult(("123456", "HEALTHY"))
        if "SELECT meta_ad_account_id" in query:
            return FakeResult(("123456",))
        if "SELECT meta_object_id, object_type" in query:
            return FakeResult(self.existing_objects)
        return FakeResult(None)

    def commit(self):
        pass


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row or []


@contextmanager
def fake_session(connection):
    yield connection


class FakeDispatcher:
    def __init__(self):
        self.calls = []

    def call_tool(self, platform, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name == "ads_get_ad_accounts":
            return MCPResponse(True, platform, tool_name, {"ad_accounts": [{"ad_account_id": "123456", "is_queryable": True}]})
        if tool_name == "ads_get_field_context":
            fields = []
            for name in ("effective_status", "objective", "campaign_id", "adset_id", "amount_spent", "impressions", "clicks", "omni_purchase", "offsite_conversion_fb_pixel_purchase"):
                fields.append({"field_name": name, "levels": ["campaign", "adset", "ad"]})
            return MCPResponse(True, platform, tool_name, {"fields": fields})
        level = arguments["level"]
        if level == "campaign":
            if arguments.get("cursor"):
                return MCPResponse(True, platform, tool_name, {"data": [], "pagination": {"next_cursor": None}})
            return MCPResponse(True, platform, tool_name, {"data": [{
                "id": "camp-1", "name": "Campaign 1", "objective": "OUTCOME_TRAFFIC",
                "effective_status": "ACTIVE", "amount_spent": "12.50", "impressions": "100", "clicks": "8",
                "actions": [{"action_type": "purchase", "value": "2"}],
            }], "pagination": {"next_cursor": "next-page"}})
        if level == "adset":
            return MCPResponse(True, platform, tool_name, {"data": [{
                "id": "set-1", "name": "Ad set 1", "campaign_id": "camp-1",
                "effective_status": "ACTIVE", "amount_spent": "5.00", "impressions": "50", "clicks": "4",
                "actions": [],
            }]})
        return MCPResponse(True, platform, tool_name, {"data": [{
            "id": "ad-1", "name": "Ad 1", "campaign_id": "camp-1", "adset_id": "set-1",
            "effective_status": "ACTIVE", "amount_spent": "2.00", "impressions": "20", "clicks": "2",
            "actions": [{"action_type": "omni_purchase", "value": "1"}],
        }]})


class FakeSyncRedis:
    def __init__(self):
        self.lists = {}

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def lmove(self, source, destination, where_from, where_to):
        source_items = self.lists.setdefault(source, [])
        if not source_items:
            return None
        value = source_items.pop(0 if where_from == "LEFT" else -1)
        destination_items = self.lists.setdefault(destination, [])
        destination_items.insert(0, value) if where_to == "LEFT" else destination_items.append(value)
        return value

    def lrem(self, key, count, value):
        items = self.lists.setdefault(key, [])
        if value not in items:
            return 0
        items.remove(value)
        return 1

    def eval(self, _script, _key_count, source, destination, value):
        removed = self.lrem(source, 1, value)
        if removed:
            self.lists.setdefault(destination, []).insert(0, value)
        return removed

    def close(self):
        pass


class FakeMetaSyncOutboxDatabase:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def session(self):
        pending = deepcopy(self.rows)

        class Connection:
            def execute(inner, statement, params=None):
                if statement.lstrip().startswith("SELECT sync_id, workspace_id"):
                    return FakeResult([
                        (row["sync_id"], row["workspace_id"], row["sync_type"], row["lookback_days"])
                        for row in pending if row["queued_at"] is None
                    ])
                if statement.lstrip().startswith("UPDATE meta_sync_outbox"):
                    for row in pending:
                        if row["sync_id"] in params:
                            row["queued_at"] = "synthetic timestamp"
                return FakeResult()

        try:
            yield Connection()
        except Exception:
            raise
        else:
            self.rows = pending


def test_sync_pages_imports_object_tree_and_metrics(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(meta_sync, "get_session", lambda: fake_session(connection))
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "mock-token")
    dispatcher = FakeDispatcher()
    sync_id = uuid4()

    result = meta_sync.execute_workspace_sync(
        "client-a", sync_id, dispatcher=dispatcher, sleep=lambda _: None,
        now=lambda: datetime(2026, 10, 14, tzinfo=timezone.utc),
    )

    assert result["status"] == "COMPLETED"
    assert result["objects_synced"] == 3
    assert result["metric_snapshots"] == 3
    assert any("meta_synced_objects" in query for query, _ in connection.executed)
    assert any("meta_performance_metrics" in query for query, _ in connection.executed)
    assert any("campaign_id" in arguments.get("fields", []) for _, arguments in dispatcher.calls if arguments.get("level") == "adset")
    assert any("adset_id" in arguments.get("fields", []) for _, arguments in dispatcher.calls if arguments.get("level") == "ad")
    assert any("omni_purchase" in arguments.get("fields", []) for _, arguments in dispatcher.calls if arguments.get("level") == "campaign")
    assert all("actions" not in arguments.get("fields", []) for _, arguments in dispatcher.calls)


def test_metric_refresh_queries_only_imported_objects(monkeypatch):
    connection = FakeConnection()
    connection.existing_objects = [("campaign-1", "CAMPAIGN")]
    monkeypatch.setattr(meta_sync, "get_session", lambda: fake_session(connection))
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "mock-token")
    dispatcher = FakeDispatcher()

    result = meta_sync.execute_workspace_sync(
        "client-a", uuid4(), sync_type="METRIC_REFRESH",
        dispatcher=dispatcher, sleep=lambda _: None,
    )

    assert result["status"] == "COMPLETED"
    assert result["objects_synced"] == 0
    assert result["metric_snapshots"] == 1
    metric_query = next(
        arguments
        for tool_name, arguments in dispatcher.calls
        if tool_name == "ads_get_ad_entities" and arguments.get("object_ids")
    )
    assert metric_query["object_ids"] == ["campaign-1"]


def test_redis_meta_sync_queue_recovers_unacknowledged_jobs_in_order():
    redis = FakeSyncRedis()
    first = queue.MetaSyncJob(str(uuid4()), "client-a", "INITIAL_IMPORT", 7)
    second = queue.MetaSyncJob(str(uuid4()), "client-b", "METRIC_REFRESH", 14)
    queue.enqueue_meta_sync_job(first, redis_client=redis)
    queue.enqueue_meta_sync_job(second, redis_client=redis)

    first_message = queue.claim_meta_sync_job(redis_client=redis)
    assert first_message is not None
    assert queue.recover_meta_sync_jobs(redis_client=redis) == 1
    assert queue.claim_meta_sync_job(redis_client=redis) == first_message
    assert queue.acknowledge_meta_sync_job(first_message, redis_client=redis) == 1
    assert queue.claim_meta_sync_job(redis_client=redis) is not None


def test_meta_sync_outbox_retries_redis_enqueue_failure(monkeypatch):
    job = queue.MetaSyncJob(str(uuid4()), "client-a", "INITIAL_IMPORT", 7)
    database = FakeMetaSyncOutboxDatabase([{
        "sync_id": job.sync_id,
        "workspace_id": job.workspace_id,
        "sync_type": job.sync_type,
        "lookback_days": job.lookback_days,
        "queued_at": None,
    }])
    monkeypatch.setattr(outbox, "get_session", database.session)

    def redis_down(_jobs):
        raise ConnectionError("Redis unavailable")

    try:
        outbox.dispatch_pending_meta_sync_jobs(enqueue_fn=redis_down)
    except ConnectionError:
        pass
    else:
        raise AssertionError("Redis enqueue failure should leave the outbox pending.")
    assert database.rows[0]["queued_at"] is None

    redis = FakeSyncRedis()
    assert outbox.dispatch_pending_meta_sync_jobs(
        enqueue_fn=lambda jobs: queue.enqueue_meta_sync_jobs(jobs, redis_client=redis)
    ) == 1
    assert database.rows[0]["queued_at"] == "synthetic timestamp"
    assert queue.claim_meta_sync_job(redis_client=redis) is not None


def test_meta_sync_worker_acknowledges_after_success(monkeypatch):
    redis = FakeSyncRedis()
    job = queue.MetaSyncJob(str(uuid4()), "client-a", "METRIC_REFRESH", 7)
    queue.enqueue_meta_sync_job(job, redis_client=redis)
    called = {}

    def fake_execute(workspace_id, sync_id, **kwargs):
        called.update(workspace_id=workspace_id, sync_id=sync_id, **kwargs)
        return {"sync_id": str(sync_id), "status": "COMPLETED"}

    monkeypatch.setattr(meta_sync, "execute_workspace_sync", fake_execute)

    result = meta_sync.run_meta_sync_once(redis_client=redis)

    assert result["status"] == "COMPLETED"
    assert called == {
        "workspace_id": "client-a",
        "sync_id": UUID(job.sync_id),
        "sync_type": "METRIC_REFRESH",
        "lookback_days": 7,
    }
    assert redis.lists[queue.META_SYNC_PROCESSING_QUEUE] == []


def test_meta_sync_worker_requeues_unexpected_execution_error(monkeypatch):
    redis = FakeSyncRedis()
    job = queue.MetaSyncJob(str(uuid4()), "client-a", "INITIAL_IMPORT", 7)
    queue.enqueue_meta_sync_job(job, redis_client=redis)

    def fail_before_recording(*_args, **_kwargs):
        raise RuntimeError("unexpected worker failure")

    monkeypatch.setattr(meta_sync, "execute_workspace_sync", fail_before_recording)

    assert meta_sync.run_meta_sync_once(redis_client=redis) is None
    assert redis.lists[queue.META_SYNC_QUEUE]
    assert redis.lists[queue.META_SYNC_PROCESSING_QUEUE] == []


def test_read_backoff_retries_429_with_bounded_delay():
    dispatcher = FakeDispatcher()
    responses = [
        MCPResponse(False, "facebook", "ads_get_ad_accounts", {}, "rate limit", status_code=429),
        MCPResponse(True, "facebook", "ads_get_ad_accounts", {"ad_accounts": []}),
    ]
    dispatcher.call_tool = lambda *_args: responses.pop(0)
    delays = []

    response = meta_sync.call_read_with_backoff(
        dispatcher, "ads_get_ad_accounts", {}, sleep=delays.append,
        base_delay_seconds=2, max_delay_seconds=2,
    )

    assert response.success is True
    assert delays == [2]


def test_meta_graph_rate_limit_error_retries_without_retrying_auth_errors():
    assert meta_sync._is_retryable(MCPResponse(False, "facebook", "read", {}, "Error code 17: User request limit reached"))
    assert not meta_sync._is_retryable(MCPResponse(False, "facebook", "read", {}, "Error code 401: Invalid token"))


def test_conversion_metric_prefers_aggregate_to_overlapping_action_types():
    assert meta_sync._conversions({
        "omni_purchase": "4",
        "offsite_conversion_fb_pixel_purchase": "3",
        "actions": [{"action_type": "purchase", "value": "2"}],
    }) == 4
    assert meta_sync._conversions({
        "actions": [
            {"action_type": "omni_purchase", "value": "3"},
            {"action_type": "purchase", "value": "2"},
        ],
    }) == 3


def test_sync_uses_contract_compliant_conversation_id(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(meta_sync, "get_session", lambda: fake_session(connection))
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "mock-token")
    dispatcher = FakeDispatcher()

    result = meta_sync.execute_workspace_sync(
        "client-a", uuid4(), dispatcher=dispatcher, sleep=lambda _: None,
    )

    assert result["status"] == "COMPLETED"
    conversation_ids = {
        arguments["client_conversation_id"]
        for _, arguments in dispatcher.calls
        if "client_conversation_id" in arguments
    }
    assert len(conversation_ids) == 1
    assert re.fullmatch(r"[A-Za-z0-9]{20}", conversation_ids.pop())


def test_missing_live_read_configuration_records_failure(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(meta_sync, "get_session", lambda: fake_session(connection))
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "false")
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "mock-token")

    result = meta_sync.execute_workspace_sync("client-a", uuid4(), sleep=lambda _: None)

    assert result["status"] == "FAILED"
    assert "META_MCP_ENABLE_LIVE" in result["error"]
    assert any("status='FAILED'" in query for query, _ in connection.executed)


def test_partial_structural_failure_persists_failed_log_and_count(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(meta_sync, "get_session", lambda: fake_session(connection))
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "mock-token")

    class FailingAdSetDispatcher(FakeDispatcher):
        def call_tool(self, platform, tool_name, arguments):
            if tool_name == "ads_get_ad_entities" and arguments.get("level") == "adset":
                return MCPResponse(False, platform, tool_name, {}, "temporary timeout", status_code=504)
            return super().call_tool(platform, tool_name, arguments)

    result = meta_sync.execute_workspace_sync(
        "client-a", uuid4(), dispatcher=FailingAdSetDispatcher(), sleep=lambda _: None,
        max_attempts=1,
    )
    assert result["status"] == "FAILED"
    assert "timeout" in result["error"]
    assert result["objects_synced"] == 1

    failed_update = next(
        (params for query, params in connection.executed if "status='FAILED'" in query),
        None,
    )
    assert failed_update is not None
    assert failed_update[0] == 1


def test_dashboard_state_tokens_and_staleness():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert build_meta_sync_dashboard_state("client-a", None, (0, None), now=now)["ui_state"] == "NO_DATA"
    running = (uuid4(), "INITIAL_IMPORT", "RUNNING", 0, None, now, None)
    failed = (uuid4(), "INITIAL_IMPORT", "FAILED", 0, "rate-limited", now, now)
    completed = (uuid4(), "INITIAL_IMPORT", "COMPLETED", 10, None, now, now)
    stale = (uuid4(), "INITIAL_IMPORT", "COMPLETED", 10, None, now - timedelta(days=2), now - timedelta(days=2))

    assert build_meta_sync_dashboard_state("client-a", running, (0, None), now=now)["ui_state"] == "LOADING"
    assert build_meta_sync_dashboard_state("client-a", failed, (0, None), now=now)["ui_state"] == "META_REQUEST_FAILED"
    assert build_meta_sync_dashboard_state("client-a", completed, (5, now), now=now)["ui_state"] == "SYNCED"
    assert build_meta_sync_dashboard_state("client-a", stale, (5, now - timedelta(days=2)), now=now)["ui_state"] == "STALE"
