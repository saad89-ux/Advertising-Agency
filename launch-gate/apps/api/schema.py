"""Application database schema initialization."""

from apps.api import (
    ads,
    approvals,
    batches,
    clients,
    deduplication,
    edits,
    evidence,
    findings,
    notifications,
    outbox,
    rejected_rows,
)
from apps.api import guest_sessions
from apps.api.platforms_schema import init_platform_tables
from apps.worker import cost_ledger, sender
from apps.worker.tracing import create_trace_tables


def init_schema() -> None:
    """Create the API's tables and constraints in dependency order."""
    clients.create_client_tables()
    guest_sessions.init_guest_tables()
    batches.create_batches_table()
    guest_sessions.init_batch_guest_ownership()
    ads.create_ads_table()
    ads.ensure_platform_raw_column()
    init_platform_tables()
    outbox.create_ad_job_outbox_table()
    deduplication.create_deduplication_constraints()
    rejected_rows.create_rejected_rows_table()
    findings.create_findings_table()
    approvals.create_approval_tables()
    evidence.create_approval_evidence_table()
    edits.create_ad_edits_table()
    cost_ledger.create_cost_ledger_table()
    sender.create_send_tables()
    notifications.create_notification_tables()
    create_trace_tables()
