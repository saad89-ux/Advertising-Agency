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
    outbox,
    rejected_rows,
)
from apps.worker import sender


def init_schema() -> None:
    """Create the API's tables and constraints in dependency order."""
    clients.create_client_tables()
    batches.create_batches_table()
    ads.create_ads_table()
    ads.ensure_platform_raw_column()
    outbox.create_ad_job_outbox_table()
    deduplication.create_deduplication_constraints()
    rejected_rows.create_rejected_rows_table()
    findings.create_findings_table()
    findings.create_dismissed_findings_table()
    approvals.create_approval_tables()
    evidence.create_approval_evidence_table()
    edits.create_ad_edits_table()
    sender.create_send_tables()
