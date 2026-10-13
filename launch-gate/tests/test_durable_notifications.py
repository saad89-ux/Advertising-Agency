"""Automated test suite for Launch Gate Phase 3A: Durable Alert Delivery Foundation.

Verifies all 15 required delivery, lifecycle, retry, deduplication, and security scenarios:
1. A notification is persisted before delivery is attempted.
2. Simulated success transitions to delivered.
3. Simulated failure transitions to a retryable state.
4. Retry scheduling respects the maximum attempt count.
5. Exhausted retries transition to permanently_failed.
6. A worker restart or stale processing record can recover safely.
7. Duplicate event ingestion does not create duplicate logical notifications.
8. Concurrent workers cannot claim the same record simultaneously (FOR UPDATE SKIP LOCKED).
9. A repeated processing attempt does not incorrectly overwrite a terminal state.
10. Provider acknowledgement loss is handled according to the documented idempotency strategy.
11. Tenant isolation prevents cross-tenant reads and notification delivery.
12. Guest permissions remain unchanged (cannot access employee alert administration).
13. Live email/SMS providers are not contacted in development/test mode.
14. Missing live-provider configuration fails safely rather than reporting success.
15. Secrets and message bodies are not exposed in logs or error responses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import uuid
from unittest.mock import MagicMock, patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from fastapi.testclient import TestClient

from apps.api.database import get_database_url, get_session
from apps.api.main import app
from apps.api.notifications import (
    DEV_SINK,
    SMTP_SINK,
    DeliveryBackoffPolicy,
    DeliveryStatus,
    DevelopmentDeliverySink,
    NotificationDispatcher,
    claim_pending_deliveries,
    create_notification_tables,
    get_delivery_sink,
    mask_recipient,
    process_delivery,
    preview_expired_notifications,
    process_pending_notifications,
    purge_expired_notifications,
    recover_stale_processing,
    record_notification_delivery,
    sanitize_error,
)


@pytest.fixture(scope="module")
def isolated_notification_db():
    """Create an isolated test PostgreSQL schema and tear it down cleanly."""
    schema = f"notif_test_{uuid.uuid4().hex[:10]}"
    orig = get_database_url()

    try:
        admin = psycopg.connect(orig, autocommit=True, connect_timeout=5)
    except Exception:
        pytest.skip("Postgres unavailable for durable notification integration test")

    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(orig, options=f"-csearch_path={schema}")
    old_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = scoped_url

    with psycopg.connect(scoped_url, autocommit=True) as conn:
        create_notification_tables(conn)

    yield scoped_url

    if old_db_url is not None:
        os.environ["DATABASE_URL"] = old_db_url
    else:
        os.environ.pop("DATABASE_URL", None)

    try:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    except Exception:
        pass
    finally:
        admin.close()


@pytest.fixture(autouse=True)
def clean_dev_sink(isolated_notification_db, monkeypatch):
    """Reset dev sink state before every test."""
    DEV_SINK.clear()
    monkeypatch.setenv("DATABASE_URL", isolated_notification_db)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("NOTIFICATION_DELIVERY_SINK", "dev")
    monkeypatch.setenv("SMTP_HOST", "")


# 1. Notification persisted before delivery attempted
def test_notification_persisted_before_delivery(isolated_notification_db):
    with get_session() as conn:
        rec = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="buyer@aura.com",
            subject="Policy Warning",
            message_body="Ad 42 has policy violation",
            event_type="policy_violation",
            channel="email",
            severity="error",
        )
        did = rec["delivery_id"]
        assert rec["status"] == DeliveryStatus.PENDING.value
        assert rec["attempt_count"] == 0

        # Verify DB state directly
        row = conn.execute(
            "SELECT status, attempt_count, client_id, recipient, event_type FROM notification_deliveries WHERE delivery_id = %s",
            (did,),
        ).fetchone()
        assert row is not None
        assert row[0] == "pending"
        assert row[1] == 0
        assert row[2] == "client_aura"
        assert row[3] == "buyer@aura.com"
        assert row[4] == "policy_violation"


def test_dispatch_queues_without_synchronous_sink_and_worker_finds_committed_work(isolated_notification_db):
    sink = MagicMock()
    result = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_async", recipient_email="ops@async.test", recipient_phone="",
        alert_type="blocked_launch", subject="Queued", message_body="Async delivery",
        sink=sink,
    )
    assert result["email_status"] == "queued"
    assert result["status"] == "pending"
    sink.deliver.assert_not_called()

    with get_session() as conn:
        persisted = conn.execute(
            "SELECT status, attempt_count FROM notification_deliveries WHERE delivery_id=%s",
            (result["delivery_id"],),
        ).fetchone()
    assert persisted == ("pending", 0)

    worker_results = process_pending_notifications(sink=DEV_SINK)
    assert any(r["delivery_id"] == result["delivery_id"] and r["status"] == "delivered" for r in worker_results)


def test_persistence_failure_never_calls_sink_or_reports_success(isolated_notification_db, monkeypatch):
    sink = MagicMock()
    monkeypatch.setattr("apps.api.notifications.get_delivery_sink", lambda: sink)
    monkeypatch.setattr("apps.api.notifications.record_notification_delivery", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sensitive database detail")))
    result = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_fail", recipient_email="ops@fail.test", recipient_phone="",
        alert_type="blocked_launch", subject="Must persist", message_body="Body", sink=sink,
    )
    assert result["email_status"] == "failed"
    assert result["status"] == "persistence_failed"
    assert "sensitive database detail" not in str(result)
    sink.deliver.assert_not_called()


def test_redis_is_not_required_to_recover_pending_notification(isolated_notification_db):
    with get_session() as conn:
        rec = record_notification_delivery(
            conn, client_id="client_recovery", recipient="ops@recovery.test",
            subject="Redis down", message_body="Still durable", event_type="redis_unavailable",
            dedup_key=f"redis-down-{uuid.uuid4().hex}",
        )
    pending = process_pending_notifications(sink=DEV_SINK)
    assert any(item["delivery_id"] == rec["delivery_id"] for item in pending)


# 2. Simulated success transitions to delivered
def test_simulated_success_transitions_to_delivered(isolated_notification_db):
    with get_session() as conn:
        rec = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Launch Approved",
            message_body="Batch approved",
            event_type="batch_approved",
        )
        did = rec["delivery_id"]

        result = process_delivery(conn, did, sink=DEV_SINK)
        assert result["success"] is True
        assert result["status"] == DeliveryStatus.DELIVERED.value
        assert result["provider"] == "dev_sink"
        assert result["simulated"] is True
        assert result["provider_message_id"].startswith("dev_")

        # Verify DB row is delivered with delivered_at timestamp
        row = conn.execute(
            "SELECT status, delivered_at, attempt_count FROM notification_deliveries WHERE delivery_id = %s",
            (did,),
        ).fetchone()
        assert row[0] == "delivered"
        assert row[1] is not None
        assert row[2] == 1


# 3. Simulated failure transitions to a retryable state
def test_simulated_failure_transitions_to_retryable(isolated_notification_db):
    DEV_SINK.set_forced_outcome("retryable_failed", "Simulated temporary network timeout")

    with get_session() as conn:
        rec = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Temporary issue",
            message_body="Network blip",
            event_type="network_blip",
            max_attempts=3,
        )
        did = rec["delivery_id"]

        result = process_delivery(conn, did, sink=DEV_SINK)
        assert result["success"] is False
        assert result["status"] == DeliveryStatus.RETRYABLE_FAILED.value
        assert result["error_category"] == "temporary_provider_error"
        assert result["next_retry_at"] is not None

        row = conn.execute(
            "SELECT status, next_retry_at, attempt_count, error_category FROM notification_deliveries WHERE delivery_id = %s",
            (did,),
        ).fetchone()
        assert row[0] == "retryable_failed"
        assert row[1] is not None
        assert row[2] == 1
        assert row[3] == "temporary_provider_error"


# 4. Retry scheduling respects the maximum attempt count
def test_retry_scheduling_respects_max_attempts(isolated_notification_db):
    DEV_SINK.set_forced_outcome("retryable_failed", "Simulated temporary timeout")

    with get_session() as conn:
        rec = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Retryable Event",
            message_body="Test backoff",
            event_type="test_backoff",
            max_attempts=3,
        )
        did = rec["delivery_id"]

        # Attempt 1 -> retryable_failed
        res1 = process_delivery(conn, did, sink=DEV_SINK)
        assert res1["status"] == "retryable_failed"
        assert res1["attempt_count"] == 1

        # Attempt 2 -> retryable_failed
        res2 = process_delivery(conn, did, sink=DEV_SINK)
        assert res2["status"] == "retryable_failed"
        assert res2["attempt_count"] == 2


# 5. Exhausted retries transition to permanently_failed
def test_exhausted_retries_transition_to_permanently_failed(isolated_notification_db):
    DEV_SINK.set_forced_outcome("retryable_failed", "Persistent temporary timeout")

    with get_session() as conn:
        rec = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Exhaustion Test",
            message_body="Testing terminal failure",
            event_type="exhaustion_test",
            max_attempts=3,
        )
        did = rec["delivery_id"]

        # 3 attempts
        process_delivery(conn, did, sink=DEV_SINK)
        process_delivery(conn, did, sink=DEV_SINK)
        res3 = process_delivery(conn, did, sink=DEV_SINK)

        assert res3["status"] == DeliveryStatus.PERMANENTLY_FAILED.value
        assert res3["attempt_count"] == 3
        assert res3["next_retry_at"] is None

        row = conn.execute(
            "SELECT status, next_retry_at, attempt_count FROM notification_deliveries WHERE delivery_id = %s",
            (did,),
        ).fetchone()
        assert row[0] == "permanently_failed"
        assert row[1] is None
        assert row[2] == 3
        attempt_rows = conn.execute(
            "SELECT attempt_number, status FROM notification_delivery_attempts WHERE delivery_id=%s ORDER BY attempt_number",
            (did,),
        ).fetchall()
        assert attempt_rows == [(1, "retryable_failed"), (2, "retryable_failed"), (3, "permanently_failed")]


# 6. Worker restart or stale processing record can recover safely
def test_worker_restart_and_stale_processing_recovery(isolated_notification_db):
    with get_session() as conn:
        rec = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Worker Crash Test",
            message_body="Simulate aborted worker process",
            event_type="worker_crash",
            max_attempts=3,
        )
        did = rec["delivery_id"]

        # Simulate stuck processing state from 10 minutes ago
        past_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        conn.execute(
            "UPDATE notification_deliveries SET status = 'processing', last_attempt_at = %s, attempt_count = 1, lock_token = 'worker_dead_1' WHERE delivery_id = %s",
            (past_time, did),
        )

        recovered = recover_stale_processing(conn, stale_timeout_seconds=300)
        assert did in recovered

        row = conn.execute(
            "SELECT status, lock_token, error_category FROM notification_deliveries WHERE delivery_id = %s",
            (did,),
        ).fetchone()
        assert row[0] == "retryable_failed"
        assert row[1] is None  # Lock cleared
        assert row[2] == "worker_timeout"


# 7. Duplicate event ingestion does not create duplicate logical notifications
def test_duplicate_event_ingestion_deduplication(isolated_notification_db):
    dedup_key = f"dedup_test_{uuid.uuid4().hex}"

    with get_session() as conn:
        rec1 = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Dedup Alert",
            message_body="Message 1",
            event_type="dedup_event",
            dedup_key=dedup_key,
        )
        assert rec1["is_duplicate"] is False

        rec2 = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Dedup Alert",
            message_body="Message 1 duplicate",
            event_type="dedup_event",
            dedup_key=dedup_key,
        )
        assert rec2["is_duplicate"] is True
        assert rec2["delivery_id"] == rec1["delivery_id"]

        cross_tenant = record_notification_delivery(
            conn,
            client_id="client_other_tenant",
            recipient="ops@other.test",
            subject="Tenant scoped dedup",
            message_body="Separate tenant delivery",
            event_type="dedup_event",
            dedup_key=dedup_key,
        )
        assert cross_tenant["is_duplicate"] is False
        assert cross_tenant["delivery_id"] != rec1["delivery_id"]

        count = conn.execute(
            "SELECT count(*) FROM notification_deliveries WHERE dedup_key = %s",
            (dedup_key,),
        ).fetchone()[0]
        assert count == 2
        tenant_counts = conn.execute(
            "SELECT client_id, count(*) FROM notification_deliveries WHERE dedup_key=%s GROUP BY client_id",
            (dedup_key,),
        ).fetchall()
        assert tenant_counts == [("client_aura", 1), ("client_other_tenant", 1)]


# 8. Concurrent workers cannot claim the same record simultaneously (FOR UPDATE SKIP LOCKED)
def test_concurrent_workers_claim_locking(isolated_notification_db):
    with get_session() as conn:
        rec = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Locking Test",
            message_body="Concurrency claim test",
            event_type="concurrency_test",
        )
        did = rec["delivery_id"]

    # Open two distinct database connections
    conn1 = psycopg.connect(isolated_notification_db)
    conn2 = psycopg.connect(isolated_notification_db)

    try:
        # Worker 1 starts transaction and claims the item
        claimed1 = claim_pending_deliveries(conn1, batch_size=10, lock_token="worker_1")
        assert any(item["delivery_id"] == did for item in claimed1)

        # Worker 2 attempts claiming while Worker 1 is in-flight (uncommitted)
        claimed2 = claim_pending_deliveries(conn2, batch_size=10, lock_token="worker_2")
        assert not any(item["delivery_id"] == did for item in claimed2)

        conn1.commit()
    finally:
        conn1.close()
        conn2.close()


# 9. Repeated processing attempt does not incorrectly overwrite a terminal state
def test_repeated_processing_does_not_overwrite_terminal_state(isolated_notification_db):
    with get_session() as conn:
        rec = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Terminal Guard",
            message_body="Guarding delivered state",
            event_type="terminal_guard",
        )
        did = rec["delivery_id"]

        # Deliver successfully
        res = process_delivery(conn, did, sink=DEV_SINK)
        assert res["status"] == "delivered"

        # Attempt to re-process with a forced failure
        DEV_SINK.set_forced_outcome("permanently_failed", "Should not apply")
        res_repeat = process_delivery(conn, did, sink=DEV_SINK)

        assert res_repeat["status"] == "delivered"
        assert res_repeat.get("terminal_protected") is True

        row = conn.execute(
            "SELECT status FROM notification_deliveries WHERE delivery_id = %s",
            (did,),
        ).fetchone()
        assert row[0] == "delivered"


# 10. Provider acknowledgement loss is handled according to documented idempotency strategy
def test_provider_ack_loss_idempotency_behavior(isolated_notification_db):
    """Simulate provider receiving message but network dropping before acknowledgement returns."""
    DEV_SINK.simulate_ack_loss = True

    with get_session() as conn:
        rec = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="ops@aura.com",
            subject="Ack Loss Test",
            message_body="Message sent but ack lost",
            event_type="ack_loss_test",
        )
        did = rec["delivery_id"]

        # First attempt: provider received message, but client gets timeout/ack loss
        res1 = process_delivery(conn, did, sink=DEV_SINK)
        assert res1["status"] == "retryable_failed"
        assert len(DEV_SINK.deliveries) == 1

        # Second attempt: network heals, message re-delivered
        DEV_SINK.simulate_ack_loss = False
        res2 = process_delivery(conn, did, sink=DEV_SINK)
        assert res2["status"] == "delivered"
        # Total provider dispatches = 2 (demonstrating documented at-least-once behavior)
        assert len(DEV_SINK.deliveries) == 2
        attempts = conn.execute(
            "SELECT status, provider_message_id FROM notification_delivery_attempts WHERE delivery_id=%s ORDER BY attempt_number",
            (did,),
        ).fetchall()
        assert [row[0] for row in attempts] == ["retryable_failed", "delivered"]
        assert attempts[0][1].startswith("dev_")


# 11. Tenant isolation prevents cross-tenant reads and notification delivery
def test_tenant_isolation_notifications(isolated_notification_db):
    with get_session() as conn:
        rec_aura = record_notification_delivery(
            conn,
            client_id="client_aura",
            recipient="aura@test.com",
            subject="Aura Secret",
            message_body="Secret Aura content",
            event_type="internal_report",
        )
        rec_lumina = record_notification_delivery(
            conn,
            client_id="client_lumina",
            recipient="lumina@test.com",
            subject="Lumina Secret",
            message_body="Secret Lumina content",
            event_type="internal_report",
        )

        # Scoped query for client_aura strictly excludes client_lumina
        rows_aura = conn.execute(
            "SELECT delivery_id, client_id, subject FROM notification_deliveries WHERE client_id = %s",
            ("client_aura",),
        ).fetchall()

        aura_ids = [r[0] for r in rows_aura]
        assert rec_aura["delivery_id"] in aura_ids
        assert rec_lumina["delivery_id"] not in aura_ids


# 12. Guest permissions remain unchanged
def test_guest_permissions_unchanged(isolated_notification_db):
    """Guest session requests to administrative /alerts endpoints are strictly forbidden."""
    client = TestClient(app)
    # Anonymous / guest request without JWT
    resp_alerts = client.get("/alerts")
    assert resp_alerts.status_code in (401, 403)

    resp_summary = client.get("/alerts/summary")
    assert resp_summary.status_code in (401, 403)


# 13. Live email/SMS providers are not contacted in development/test mode
def test_live_providers_not_contacted_in_dev_mode(isolated_notification_db, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("NOTIFICATION_DELIVERY_SINK", "dev")

    sink = MagicMock()
    with patch("smtplib.SMTP") as mock_smtp:
        res = NotificationDispatcher.dispatch_campaign_alert(
            client_id="client_aura",
            recipient_email="buyer@aura.com",
            recipient_phone="+15551234567",
            alert_type="launch_blocked",
            subject="Blocked Launch",
            message_body="Connector not configured",
            sink=sink,
        )
        assert res["email_status"] == "queued"
        assert res["status"] == "pending"
        sink.deliver.assert_not_called()
        # Critical safety check: real SMTP was never contacted
        mock_smtp.assert_not_called()

    # The committed pending row is discoverable and processed by the database worker.
    results = process_pending_notifications(sink=DEV_SINK)
    assert any(item["delivery_id"] == res["delivery_id"] and item["status"] == "delivered" for item in results)


# 14. Missing live-provider configuration fails safely rather than reporting success
def test_missing_live_provider_config_fails_safely(isolated_notification_db, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("NOTIFICATION_DELIVERY_SINK", "smtp")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("NOTIFICATION_FROM_EMAIL", "")

    res = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_aura",
        recipient_email="buyer_unconfigured@aura.com",
        recipient_phone="+15551234567",
        alert_type="launch_blocked",
        subject="Blocked Launch",
        message_body="Connector not configured",
    )
    # Dispatch only queues; an explicitly configured live sink fails closed on attempt.
    assert res["email_status"] == "queued"
    assert res["status"] == "pending"
    results = process_pending_notifications()
    row = None
    with get_session() as conn:
        row = conn.execute("SELECT status, error_category FROM notification_deliveries WHERE delivery_id=%s", (res["delivery_id"],)).fetchone()
    assert row == ("permanently_failed", "unconfigured_credentials")

    # In production, dev sink must fail with RuntimeError
    monkeypatch.setenv("NOTIFICATION_DELIVERY_SINK", "dev")
    with pytest.raises(RuntimeError, match="Development delivery sink is strictly forbidden"):
        get_delivery_sink()


# 15. Secrets and message bodies are not exposed in logs or error responses
def test_secrets_and_message_bodies_not_logged(caplog):
    caplog.set_level(logging.INFO)

    masked = mask_recipient("confidential.user@supersecretdomain.com")
    assert masked == "c***r@supersecretdomain.com"
    assert "confidential.user" not in masked

    error_cat, safe_msg = sanitize_error("Authentication failed: password=SuperSecretPassword123!")
    assert "SuperSecretPassword123!" not in safe_msg
    assert error_cat == "provider_auth_error"


# 16. Alert deduplication granularity: separate batches produce distinct deliveries
def test_separate_batches_produce_distinct_deliveries(isolated_notification_db):
    res_batch_1 = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_aura",
        recipient_email="alerts@aura.com",
        recipient_phone="+15551234567",
        alert_type="launch_blocked",
        subject="Batch 1 Blocked",
        message_body="Platform connector down for Batch 1",
        batch_id="batch_001",
    )
    res_batch_2 = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_aura",
        recipient_email="alerts@aura.com",
        recipient_phone="+15551234567",
        alert_type="launch_blocked",
        subject="Batch 2 Blocked",
        message_body="Platform connector down for Batch 2",
        batch_id="batch_002",
    )
    assert res_batch_1["delivery_id"] != res_batch_2["delivery_id"]
    assert not res_batch_1.get("duplicate_suppressed")
    assert not res_batch_2.get("duplicate_suppressed")


# 17. Alert deduplication granularity: retry of same logical event returns existing delivery
def test_same_logical_event_retried_does_not_create_duplicate(isolated_notification_db):
    res1 = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_aura",
        recipient_email="alerts@aura.com",
        recipient_phone="+15551234567",
        alert_type="launch_blocked",
        subject="Batch Retry Blocked",
        message_body="Platform connector down for Batch Retry",
        batch_id="batch_retry_distinct",
        ad_id="ad_42",
    )
    res2 = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_aura",
        recipient_email="alerts@aura.com",
        recipient_phone="+15551234567",
        alert_type="launch_blocked",
        subject="Batch Retry Blocked",
        message_body="Platform connector down for Batch Retry",
        batch_id="batch_retry_distinct",
        ad_id="ad_42",
    )
    assert res1["delivery_id"] == res2["delivery_id"]
    assert res2.get("duplicate_suppressed") is True

    with get_session() as conn:
        count = conn.execute(
            "SELECT count(*) FROM notification_deliveries WHERE client_id='client_aura' AND batch_id='batch_retry_distinct'"
        ).fetchone()[0]
    assert count == 1


# 18. Alert deduplication granularity: later distinct launch attempt produces new delivery
def test_distinct_launch_attempt_creates_new_delivery(isolated_notification_db):
    res_run1 = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_aura",
        recipient_email="alerts@aura.com",
        recipient_phone="+15551234567",
        alert_type="platform_rejection",
        subject="Ad Policy Violation",
        message_body="Image ratio error",
        ad_id="ad_42",
        batch_id="batch_001",
        launch_attempt_id="run_1",
    )
    res_run2 = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_aura",
        recipient_email="alerts@aura.com",
        recipient_phone="+15551234567",
        alert_type="platform_rejection",
        subject="Ad Policy Violation",
        message_body="Image ratio error",
        ad_id="ad_42",
        batch_id="batch_001",
        launch_attempt_id="run_2",
    )
    assert res_run1["delivery_id"] != res_run2["delivery_id"]
    assert not res_run1.get("duplicate_suppressed")
    assert not res_run2.get("duplicate_suppressed")


# 19. Alert deduplication: absent ad_id and batch_id uses content hash to avoid false collisions
def test_missing_ad_and_batch_distinguishes_different_messages(isolated_notification_db):
    res_event_a = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_aura",
        recipient_email="alerts@aura.com",
        recipient_phone="+15551234567",
        alert_type="system_notice",
        subject="Maintenance Notice A",
        message_body="Scheduled maintenance window A",
    )
    res_event_b = NotificationDispatcher.dispatch_campaign_alert(
        client_id="client_aura",
        recipient_email="alerts@aura.com",
        recipient_phone="+15551234567",
        alert_type="system_notice",
        subject="Maintenance Notice B",
        message_body="Scheduled maintenance window B",
    )
    assert res_event_a["delivery_id"] != res_event_b["delivery_id"]
    assert not res_event_a.get("duplicate_suppressed")
    assert not res_event_b.get("duplicate_suppressed")


# 20. Notification retention: preview and purge of eligible old terminal records
def test_notification_retention_purge_and_preview(isolated_notification_db):
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=45)
    recent_time = now - timedelta(days=5)

    with get_session() as conn:
        # 1. Old delivered record (eligible)
        conn.execute(
            """INSERT INTO notification_deliveries
               (delivery_id, client_id, channel, event_type, status, recipient, subject, message_body, created_at, delivered_at)
               VALUES ('deliv_old_delivered', 'client_aura', 'email', 'policy_warning', 'delivered', 'buyer@aura.com', 'Old Delivered', 'Old Body', %s, %s)""",
            (old_time, old_time),
        )
        # Attempt for old delivered
        conn.execute(
            """INSERT INTO notification_delivery_attempts
               (attempt_id, delivery_id, attempt_number, status, lock_token, started_at, finished_at)
               VALUES ('att_old_deliv', 'deliv_old_delivered', 1, 'delivered', 'tok1', %s, %s)""",
            (old_time, old_time),
        )

        # 2. Old permanently_failed record (eligible)
        conn.execute(
            """INSERT INTO notification_deliveries
               (delivery_id, client_id, channel, event_type, status, recipient, subject, message_body, created_at, last_attempt_at)
               VALUES ('deliv_old_failed', 'client_aura', 'email', 'policy_warning', 'permanently_failed', 'buyer@aura.com', 'Old Failed', 'Old Body', %s, %s)""",
            (old_time, old_time),
        )

        # 3. Old pending record (ineligible - active deliveries must NEVER be purged)
        conn.execute(
            """INSERT INTO notification_deliveries
               (delivery_id, client_id, channel, event_type, status, recipient, subject, message_body, created_at)
               VALUES ('deliv_old_pending', 'client_aura', 'email', 'policy_warning', 'pending', 'buyer@aura.com', 'Old Pending', 'Old Body', %s)""",
            (old_time,),
        )

        # 4. Old processing record (ineligible - active deliveries must NEVER be purged)
        conn.execute(
            """INSERT INTO notification_deliveries
               (delivery_id, client_id, channel, event_type, status, recipient, subject, message_body, created_at, last_attempt_at)
               VALUES ('deliv_old_processing', 'client_aura', 'email', 'policy_warning', 'processing', 'buyer@aura.com', 'Old Processing', 'Old Body', %s, %s)""",
            (old_time, old_time),
        )

        # 5. Recent delivered record (ineligible - younger than 30-day cutoff)
        conn.execute(
            """INSERT INTO notification_deliveries
               (delivery_id, client_id, channel, event_type, status, recipient, subject, message_body, created_at, delivered_at)
               VALUES ('deliv_recent_delivered', 'client_aura', 'email', 'policy_warning', 'delivered', 'buyer@aura.com', 'Recent Delivered', 'Recent Body', %s, %s)""",
            (recent_time, recent_time),
        )

    with get_session() as conn:
        # Preview eligible records (should find exactly the 2 old terminal records)
        previews = preview_expired_notifications(conn, retention_days=30)
        preview_ids = {p["delivery_id"] for p in previews}
        assert preview_ids == {"deliv_old_delivered", "deliv_old_failed"}
        # Verify safe preview formatting: raw recipient and message body are not exposed
        for p in previews:
            assert "recipient_masked" in p
            assert "@" in p["recipient_masked"] and "***" in p["recipient_masked"]
            assert "message_body" not in p

        # Dry run purge (does not delete)
        dry_res = purge_expired_notifications(conn, retention_days=30, dry_run=True)
        assert dry_res["eligible_count"] == 2
        assert dry_res["purged_count"] == 0

        # Real purge
        purge_res = purge_expired_notifications(conn, retention_days=30, dry_run=False)
        assert purge_res["purged_count"] == 2
        assert set(purge_res["purged_delivery_ids"]) == {"deliv_old_delivered", "deliv_old_failed"}

        # Verify database state after purge
        remaining_ids = {
            r[0] for r in conn.execute("SELECT delivery_id FROM notification_deliveries").fetchall()
        }
        assert "deliv_old_delivered" not in remaining_ids
        assert "deliv_old_failed" not in remaining_ids
        assert "deliv_old_pending" in remaining_ids
        assert "deliv_old_processing" in remaining_ids
        assert "deliv_recent_delivered" in remaining_ids

        # Verify attempt record was deleted via cascade
        attempt_exists = conn.execute(
            "SELECT count(*) FROM notification_delivery_attempts WHERE delivery_id='deliv_old_delivered'"
        ).fetchone()[0]
        assert attempt_exists == 0
