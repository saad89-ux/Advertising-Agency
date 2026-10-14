"""Application database schema initialization."""

from apps.api import (
    ads,
    batches,
    clients,
    deduplication,
    findings,
    outbox,
    rejected_rows,
)


def init_schema() -> None:
    """Create the API's tables and constraints in dependency order."""
    clients.create_client_tables()
    batches.create_batches_table()
    ads.create_ads_table()
    outbox.create_ad_job_outbox_table()
    deduplication.create_deduplication_constraints()
    rejected_rows.create_rejected_rows_table()
    findings.create_findings_table()
