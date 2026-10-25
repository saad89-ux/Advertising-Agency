"""Database tables and initial seeding for social platform handles, live dispatches, and sales attribution."""

from __future__ import annotations

import logging
from typing import Any

from apps.api.database import get_session

logger = logging.getLogger(__name__)

CREATE_PLATFORMS_TABLES = """
-- 1. Connected Social Platform Accounts / Handles
CREATE TABLE IF NOT EXISTS platform_accounts (
    account_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('instagram', 'facebook', 'tiktok', 'snapchat', 'x')),
    handle TEXT NOT NULL,
    account_name TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'api_token',
    credentials_preview TEXT NOT NULL,
    encrypted_credentials TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'connected' CHECK (status IN ('connected', 'active', 'rate_limited', 'disconnected')),
    last_sync_at TIMESTAMPTZ DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb,
    is_sample BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_platform_accounts_client ON platform_accounts (client_id);
CREATE INDEX IF NOT EXISTS idx_platform_accounts_platform ON platform_accounts (platform);

-- 2. Live Ad Dispatches & Platform Review Status
CREATE TABLE IF NOT EXISTS ad_live_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    account_id TEXT REFERENCES platform_accounts (account_id) ON DELETE SET NULL,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active_live' CHECK (status IN ('scheduled', 'authenticating', 'dispatching', 'pending_review', 'active_live', 'rejected', 'paused', 'blocked_connector_not_configured')),
    platform_ad_id TEXT NOT NULL,
    platform_review_status TEXT NOT NULL DEFAULT 'Approved by Platform Review',
    platform_review_notes TEXT,
    live_preview_url TEXT,
    is_sample BOOLEAN NOT NULL DEFAULT FALSE,
    dispatched_at TIMESTAMPTZ DEFAULT NOW(),
    last_tracked_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ad_live_dispatches_ad ON ad_live_dispatches (ad_id);
CREATE INDEX IF NOT EXISTS idx_ad_live_dispatches_client ON ad_live_dispatches (client_id);

-- 3. Inbound Sales & Continuous Attribution Tracking
CREATE TABLE IF NOT EXISTS ad_performance_tracking (
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    inbound_sales_usd NUMERIC(12, 2),
    inbound_orders INT,
    impressions INT,
    clicks INT,
    ctr_percent NUMERIC(5, 2),
    roas NUMERIC(6, 2),
    spend_usd NUMERIC(10, 2),
    has_rule_violation BOOLEAN NOT NULL DEFAULT FALSE,
    violation_details TEXT,
    last_event TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ad_id, platform)
);
DO $$
DECLARE
    primary_key_name TEXT;
    primary_key_definition TEXT;
BEGIN
    SELECT conname, pg_get_constraintdef(oid)
    INTO primary_key_name, primary_key_definition
    FROM pg_constraint
    WHERE conrelid = 'ad_performance_tracking'::regclass AND contype = 'p';
    IF primary_key_definition <> 'PRIMARY KEY (ad_id, platform)' THEN
        EXECUTE format('ALTER TABLE ad_performance_tracking DROP CONSTRAINT %I', primary_key_name);
        ALTER TABLE ad_performance_tracking
            ADD CONSTRAINT ad_performance_tracking_pkey PRIMARY KEY (ad_id, platform);
    END IF;
END $$;
ALTER TABLE ad_performance_tracking ALTER COLUMN inbound_sales_usd DROP NOT NULL;
ALTER TABLE ad_performance_tracking ALTER COLUMN inbound_sales_usd DROP DEFAULT;
ALTER TABLE ad_performance_tracking ALTER COLUMN inbound_orders DROP NOT NULL;
ALTER TABLE ad_performance_tracking ALTER COLUMN inbound_orders DROP DEFAULT;
ALTER TABLE ad_performance_tracking ALTER COLUMN impressions DROP NOT NULL;
ALTER TABLE ad_performance_tracking ALTER COLUMN impressions DROP DEFAULT;
ALTER TABLE ad_performance_tracking ALTER COLUMN clicks DROP NOT NULL;
ALTER TABLE ad_performance_tracking ALTER COLUMN clicks DROP DEFAULT;
ALTER TABLE ad_performance_tracking ALTER COLUMN ctr_percent DROP NOT NULL;
ALTER TABLE ad_performance_tracking ALTER COLUMN ctr_percent DROP DEFAULT;
ALTER TABLE ad_performance_tracking ALTER COLUMN roas DROP NOT NULL;
ALTER TABLE ad_performance_tracking ALTER COLUMN roas DROP DEFAULT;
ALTER TABLE ad_performance_tracking ALTER COLUMN spend_usd DROP NOT NULL;
ALTER TABLE ad_performance_tracking ALTER COLUMN spend_usd DROP DEFAULT;
CREATE INDEX IF NOT EXISTS idx_ad_perf_client ON ad_performance_tracking (client_id);
CREATE INDEX IF NOT EXISTS idx_ad_perf_platform ON ad_performance_tracking (platform);

CREATE TABLE IF NOT EXISTS meta_sync_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    sync_type VARCHAR(50) NOT NULL CHECK (sync_type IN ('INITIAL_IMPORT', 'METRIC_REFRESH')),
    status VARCHAR(50) NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    records_synced INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
ALTER TABLE meta_sync_logs ADD COLUMN IF NOT EXISTS records_synced INTEGER NOT NULL DEFAULT 0;
ALTER TABLE meta_sync_logs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE meta_sync_logs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE meta_sync_logs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_meta_sync_logs_workspace_started
    ON meta_sync_logs(workspace_id, started_at DESC);

CREATE TABLE IF NOT EXISTS meta_sync_outbox (
    sync_id UUID PRIMARY KEY REFERENCES meta_sync_logs(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    sync_type VARCHAR(50) NOT NULL CHECK (sync_type IN ('INITIAL_IMPORT', 'METRIC_REFRESH')),
    lookback_days INTEGER NOT NULL CHECK (lookback_days BETWEEN 1 AND 90),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    queued_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_meta_sync_outbox_pending
    ON meta_sync_outbox(created_at, sync_id) WHERE queued_at IS NULL;

CREATE TABLE IF NOT EXISTS meta_synced_objects (
    workspace_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    meta_object_id VARCHAR(255) NOT NULL,
    object_type VARCHAR(20) NOT NULL CHECK (object_type IN ('CAMPAIGN', 'ADSET', 'AD')),
    parent_meta_object_id VARCHAR(255),
    name TEXT,
    effective_status VARCHAR(100),
    objective VARCHAR(100),
    configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_sync_id UUID REFERENCES meta_sync_logs(id) ON DELETE SET NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, meta_object_id)
);
CREATE INDEX IF NOT EXISTS idx_meta_synced_objects_parent
    ON meta_synced_objects(workspace_id, parent_meta_object_id);
CREATE INDEX IF NOT EXISTS idx_meta_synced_objects_type_status
    ON meta_synced_objects(workspace_id, object_type, effective_status);

CREATE TABLE IF NOT EXISTS meta_performance_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    meta_object_id VARCHAR(255) NOT NULL,
    object_type VARCHAR(20) NOT NULL CHECK (object_type IN ('CAMPAIGN', 'ADSET', 'AD')),
    spend NUMERIC(12, 2) NOT NULL DEFAULT 0,
    impressions BIGINT NOT NULL DEFAULT 0,
    clicks BIGINT NOT NULL DEFAULT 0,
    conversions NUMERIC(14, 2) NOT NULL DEFAULT 0,
    reporting_start DATE NOT NULL,
    reporting_end DATE NOT NULL,
    source_attribution VARCHAR(100) NOT NULL DEFAULT 'META_INSIGHTS_API',
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (reporting_end >= reporting_start),
    UNIQUE (workspace_id, meta_object_id, object_type, reporting_start, reporting_end, source_attribution)
);
CREATE INDEX IF NOT EXISTS idx_meta_metrics_lookup
    ON meta_performance_metrics(workspace_id, meta_object_id, reporting_start, reporting_end);
"""


def init_platform_tables(connection: Any = None) -> None:
    """Create platform accounts, dispatches, and performance tracking tables."""
    def _run(conn: Any):
        with conn.cursor() as cur:
            cur.execute(CREATE_PLATFORMS_TABLES)
        if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
            conn.commit()

    if connection is not None:
        _run(connection)
    else:
        with get_session() as session:
            _run(session)
