"""Campaign Flow API — Meta review, MCP dispatches, HITL remediation, and analytics."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import hashlib
import json
import functools
import inspect
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from apps.api.access import require_client_access
from apps.api.auth import CurrentUser, get_current_user, require_role
from apps.api.guest_sessions import get_request_user, require_guest_csrf
from apps.api.database import get_session
from apps.api.notifications import NotificationDispatcher, create_notification_tables
from apps.api.client_ip import get_client_ip
from apps.api.ingest import hash_normalized_row, hash_normalized_sheet
from apps.api.platforms_schema import init_platform_tables
from packages.contracts import BatchRowContract, validate_batch_row_consistency
from apps.worker.agent_team import IssueTrackingSpecialistNode, RevenueTrackingSpecialistNode
from apps.worker.mcp_client import MCPPlatformDispatcher, MCPResponse
from apps.worker.progress_emitter import emit_progress_event
from apps.worker.states import MetaEntityStatus, entity_status_for_run
from packages.rules.platform_normalization import normalize_platform

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaign-flow", tags=["campaign-flow"])


def _metric_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return _metric_number(json.loads(value))
        except (TypeError, ValueError):
            try:
                return float(value)
            except ValueError:
                return None
    if isinstance(value, list):
        values = [_metric_number(item) for item in value]
        present = [item for item in values if item is not None]
        return sum(present) if present else None
    if isinstance(value, dict):
        return _metric_number(value.get("value"))
    return None


def endpoint_errors(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Campaign endpoint %s failed", func.__name__)
            raise HTTPException(status_code=500, detail="Internal server error. See server logs.") from exc
    wrapped.__signature__ = inspect.signature(func, eval_str=True)
    return wrapped


# --- Table Creation for Launch Notification Contacts ---

CREATE_CONTACTS_TABLE = """
ALTER TABLE batches ADD COLUMN IF NOT EXISTS campaign_name TEXT;
CREATE TABLE IF NOT EXISTS campaign_launch_contacts (
    contact_id TEXT PRIMARY KEY,
    batch_id TEXT,
    client_id TEXT NOT NULL,
    employee_email TEXT NOT NULL,
    employee_phone TEXT NOT NULL,
    notification_channel TEXT NOT NULL DEFAULT 'smtp_email',
    uploader_ip TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE campaign_launch_contacts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE campaign_launch_contacts ADD COLUMN IF NOT EXISTS uploader_ip TEXT;
ALTER TABLE campaign_launch_contacts ALTER COLUMN batch_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS idx_launch_contacts_batch ON campaign_launch_contacts (batch_id);
DELETE FROM campaign_launch_contacts old USING campaign_launch_contacts newer
WHERE old.batch_id = newer.batch_id AND old.employee_email = newer.employee_email
  AND (old.created_at, old.contact_id) < (newer.created_at, newer.contact_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_launch_contacts_batch_email
    ON campaign_launch_contacts (batch_id, employee_email);
ALTER TABLE ad_live_dispatches ADD COLUMN IF NOT EXISTS launch_attempt_id TEXT;
ALTER TABLE send_alerts ADD COLUMN IF NOT EXISTS launch_attempt_id TEXT;
ALTER TABLE ad_live_dispatches ADD COLUMN IF NOT EXISTS meta_campaign_id TEXT;
ALTER TABLE ad_live_dispatches ADD COLUMN IF NOT EXISTS meta_ad_set_id TEXT;
ALTER TABLE ad_live_dispatches ADD COLUMN IF NOT EXISTS meta_creative_id TEXT;
CREATE TABLE IF NOT EXISTS meta_launch_objects (
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('facebook', 'instagram')),
    campaign_id TEXT,
    ad_set_id TEXT,
    creative_id TEXT,
    platform_ad_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ad_id, platform)
);
CREATE TABLE IF NOT EXISTS meta_launch_campaigns (
    entity_key TEXT PRIMARY KEY,
    batch_id TEXT,
    client_id TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('facebook', 'instagram')),
    grouping_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    budget_minor_units INTEGER NOT NULL DEFAULT 0,
    meta_campaign_id TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'pending',
    hierarchy_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS meta_launch_activation_audit (
    activation_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    hierarchy_hash TEXT NOT NULL,
    campaign_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    meta_campaign_id TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    confirmed_budget_minor_units INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (batch_id, hierarchy_hash, campaign_key, platform)
);
CREATE TABLE IF NOT EXISTS meta_launch_approval_audit (
    authorization_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    hierarchy_hash TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (batch_id, client_id, hierarchy_hash)
);
CREATE TABLE IF NOT EXISTS meta_launch_ad_sets (
    entity_key TEXT PRIMARY KEY,
    campaign_entity_key TEXT NOT NULL REFERENCES meta_launch_campaigns(entity_key) ON DELETE CASCADE,
    batch_id TEXT,
    client_id TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('facebook', 'instagram')),
    grouping_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    meta_ad_set_id TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'pending',
    hierarchy_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS meta_campaign_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    grouping_key VARCHAR(255) NOT NULL,
    platform VARCHAR(50) NOT NULL DEFAULT 'meta',
    meta_id VARCHAR(255) UNIQUE,
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    approval_snapshot_hash VARCHAR(64) NOT NULL,
    config_hash VARCHAR(64) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'CREATING', 'CREATED', 'FAILED', 'RECONCILING')),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (batch_id, workspace_id, grouping_key)
);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='meta_campaign_runs'::regclass
          AND conname='meta_campaign_runs_status_lifecycle_check'
    ) THEN
        ALTER TABLE meta_campaign_runs DROP CONSTRAINT IF EXISTS meta_campaign_runs_status_check;
        ALTER TABLE meta_campaign_runs ADD CONSTRAINT meta_campaign_runs_status_lifecycle_check
            CHECK (status IN ('PENDING', 'CREATING', 'CREATED', 'FAILED', 'RECONCILING', 'ACTIVE'));
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS meta_ad_set_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_run_id UUID NOT NULL REFERENCES meta_campaign_runs(id) ON DELETE CASCADE,
    grouping_key VARCHAR(255) NOT NULL,
    meta_id VARCHAR(255) UNIQUE,
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    approval_snapshot_hash VARCHAR(64) NOT NULL,
    config_hash VARCHAR(64) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'CREATING', 'CREATED', 'FAILED', 'RECONCILING')),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (campaign_run_id, grouping_key)
);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='meta_ad_set_runs'::regclass
          AND conname='meta_ad_set_runs_status_lifecycle_check'
    ) THEN
        ALTER TABLE meta_ad_set_runs DROP CONSTRAINT IF EXISTS meta_ad_set_runs_status_check;
        ALTER TABLE meta_ad_set_runs ADD CONSTRAINT meta_ad_set_runs_status_lifecycle_check
            CHECK (status IN ('PENDING', 'CREATING', 'CREATED', 'FAILED', 'RECONCILING', 'ACTIVE'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_meta_campaign_runs_batch_workspace
    ON meta_campaign_runs(batch_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_meta_ad_set_runs_campaign
    ON meta_ad_set_runs(campaign_run_id);
INSERT INTO meta_campaign_runs
    (batch_id, workspace_id, grouping_key, platform, meta_id, idempotency_key,
     approval_snapshot_hash, config_hash, status, last_error, created_at, updated_at)
SELECT batch_id, client_id, grouping_key, 'meta', meta_campaign_id, idempotency_key,
       COALESCE(hierarchy_hash, repeat('0', 64)), COALESCE(hierarchy_hash, repeat('0', 64)),
       CASE lifecycle_status WHEN 'created_paused' THEN 'CREATED' WHEN 'active' THEN 'ACTIVE'
            WHEN 'failed' THEN 'FAILED' ELSE 'PENDING' END,
       last_error, created_at, updated_at
FROM meta_launch_campaigns
ON CONFLICT (batch_id, workspace_id, grouping_key) DO NOTHING;
INSERT INTO meta_ad_set_runs
    (campaign_run_id, grouping_key, meta_id, idempotency_key, approval_snapshot_hash,
     config_hash, status, last_error, created_at, updated_at)
SELECT campaign_run.id, legacy_ad_set.grouping_key, legacy_ad_set.meta_ad_set_id,
       legacy_ad_set.idempotency_key,
       COALESCE(legacy_ad_set.hierarchy_hash, repeat('0', 64)),
       COALESCE(legacy_ad_set.hierarchy_hash, repeat('0', 64)),
       CASE legacy_ad_set.lifecycle_status WHEN 'created_paused' THEN 'CREATED' WHEN 'active' THEN 'ACTIVE'
            WHEN 'failed' THEN 'FAILED' ELSE 'PENDING' END,
       legacy_ad_set.last_error, legacy_ad_set.created_at, legacy_ad_set.updated_at
FROM meta_launch_ad_sets legacy_ad_set
JOIN meta_launch_campaigns legacy_campaign ON legacy_campaign.entity_key=legacy_ad_set.campaign_entity_key
JOIN meta_campaign_runs campaign_run ON campaign_run.batch_id=legacy_campaign.batch_id
    AND campaign_run.workspace_id=legacy_campaign.client_id
    AND campaign_run.grouping_key=legacy_campaign.grouping_key
ON CONFLICT (campaign_run_id, grouping_key) DO NOTHING;
"""


def init_flow_tables():
    init_platform_tables()
    create_notification_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_CONTACTS_TABLE)
        if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
            conn.commit()


# --- Pydantic Request Models ---

class PreflightReviewRequest(BaseModel):
    client_id: str = ""
    batch_id: str | None = None
    input_text: str = ""
    file_name: str | None = None
    file_type: str | None = None
    social_handles: dict[str, str] = Field(default_factory=dict)
    social_tokens: dict[str, str] = Field(default_factory=dict)
    ad_overrides: dict[str, Any] = Field(default_factory=dict)


class LaunchAdModel(BaseModel):
    ad_key: str
    ad_id: str | None = None
    ad_set_key: str
    platform: str
    headline: str
    ad_text: str
    destination_url: str
    creative_file: str
    creative_mime_type: str | None = None
    page_id: str | None = None
    instagram_user_id: str | None = None


class LaunchAdSetModel(BaseModel):
    ad_set_key: str
    campaign_key: str
    name: str
    targeting_country: str
    targeting: dict[str, Any] = Field(default_factory=dict)
    placements: list[str] = Field(default_factory=list)
    optimization_goal: str = "LINK_CLICKS"
    billing_event: str = "IMPRESSIONS"
    daily_budget_minor_units: int | None = Field(None, gt=0)
    lifetime_budget_minor_units: int | None = Field(None, gt=0)
    schedule: dict[str, Any] = Field(default_factory=dict)
    ads: list[LaunchAdModel] = Field(default_factory=list)


class LaunchCampaignModel(BaseModel):
    campaign_key: str
    name: str
    objective: str = "OUTCOME_TRAFFIC"
    budget_strategy: str = "DAILY"
    daily_budget_minor_units: int | None = Field(None, gt=0)
    lifetime_budget_minor_units: int | None = Field(None, gt=0)
    currency: str = "USD"
    special_ad_category: str = "NONE"
    ad_sets: list[LaunchAdSetModel] = Field(default_factory=list)


class LaunchBatchRequest(BaseModel):
    client_id: str = ""
    batch_id: str | None = None
    launch_attempt_id: str | None = None
    meta_launch: bool = False
    campaign_name: str = "Multi-Platform Growth Burst"
    employee_email: str
    employee_phone: str = ""
    target_platforms: list[str] = Field(min_length=1, description="At least one target platform must be selected")
    headline: str | None = None
    ad_text: str | None = None
    destination_url: str | None = None
    creative_file: str | None = None
    creative_mime_type: str | None = None
    meta_page_id: str | None = None
    instagram_user_id: str | None = None
    targeting_country: str | None = None
    daily_budget_minor_units: int | None = Field(None, ge=1)
    special_ad_category: str | None = None
    campaigns: list[LaunchCampaignModel] = Field(default_factory=list)
    hierarchy_preview_hash: str | None = None


class RetryHierarchyEntityRequest(BaseModel):
    batch_id: str
    entity_key: str
    entity_type: str


class ActivateHierarchyRequest(BaseModel):
    batch_id: str
    campaign_keys: list[str] = Field(default_factory=list)
    hierarchy_hash: str
    confirmed_budget_minor_units: int = Field(gt=0)


class HitlRemediateRequest(BaseModel):
    ad_id: str
    revised_headline: str | None = None
    revised_text: str | None = None
    confirm_employee_notification: bool = True
    notification_email: str | None = None


class ManualAlertTestRequest(BaseModel):
    client_id: str = ""
    recipient_email: str
    recipient_phone: str = ""
    alert_type: str = "manual_test"



# --- Hierarchy validation and preview ---

def build_launch_hierarchy(req: LaunchBatchRequest) -> dict[str, Any]:
    """Normalize legacy launch settings and validated explicit hierarchy rows."""
    if req.campaigns:
        campaign_keys = [campaign.campaign_key for campaign in req.campaigns]
        if len(set(campaign_keys)) != len(campaign_keys):
            raise HTTPException(status_code=422, detail="campaign_key values must be unique.")
        hierarchy = {"campaigns": [campaign.model_dump() for campaign in req.campaigns]}
    else:
        if not all((req.headline, req.ad_text, req.destination_url)):
            return {"hierarchy": {"campaigns": []}, "hierarchy_hash": "", "preview": {}}
        campaign_key = "campaign-default"
        ad_set_key = "ad-set-default"
        hierarchy = {"campaigns": [{
            "campaign_key": campaign_key,
            "name": req.campaign_name,
            "objective": "OUTCOME_TRAFFIC",
            "budget_strategy": "DAILY" if req.daily_budget_minor_units else "UNSPECIFIED",
            "daily_budget_minor_units": req.daily_budget_minor_units,
            "currency": "USD",
            "special_ad_category": req.special_ad_category or "NONE",
            "ad_sets": [{
                "ad_set_key": ad_set_key,
                "campaign_key": campaign_key,
                "name": f"{req.campaign_name} Ad Set",
                "targeting_country": req.targeting_country or "",
                "targeting": {},
                "placements": req.target_platforms,
                "optimization_goal": "LINK_CLICKS",
                "schedule": {},
                "ads": [{
                    "ad_key": f"ad-{platform}",
                    "ad_set_key": ad_set_key,
                    "platform": platform,
                    "headline": req.headline,
                    "ad_text": req.ad_text,
                    "destination_url": req.destination_url,
                    "creative_file": req.creative_file or "",
                    "creative_mime_type": req.creative_mime_type,
                    "page_id": req.meta_page_id,
                    "instagram_user_id": req.instagram_user_id,
                } for platform in req.target_platforms],
            }],
        }]}

    campaign_count = 0
    ad_set_count = 0
    ad_count = 0
    creative_count = 0
    hierarchy_ad_ids: list[str] = []
    hierarchy_ad_keys: set[str] = set()
    total_budget = 0
    for campaign in hierarchy["campaigns"]:
        campaign_count += 1
        daily_budget = campaign.get("daily_budget_minor_units")
        lifetime_budget = campaign.get("lifetime_budget_minor_units")
        budget_strategy = campaign.get("budget_strategy", "DAILY")
        if budget_strategy not in {"DAILY", "LIFETIME", "AD_SET_DAILY", "AD_SET_LIFETIME", "UNSPECIFIED"}:
            raise HTTPException(status_code=422, detail=f"Unsupported budget strategy '{budget_strategy}'.")
        if daily_budget and lifetime_budget:
            raise HTTPException(status_code=422, detail="A campaign cannot set both daily and lifetime budgets.")
        if budget_strategy in {"DAILY", "AD_SET_DAILY"} and lifetime_budget:
            raise HTTPException(status_code=422, detail="DAILY budget strategy cannot set a lifetime budget.")
        if budget_strategy in {"LIFETIME", "AD_SET_LIFETIME"} and daily_budget:
            raise HTTPException(status_code=422, detail="LIFETIME budget strategy cannot set a daily budget.")
        if budget_strategy in {"DAILY", "LIFETIME"} and not (daily_budget or lifetime_budget):
            raise HTTPException(status_code=422, detail=f"Campaign '{campaign['campaign_key']}' is missing its campaign-level budget.")
        total_budget += daily_budget or lifetime_budget or 0
        ad_set_keys: set[str] = set()
        for ad_set in campaign["ad_sets"]:
            ad_set_count += 1
            ad_set_daily = ad_set.get("daily_budget_minor_units")
            ad_set_lifetime = ad_set.get("lifetime_budget_minor_units")
            if ad_set_daily and ad_set_lifetime:
                raise HTTPException(status_code=422, detail="An ad set cannot set both daily and lifetime budgets.")
            if budget_strategy == "AD_SET_DAILY" and not ad_set_daily:
                raise HTTPException(status_code=422, detail=f"Ad set '{ad_set['ad_set_key']}' is missing its daily budget.")
            if budget_strategy == "AD_SET_LIFETIME" and not ad_set_lifetime:
                raise HTTPException(status_code=422, detail=f"Ad set '{ad_set['ad_set_key']}' is missing its lifetime budget.")
            if budget_strategy in {"DAILY", "LIFETIME"} and (ad_set_daily or ad_set_lifetime):
                raise HTTPException(status_code=422, detail="Ad-set budgets cannot be supplied with a campaign-level budget strategy.")
            total_budget += ad_set_daily or ad_set_lifetime or 0
            if ad_set["campaign_key"] != campaign["campaign_key"]:
                raise HTTPException(status_code=422, detail="Ad set campaign_key does not match its parent campaign.")
            if ad_set["ad_set_key"] in ad_set_keys:
                raise HTTPException(status_code=422, detail="ad_set_key values must be unique within a campaign.")
            ad_set_keys.add(ad_set["ad_set_key"])
            creative_keys = set()
            for ad in ad_set["ads"]:
                ad_count += 1
                hierarchy_ad_id = ad.get("ad_id")
                if hierarchy_ad_id and hierarchy_ad_id in hierarchy_ad_ids:
                    raise HTTPException(status_code=422, detail="Ad IDs must be unique within an explicit hierarchy.")
                if hierarchy_ad_id:
                    hierarchy_ad_ids.append(hierarchy_ad_id)
                if ad["ad_key"] in hierarchy_ad_keys:
                    raise HTTPException(status_code=422, detail="ad_key values must be unique within an explicit hierarchy.")
                hierarchy_ad_keys.add(ad["ad_key"])
                if ad["ad_set_key"] != ad_set["ad_set_key"]:
                    raise HTTPException(status_code=422, detail="Ad ad_set_key does not match its parent ad set.")
                creative_key = (
                    ad["creative_file"], ad["creative_mime_type"], ad["headline"], ad["ad_text"], ad["destination_url"]
                )
                creative_keys.add(creative_key)
            creative_count += len(creative_keys)
    hierarchy_hash = hashlib.sha256(json.dumps(hierarchy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    target_platforms = sorted({
        ad["platform"]
        for campaign in hierarchy["campaigns"]
        for ad_set in campaign["ad_sets"]
        for ad in ad_set["ads"]
    })
    target_summary = [
        {
            "campaign_key": campaign["campaign_key"],
            "name": campaign["name"],
            "ad_sets": [
                {
                    "ad_set_key": ad_set["ad_set_key"],
                    "name": ad_set["name"],
                    "countries": sorted({
                        str(country).upper()
                        for country in (
                            ad_set.get("targeting", {}).get("geo_locations", {}).get("countries", [])
                            or [ad_set.get("targeting_country", "")]
                        )
                        if country
                    }),
                    "platforms": sorted({
                        ad["platform"]
                        for ad in ad_set["ads"]
                    }),
                }
                for ad_set in campaign["ad_sets"]
            ],
        }
        for campaign in hierarchy["campaigns"]
    ]
    return {
        "hierarchy": hierarchy,
        "hierarchy_hash": hierarchy_hash,
        "preview": {
            "campaign_count": campaign_count,
            "ad_set_count": ad_set_count,
            "creative_count": creative_count,
            "ad_count": ad_count,
            "total_budget_minor_units": total_budget,
            "target_platforms": target_platforms,
            "target_summary": target_summary,
        },
    }


def load_batch_campaigns(connection: Any, batch_id: str, client_id: str) -> list[LaunchCampaignModel] | None:
    """Rebuild grouped campaign models from an uploaded flat batch's row snapshots."""
    rows = connection.execute(
        "SELECT ad_id, hierarchy_data FROM ads WHERE batch_id=%s AND client_id=%s ORDER BY row_number",
        (batch_id, client_id),
    ).fetchall()
    if not rows or not any(row[1] is not None for row in rows):
        return None
    if any(row[1] is None for row in rows):
        raise HTTPException(status_code=422, detail="Hierarchy data must be present on every ad row in this batch.")

    contract_rows: list[BatchRowContract] = []
    for ad_id, raw_hierarchy in rows:
        hierarchy_data = raw_hierarchy if isinstance(raw_hierarchy, dict) else json.loads(raw_hierarchy)
        contract_rows.append(BatchRowContract.model_validate({**hierarchy_data, "ad_id": str(ad_id)}))
    validated_rows = validate_batch_row_consistency([row.model_dump() for row in contract_rows])

    campaigns: dict[str, dict[str, Any]] = {}
    ad_sets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in validated_rows:
        campaign = campaigns.setdefault(row.campaign_key, {
            "campaign_key": row.campaign_key,
            "name": row.campaign_name,
            "objective": row.campaign_objective,
            "budget_strategy": row.campaign_budget_strategy,
            "daily_budget_minor_units": row.campaign_budget_amount if row.campaign_budget_strategy == "DAILY" else None,
            "lifetime_budget_minor_units": row.campaign_budget_amount if row.campaign_budget_strategy == "LIFETIME" else None,
            "currency": "USD",
            "special_ad_category": row.campaign_special_category,
            "ad_sets": [],
        })
        ad_set_key = (row.campaign_key, row.ad_set_key)
        ad_set = ad_sets.get(ad_set_key)
        if ad_set is None:
            countries = row.targeting_spec.get("geo_locations", {}).get("countries", [])
            ad_set = {
                "ad_set_key": row.ad_set_key,
                "campaign_key": row.campaign_key,
                "name": row.ad_set_name,
                "targeting_country": countries[0] if countries else "",
                "targeting": row.targeting_spec,
                "placements": row.placements,
                "optimization_goal": row.optimization_goal,
                "billing_event": row.billing_event,
                "schedule": row.schedule,
                "daily_budget_minor_units": row.ad_set_budget_amount if row.campaign_budget_strategy == "AD_SET_DAILY" else None,
                "lifetime_budget_minor_units": row.ad_set_budget_amount if row.campaign_budget_strategy == "AD_SET_LIFETIME" else None,
                "ads": [],
            }
            ad_sets[ad_set_key] = ad_set
            campaign["ad_sets"].append(ad_set)
        ad_set["ads"].append({
            "ad_key": row.ad_key,
            "ad_id": row.ad_id,
            "ad_set_key": row.ad_set_key,
            "platform": normalize_platform(row.platform).canonical_value or row.platform,
            "headline": row.headline,
            "ad_text": row.body_copy,
            "destination_url": row.destination_url,
            "creative_file": row.creative_url,
            "creative_mime_type": row.creative_mime_type,
            "page_id": row.page_id,
            "instagram_user_id": row.instagram_actor_id,
        })

    return [LaunchCampaignModel.model_validate(campaign) for campaign in campaigns.values()]


def campaign_platforms(campaign: LaunchCampaignModel | dict[str, Any]) -> list[str]:
    """Return canonical platform targets actually used by a campaign's ads."""
    ad_sets = campaign.ad_sets if isinstance(campaign, LaunchCampaignModel) else campaign.get("ad_sets", [])
    platforms = []
    for ad_set in ad_sets:
        ads = ad_set.ads if isinstance(ad_set, LaunchAdSetModel) else ad_set.get("ads", [])
        for ad in ads:
            platform = ad.platform if isinstance(ad, LaunchAdModel) else ad.get("platform", "")
            normalized = normalize_platform(str(platform))
            if normalized.canonical_value in {"facebook", "instagram"} and normalized.canonical_value not in platforms:
                platforms.append(normalized.canonical_value)
    return platforms


def order_ads_by_hierarchy(
    campaigns: list[dict[str, Any]],
    ad_rows: list[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    """Validate exact hierarchy-to-upload coverage and return rows in tree order."""
    rows_by_id = {str(row[0]): row for row in ad_rows}
    ordered_rows: list[tuple[Any, ...]] = []
    seen_ids: set[str] = set()
    for campaign in campaigns:
        for ad_set in campaign["ad_sets"]:
            for ad in ad_set["ads"]:
                ad_id = str(ad.get("ad_id") or "")
                if not ad_id or ad_id not in rows_by_id:
                    raise HTTPException(status_code=422, detail=f"Hierarchy ad '{ad.get('ad_key')}' does not match an uploaded ad row.")
                if ad_id in seen_ids:
                    raise HTTPException(status_code=422, detail=f"Uploaded ad '{ad_id}' appears more than once in the hierarchy.")
                if ad["platform"] != rows_by_id[ad_id][6]:
                    raise HTTPException(status_code=422, detail=f"Hierarchy platform does not match uploaded ad '{ad_id}'.")
                persisted_row = rows_by_id[ad_id]
                if (
                    ad.get("headline") != (persisted_row[1] or "")
                    or ad.get("ad_text") != (persisted_row[2] or "")
                    or ad.get("destination_url") != (persisted_row[3] or "")
                    or ad.get("creative_file") != (persisted_row[5] or "")
                ):
                    raise HTTPException(status_code=409, detail=f"Hierarchy content or creative does not match uploaded ad '{ad_id}'. Re-upload or recheck the batch.")
                seen_ids.add(ad_id)
                ordered_rows.append(rows_by_id[ad_id])
    if seen_ids != set(rows_by_id):
        raise HTTPException(status_code=422, detail="Every uploaded ad row must appear exactly once in the approved hierarchy.")
    return ordered_rows


# --- Endpoints ---

def resolve_campaign_client(user: CurrentUser, client_id: str = "", batch_id: str | None = None,
                            destination_url: str = "", input_text: str = "") -> str:
    """Resolve only an authorized real client; never guess another client's campaign."""
    if user.guest_session_id:
        if client_id and client_id not in user.assigned_client_ids:
            raise HTTPException(status_code=404, detail="client not found")
        if batch_id:
            with get_session() as conn:
                owned = conn.execute("SELECT 1 FROM batches WHERE batch_id=%s AND guest_session_id=%s", (batch_id, user.guest_session_id)).fetchone()
            if not owned:
                raise HTTPException(status_code=404, detail="batch not found")
        return user.assigned_client_ids[0]
    if client_id.strip():
        require_client_access(user, client_id.strip())
        return client_id.strip()
    allowed = list(user.assigned_client_ids)
    if batch_id:
        with get_session() as conn:
            rows = conn.execute("SELECT DISTINCT client_id FROM ads WHERE batch_id = %s AND client_id = ANY(%s)", (batch_id, allowed)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        if len(rows) > 1:
            raise HTTPException(status_code=422, detail="This sheet contains multiple clients. Upload a separate sheet for each client's campaign.")
    if destination_url:
        from apps.api.client_inference import infer_client_id, load_domain_map
        inferred = infer_client_id(destination_url, load_domain_map())
        if inferred in allowed:
            return inferred
    if len(allowed) == 1:
        return allowed[0]
    if allowed and input_text.strip():
        import re
        with get_session() as conn:
            rows = conn.execute("SELECT client_id, name FROM clients WHERE client_id = ANY(%s)", (allowed,)).fetchall()
        matches = {cid for cid, name in rows if any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", input_text, re.I) for term in (cid, name))}
        if len(matches) == 1:
            return matches.pop()
    detail = "Your account has no assigned campaign client. An account manager needs to assign your real client before launch." if not allowed else "Include the client's name in your campaign prompt or its landing-page URL so we can identify the campaign automatically."
    raise HTTPException(status_code=422, detail=detail)


@router.post("/hierarchy-preview")
@endpoint_errors
def hierarchy_preview(req: LaunchBatchRequest, request: Request, user: CurrentUser = Depends(get_request_user)) -> dict[str, Any]:
    """Read-only preview of the campaign hierarchy; no Meta objects are created."""
    if user.guest_session_id:
        require_guest_csrf(request)
    if req.batch_id:
        req.client_id = resolve_campaign_client(user, req.client_id, req.batch_id)
        with get_session() as conn:
            stored_campaigns = load_batch_campaigns(conn, req.batch_id, req.client_id)
        if stored_campaigns is not None:
            req.campaigns = stored_campaigns
            req.target_platforms = list(dict.fromkeys(
                ad["platform"]
                for campaign in stored_campaigns
                for ad_set in campaign.ad_sets
                for ad in ad_set.ads
            )) or req.target_platforms
    result = build_launch_hierarchy(req)
    preview = result["preview"]
    if req.model_dump().get("meta_launch"):
        if user.guest_session_id:
            raise HTTPException(status_code=403, detail="Meta campaign previews require an authenticated approver.")
        with get_session() as conn:
            from apps.api.clients import resolve_client_meta_account
            try:
                preview["meta_ad_account_id"] = resolve_client_meta_account(conn, req.client_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"preview": preview, "hierarchy_hash": result["hierarchy_hash"]}


@router.post("/resolve-client")
@endpoint_errors
def resolve_launch_client(req: PreflightReviewRequest, request: Request, user: CurrentUser = Depends(get_request_user)):
    if user.guest_session_id:
        require_guest_csrf(request)
    return {"client_id": resolve_campaign_client(user, req.client_id, req.batch_id,
            str(req.ad_overrides.get("destination_url") or ""), req.input_text)}

@router.post("/preflight-review")
@endpoint_errors
def preflight_review(
    req: PreflightReviewRequest,
    request: Request,
    user: CurrentUser = Depends(get_request_user),
    background: bool = False,
) -> dict[str, Any]:
    """Review uploaded resources, check copy, creative specs, and verify Meta credentials."""
    init_flow_tables()
    if user.guest_session_id:
        require_guest_csrf(request)
    overrides = req.ad_overrides or {}
    if overrides.get("meta_launch") and user.guest_session_id:
        raise HTTPException(status_code=403, detail="Meta campaign previews require an authenticated approver.")
    client_id = resolve_campaign_client(user, req.client_id, req.batch_id,
                 str(req.ad_overrides.get("destination_url") or ""), req.input_text)
    stored_hierarchy = None
    if req.batch_id:
        with get_session() as conn:
            stored_hierarchy = load_batch_campaigns(conn, req.batch_id, client_id)

    if overrides.get("meta_launch") and stored_hierarchy is None:
        missing_meta = []
        for field, label in (("meta_page_id", "Facebook Page ID"), ("targeting_country", "Targeting country"),
                             ("daily_budget_minor_units", "Daily budget"), ("special_ad_category", "Special ad category")):
            if not overrides.get(field):
                missing_meta.append({"field": field, "label": label, "reason": "Required to build the Meta campaign."})
        if not str(overrides.get("targeting_country") or "").strip().isalpha() or len(str(overrides.get("targeting_country") or "").strip()) != 2:
            missing_meta.append({"field": "targeting_country", "label": "Targeting country", "reason": "Enter a two-letter country code."})
        if "instagram" in (overrides.get("target_platforms") or []) and not overrides.get("instagram_user_id"):
            missing_meta.append({"field": "instagram_user_id", "label": "Instagram account ID", "reason": "Required when Instagram is selected."})
        if overrides.get("special_ad_category") not in {"NONE", "HOUSING", "EMPLOYMENT", "FINANCIAL_PRODUCTS_SERVICES", "ISSUES_ELECTIONS_POLITICS"}:
            missing_meta.append({"field": "special_ad_category", "label": "Special ad category", "reason": "Choose None or the applicable Meta special ad category."})
        creative_url = str(overrides.get("creative_file") or "")
        if not creative_url.startswith("https://"):
            missing_meta.append({"field": "creative_file", "label": "Public creative image URL", "reason": "Meta needs an uploaded, publicly accessible HTTPS image."})
        if overrides.get("creative_mime_type") not in {"image/jpeg", "image/png"}:
            missing_meta.append({"field": "creative_mime_type", "label": "Creative image format", "reason": "The current Meta create chain requires a PNG or JPEG image."})
        if missing_meta:
            return {"status": "missing_requirements", "client_id": client_id,
                    "missing_items": missing_meta, "missing_count": len(missing_meta)}
    elif overrides.get("meta_launch") and stored_hierarchy is not None:
        valid_objectives = {
            "OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT",
            "OUTCOME_LEADS", "OUTCOME_SALES", "OUTCOME_APP_PROMOTION",
        }
        missing_meta = []
        for campaign in stored_hierarchy:
            if campaign.objective not in valid_objectives:
                missing_meta.append({"field": "campaign_objective", "label": campaign.campaign_key, "reason": "Unsupported Meta campaign objective."})
            for ad_set in campaign.ad_sets:
                country_codes = ad_set.targeting.get("geo_locations", {}).get("countries", [])
                if not country_codes or any(len(str(code).strip()) != 2 or not str(code).strip().isalpha() for code in country_codes):
                    missing_meta.append({"field": "targeting_spec", "label": ad_set.ad_set_key, "reason": "Specify one or more two-letter countries for this launch integration."})
                for ad in ad_set.ads:
                    if not ad.page_id or not ad.creative_file.startswith("https://"):
                        missing_meta.append({"field": "creative_or_page", "label": ad.ad_key, "reason": "Each ad requires a Page ID and public HTTPS creative."})
                    if ad.creative_mime_type not in {"image/jpeg", "image/png"}:
                        missing_meta.append({"field": "creative_mime_type", "label": ad.ad_key, "reason": "Each creative must declare PNG or JPEG MIME type."})
                    if ad.platform == "instagram" and not ad.instagram_user_id:
                        missing_meta.append({"field": "instagram_actor_id", "label": ad.ad_key, "reason": "Instagram ads require an Instagram actor ID."})
        if missing_meta:
            return {"status": "missing_requirements", "client_id": client_id, "missing_items": missing_meta, "missing_count": len(missing_meta)}

    if background:
        from apps.api.review_jobs import submit
        fingerprint = req.model_dump()
        # Invalidate cached checks when persisted ad content changes.
        if req.batch_id:
            with get_session() as conn:
                fingerprint["content_versions"] = conn.execute(
                    "SELECT ad_id, row_hash FROM ads WHERE batch_id=%s AND client_id=%s ORDER BY ad_id",
                    (req.batch_id, client_id)).fetchall()
        return submit(user.user_id, fingerprint, lambda: preflight_review(req, request=request, user=user, background=False))

    raw_text = (req.input_text or "").strip()
    overrides = req.ad_overrides or {}

    if req.batch_id:
        from packages.contracts import AdContract
        from apps.api.preflight import review_ad
        with get_session() as conn:
            rows = conn.execute("SELECT ad_id, platform, headline, ad_text, destination_url, creative_file, row_hash, sheet_hash, row_number FROM ads WHERE batch_id = %s AND client_id = %s", (req.batch_id, client_id)).fetchall()
            grouped_campaigns = stored_hierarchy
        if not rows:
            raise HTTPException(status_code=422, detail="No ads for this client in the uploaded batch.")
        checks = []
        for row in rows:
            ad = AdContract(ad_id=row[0], batch_id=req.batch_id, client_id=client_id, platform=row[1], headline=row[2], ad_text=row[3], destination_url=row[4], creative_file=row[5] or None, row_hash=row[6], sheet_hash=row[7], row_number=row[8])
            checks.extend({**check, "ad_id": ad.ad_id, "platform": ad.platform} for check in review_ad(ad))
        from apps.api.preflight import summarize_preflight_checks
        verdict = summarize_preflight_checks(checks)
        hierarchy_result: dict[str, Any] = {}
        if grouped_campaigns is not None:
            platforms = list(dict.fromkeys(
                ad.platform
                for campaign in grouped_campaigns
                for ad_set in campaign.ad_sets
                for ad in ad_set.ads
            ))
            launch_preview_request = LaunchBatchRequest(
                client_id=client_id,
                batch_id=req.batch_id,
                employee_email="preflight@launch-gate.invalid",
                target_platforms=platforms,
                campaigns=grouped_campaigns,
            )
            hierarchy_result = build_launch_hierarchy(launch_preview_request)
        elif overrides.get("meta_launch"):
            platforms = overrides.get("target_platforms") or list(dict.fromkeys(row[1] for row in rows))
            launch_preview_request = LaunchBatchRequest(
                client_id=client_id,
                batch_id=req.batch_id,
                campaign_name=str(overrides.get("campaign_name") or "Campaign"),
                employee_email="preflight@launch-gate.invalid",
                target_platforms=platforms,
                headline=str(overrides.get("headline") or ""),
                ad_text=str(overrides.get("ad_text") or ""),
                destination_url=str(overrides.get("destination_url") or ""),
                creative_file=str(overrides.get("creative_file") or ""),
                creative_mime_type=overrides.get("creative_mime_type"),
                meta_page_id=overrides.get("meta_page_id"),
                instagram_user_id=overrides.get("instagram_user_id"),
                targeting_country=overrides.get("targeting_country"),
                daily_budget_minor_units=overrides.get("daily_budget_minor_units"),
                special_ad_category=overrides.get("special_ad_category"),
                meta_launch=True,
            )
            hierarchy_result = build_launch_hierarchy(launch_preview_request)
        if overrides.get("meta_launch") and hierarchy_result:
            with get_session() as conn:
                from apps.api.clients import resolve_client_meta_account
                try:
                    hierarchy_result["preview"]["meta_ad_account_id"] = resolve_client_meta_account(conn, client_id)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            **verdict,
            "checks": checks,
            "missing_items": [],
            "campaign_summary": {},
            "client_id": client_id,
            **({
                "hierarchy_preview": hierarchy_result["preview"],
                "hierarchy_hash": hierarchy_result["hierarchy_hash"],
            } if hierarchy_result else {}),
        }

    headline = str(overrides.get("headline") or (raw_text.splitlines()[0] if raw_text else "")).strip()
    ad_text = str(overrides.get("ad_text") or raw_text).strip()
    dest_url = str(overrides.get("destination_url") or "").strip()
    missing = []
    for field, value in (("headline", headline), ("ad_text", ad_text), ("destination_url", dest_url)):
        if not value or (field == "destination_url" and not value.startswith(("http://", "https://"))):
            missing.append({"field": field, "label": field.replace("_", " ").title(),
                            "type": "url" if field == "destination_url" else "text",
                            "reason": "Required for a real campaign.", "placeholder": ""})
    if missing:
        return {"status": "missing_requirements", "missing_items": missing,
                "missing_count": len(missing), "client_id": client_id}
    from packages.contracts import AdContract
    from apps.api.preflight import review_ad
    platforms = overrides.get("target_platforms") or ["facebook", "instagram"]
    checks = []
    for platform in platforms:
        ad = AdContract(ad_id="preflight", batch_id="preflight", client_id=client_id,
                        row_number=1, platform=platform, headline=headline, ad_text=ad_text,
                        destination_url=dest_url, creative_file=overrides.get("creative_file") or None,
                        row_hash="preflight", sheet_hash="preflight")
        checks.extend({**check, "platform": platform} for check in review_ad(ad))
    from apps.api.preflight import summarize_preflight_checks
    verdict = summarize_preflight_checks(checks)
    hierarchy_result = None
    if overrides.get("meta_launch"):
        preview_request = LaunchBatchRequest(
            client_id=client_id,
            campaign_name=str(overrides.get("campaign_name") or "Campaign"),
            employee_email="preflight@launch-gate.invalid",
            target_platforms=platforms,
            headline=headline,
            ad_text=ad_text,
            destination_url=dest_url,
            creative_file=str(overrides.get("creative_file") or ""),
            creative_mime_type=overrides.get("creative_mime_type"),
            meta_page_id=overrides.get("meta_page_id"),
            instagram_user_id=overrides.get("instagram_user_id"),
            targeting_country=overrides.get("targeting_country"),
            daily_budget_minor_units=overrides.get("daily_budget_minor_units"),
            special_ad_category=overrides.get("special_ad_category"),
            meta_launch=True,
        )
        hierarchy_result = build_launch_hierarchy(preview_request)
        with get_session() as conn:
            from apps.api.clients import resolve_client_meta_account
            try:
                hierarchy_result["preview"]["meta_ad_account_id"] = resolve_client_meta_account(conn, client_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**verdict, "client_id": client_id, "checks": checks, "missing_items": [],
        "campaign_summary": {"headline": headline, "ad_text": ad_text,
                             "destination_url": dest_url, "creative_file": overrides.get("creative_file")},
        **({
            "hierarchy_preview": hierarchy_result["preview"],
            "hierarchy_hash": hierarchy_result["hierarchy_hash"],
        } if hierarchy_result else {}),
        "message": verdict["message"]}


@router.post("/hierarchy/retry")
@endpoint_errors
def retry_hierarchy_entity(req: RetryHierarchyEntityRequest, user: CurrentUser = Depends(require_role("buyer", "account_manager"))) -> dict[str, Any]:
    """Retry one failed campaign or ad set without recreating successful parents."""
    if req.entity_type not in {"campaign", "ad_set"}:
        raise HTTPException(status_code=422, detail="entity_type must be campaign or ad_set.")
    table = "meta_campaign_runs" if req.entity_type == "campaign" else "meta_ad_set_runs"
    id_column = "meta_id"
    with get_session() as conn:
        with conn.cursor() as cur:
            if req.entity_type == "campaign":
                cur.execute("SELECT id, workspace_id, meta_id, status FROM meta_campaign_runs WHERE batch_id=%s AND (grouping_key=%s OR id::text=%s)", (req.batch_id, req.entity_key, req.entity_key))
            else:
                cur.execute("""SELECT ad_set.id, campaign.workspace_id, ad_set.meta_id, ad_set.status
                             FROM meta_ad_set_runs ad_set JOIN meta_campaign_runs campaign ON campaign.id=ad_set.campaign_run_id
                             WHERE campaign.batch_id=%s AND (ad_set.grouping_key=%s OR ad_set.id::text=%s)""",
                            (req.batch_id, req.entity_key, req.entity_key))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Hierarchy entity was not found in this batch.")
            if str(row[1]) not in set(user.assigned_client_ids):
                raise HTTPException(status_code=403, detail="Client is not assigned to this user.")
            run_id, client_id, meta_id, run_status = row[0], row[1], row[2], row[3]
            if meta_id and run_status == "CREATED":
                return {"status": "not_required", "entity_key": req.entity_key, "meta_object_id": meta_id}
            cur.execute(f"UPDATE {table} SET status='PENDING', last_error=NULL, updated_at=NOW() WHERE id=%s", (run_id,))
            if req.entity_type == "campaign":
                cur.execute("UPDATE meta_launch_campaigns SET lifecycle_status='pending', last_error=NULL, updated_at=NOW() WHERE batch_id=%s AND client_id=%s AND grouping_key=%s", (req.batch_id, client_id, req.entity_key))
            else:
                cur.execute("UPDATE meta_launch_ad_sets SET lifecycle_status='pending', last_error=NULL, updated_at=NOW() WHERE batch_id=%s AND client_id=%s AND grouping_key=%s", (req.batch_id, client_id, req.entity_key))
        conn.commit()
    return {"status": "queued", "entity_key": req.entity_key, "entity_type": req.entity_type}


@router.get("/hierarchy-status/{batch_id}")
@endpoint_errors
def get_hierarchy_status(batch_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """Read hierarchy progress and aggregate planned/saved object counts from persisted IDs."""
    with get_session() as conn:
        with conn.cursor() as cur:
            allowed_clients = list(user.assigned_client_ids)
            cur.execute(
                """SELECT ad_set.id::text, ad_set.campaign_run_id::text,
                          campaign.grouping_key, ad_set.grouping_key,
                          ad_set.meta_id, ad_set.status, ad_set.last_error, campaign.workspace_id
                   FROM meta_ad_set_runs ad_set
                   JOIN meta_campaign_runs campaign ON campaign.id=ad_set.campaign_run_id
                   WHERE campaign.batch_id=%s AND campaign.workspace_id=ANY(%s)""",
                (batch_id, allowed_clients),
            )
            ad_sets = cur.fetchall()
            cur.execute(
                """SELECT run.id::text, run.grouping_key, run.platform, run.status, run.meta_id,
                          run.last_error, run.workspace_id, COALESCE(legacy.budget_minor_units, 0)
                   FROM meta_campaign_runs run
                   LEFT JOIN meta_launch_campaigns legacy
                     ON legacy.batch_id=run.batch_id AND legacy.client_id=run.workspace_id
                    AND legacy.grouping_key=run.grouping_key
                   WHERE run.batch_id=%s AND run.workspace_id=ANY(%s)""",
                (batch_id, allowed_clients),
            )
            campaigns = cur.fetchall()
            if not campaigns:
                raise HTTPException(status_code=404, detail="No hierarchy runs found for this authorized batch.")
            cur.execute("SELECT COUNT(*), COUNT(platform_ad_id) FROM meta_launch_objects o JOIN ads a ON a.ad_id=o.ad_id WHERE a.batch_id=%s", (batch_id,))
            ad_counts = cur.fetchone()
    progress = build_hierarchy_progress(campaigns, ad_sets)
    return {
        "batch_id": batch_id,
        **progress,
        "metrics": {
            "campaign_count": len(campaigns),
            "ad_set_count": len(ad_sets),
            "ad_count": int(ad_counts[0] or 0),
            "created_ad_count": int(ad_counts[1] or 0),
            "planned_budget_minor_units": sum(int(row[4] or 0) for row in campaigns),
        },
    }


def build_hierarchy_progress(
    campaigns: list[tuple[Any, ...]],
    ad_sets: list[tuple[Any, ...]],
) -> dict[str, Any]:
    """Build the client-facing nested hierarchy and aggregate successful progress."""
    campaigns_by_id: dict[str, dict[str, Any]] = {}
    campaign_payloads = []
    for row in campaigns:
        status_value = entity_status_for_run(row[3]).value
        campaign = {
            "run_id": row[0],
            "grouping_key": row[1],
            "platform": row[2],
            "status": status_value,
            "meta_object_id": row[4],
            "last_error": row[5],
            "workspace_id": row[6],
            "budget_minor_units": row[7],
            "ad_sets": [],
        }
        campaigns_by_id[row[0]] = campaign
        campaign_payloads.append(campaign)

    ad_set_payloads = []
    for row in ad_sets:
        status_value = entity_status_for_run(row[5]).value
        ad_set = {
            "run_id": row[0],
            "campaign_run_id": row[1],
            "campaign_key": row[2],
            "grouping_key": row[3],
            "meta_object_id": row[4],
            "status": status_value,
            "last_error": row[6],
            "workspace_id": row[7],
        }
        parent = campaigns_by_id.get(row[1])
        if parent is not None:
            parent["ad_sets"].append(ad_set)
        ad_set_payloads.append(ad_set)

    successful_statuses = {MetaEntityStatus.CREATED_PAUSED.value, MetaEntityStatus.ACTIVE.value}
    entity_count = len(campaign_payloads) + len(ad_set_payloads)
    successful_count = sum(
        item["status"] in successful_statuses
        for item in campaign_payloads + ad_set_payloads
    )
    aggregate_completion = int(successful_count * 100 / entity_count) if entity_count else 0
    return {
        "aggregate_completion_percentage": aggregate_completion,
        "summary": {
            "total_campaign_groups": len(campaign_payloads),
            "completed_campaign_groups": sum(item["status"] in successful_statuses for item in campaign_payloads),
            "total_ad_sets": len(ad_set_payloads),
            "completed_ad_sets": sum(item["status"] in successful_statuses for item in ad_set_payloads),
        },
        "structure": campaign_payloads,
        "campaigns": [
            {
                "run_id": row[0],
                "grouping_key": row[1],
                "platform": row[2],
                "status": row[3],
                "meta_object_id": row[4],
                "last_error": row[5],
                "workspace_id": row[6],
                "budget_minor_units": row[7],
            }
            for row in campaigns
        ],
        "ad_sets": [
            {
                "run_id": row[0],
                "campaign_key": row[2],
                "grouping_key": row[3],
                "meta_object_id": row[4],
                "status": row[5],
                "last_error": row[6],
                "workspace_id": row[7],
            }
            for row in ad_sets
        ],
    }


def validate_activation_campaigns(campaigns: list[tuple[Any, ...]]) -> None:
    if not campaigns:
        raise HTTPException(status_code=409, detail="No paused campaigns matched the approved hierarchy snapshot.")
    invalid_campaigns = [
        str(row[0])
        for row in campaigns
        if not row[2] or row[5] != "created_paused"
    ]
    if invalid_campaigns:
        raise HTTPException(
            status_code=409,
            detail=f"Only created_paused campaigns with Meta IDs can be activated: {', '.join(invalid_campaigns)}.",
        )


def meta_activation_confirmed_active(result: dict[str, Any]) -> bool:
    return str(result.get("status") or "").casefold() == "active"


@router.post("/activate-hierarchy", status_code=status.HTTP_200_OK)
@endpoint_errors
def activate_hierarchy(req: ActivateHierarchyRequest, request: Request, user: CurrentUser = Depends(require_role("account_manager", "buyer"))) -> dict[str, Any]:
    """Explicitly activate previously created paused Meta objects after human confirmation."""
    if user.guest_session_id:
        require_guest_csrf(request)
    from apps.worker.mcp_client import meta_live_writes_enabled
    if not meta_live_writes_enabled():
        raise HTTPException(status_code=409, detail="Meta live activation requires META_MCP_ENABLE_LIVE=true and META_MCP_ALLOW_WRITES=true.")

    mcp_dispatcher = MCPPlatformDispatcher(
        access_token=os.environ.get("META_SYSTEM_USER_TOKEN", "").strip()
    )
    activated_campaigns: list[str] = []
    with get_session() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT client_id FROM ads WHERE batch_id=%s", (req.batch_id,))
            client_rows = cur.fetchall()
            if len(client_rows) != 1:
                raise HTTPException(status_code=422, detail="Activation batch must resolve to exactly one authorized client.")
            batch_client_id = str(client_rows[0][0])
            if batch_client_id not in set(user.assigned_client_ids):
                raise HTTPException(status_code=403, detail="Client is not assigned to this user.")
            from apps.api.clients import resolve_client_meta_account
            try:
                meta_ad_account_id = resolve_client_meta_account(conn, batch_client_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            cur.execute(
                """SELECT grouping_key, platform, meta_campaign_id, hierarchy_hash,
                          budget_minor_units, lifecycle_status
                   FROM meta_launch_campaigns
                   WHERE batch_id=%s AND client_id=%s AND hierarchy_hash=%s
                   ORDER BY grouping_key""",
                (req.batch_id, batch_client_id, req.hierarchy_hash),
            )
            campaigns = cur.fetchall()
            if req.campaign_keys:
                campaigns = [row for row in campaigns if row[0] in set(req.campaign_keys)]
            validate_activation_campaigns(campaigns)
            planned_budget = sum(int(row[4]) for row in campaigns)
            if req.confirmed_budget_minor_units != planned_budget:
                raise HTTPException(status_code=409, detail=f"Confirmed budget {req.confirmed_budget_minor_units} does not match planned budget {planned_budget}.")
            needs_reconciliation: list[str] = []
            activation_errors: dict[str, str] = {}
            activated_ad_sets: list[tuple[str, str]] = []
            reconciliation_ad_sets: list[str] = []
            for grouping_key, platform, meta_campaign_id, _stored_hash, _budget, _lifecycle_status in campaigns:
                cur.execute(
                    """SELECT ad_set.grouping_key, ad_set.meta_id
                       FROM meta_ad_set_runs ad_set
                       JOIN meta_campaign_runs campaign ON campaign.id=ad_set.campaign_run_id
                       WHERE campaign.batch_id=%s AND campaign.workspace_id=%s
                         AND campaign.grouping_key=%s
                       ORDER BY ad_set.grouping_key""",
                    (req.batch_id, batch_client_id, grouping_key),
                )
                ad_set_rows = cur.fetchall()
                if any(not row[1] for row in ad_set_rows):
                    raise HTTPException(status_code=409, detail=f"Campaign {grouping_key} has an ad set without a confirmed Meta ID.")
                ad_set_runs = [(str(row[0]), str(row[1])) for row in ad_set_rows]
                ad_set_ids = [meta_id for _key, meta_id in ad_set_runs]
                cur.execute(
                    """SELECT DISTINCT objects.platform_ad_id
                       FROM meta_launch_objects objects
                       JOIN ads ON ads.ad_id=objects.ad_id
                       WHERE ads.batch_id=%s AND ads.client_id=%s
                         AND objects.campaign_id=%s
                       ORDER BY objects.platform_ad_id""",
                    (req.batch_id, batch_client_id, meta_campaign_id),
                )
                ad_rows = cur.fetchall()
                if any(not row[0] for row in ad_rows):
                    raise HTTPException(status_code=409, detail=f"Campaign {grouping_key} has an ad without a confirmed Meta ID.")
                ad_ids = [str(row[0]) for row in ad_rows]
                cur.execute(
                    """INSERT INTO meta_launch_activation_audit
                       (activation_id, batch_id, hierarchy_hash, campaign_key, platform, meta_campaign_id, approved_by, confirmed_budget_minor_units)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (batch_id, hierarchy_hash, campaign_key, platform) DO UPDATE SET
                       meta_campaign_id=EXCLUDED.meta_campaign_id, approved_by=EXCLUDED.approved_by,
                       confirmed_budget_minor_units=EXCLUDED.confirmed_budget_minor_units""",
                    (f"activation_{uuid.uuid4().hex}", req.batch_id, req.hierarchy_hash, grouping_key, platform,
                     meta_campaign_id, user.user_id, req.confirmed_budget_minor_units),
                )
                conn.commit()
                activation_targets = [
                    (meta_campaign_id, "campaign"),
                    *((ad_set_id, "ad_set") for ad_set_id in ad_set_ids),
                    *((ad_id, "ad") for ad_id in ad_ids),
                ]
                activation_error = None
                for entity_id, entity_type in activation_targets:
                    response = mcp_dispatcher.call_tool(platform, "ads_activate_entity", {
                        "ad_account_id": meta_ad_account_id,
                        "entity_id": entity_id,
                        "entity_type": entity_type,
                        "object_ids": [entity_id],
                        "publish_as_active": True,
                    })
                    if not response.success:
                        activation_error = response.error or f"Meta {entity_type} activation failed."
                        break
                    result = response.result or {}
                    if not meta_activation_confirmed_active(result):
                        activation_error = (
                            f"Meta did not confirm {entity_type} {entity_id} as active; "
                            "status requires reconciliation."
                        )
                        break

                if activation_error is None:
                    cur.execute(
                        """UPDATE meta_launch_campaigns
                           SET lifecycle_status='active', updated_at=NOW(), last_error=NULL
                           WHERE batch_id=%s AND client_id=%s AND grouping_key=%s
                             AND hierarchy_hash=%s AND lifecycle_status='created_paused'""",
                        (req.batch_id, batch_client_id, grouping_key, req.hierarchy_hash),
                    )
                    cur.execute(
                        """UPDATE meta_campaign_runs SET status='ACTIVE', updated_at=NOW(), last_error=NULL
                           WHERE batch_id=%s AND grouping_key=%s AND workspace_id=%s""",
                        (req.batch_id, grouping_key, batch_client_id),
                    )
                    cur.execute(
                        """UPDATE meta_ad_set_runs SET status='ACTIVE', updated_at=NOW(), last_error=NULL
                           WHERE campaign_run_id IN (
                               SELECT id FROM meta_campaign_runs
                               WHERE batch_id=%s AND grouping_key=%s AND workspace_id=%s
                           ) AND meta_id=ANY(%s)""",
                        (req.batch_id, grouping_key, batch_client_id, ad_set_ids),
                    )
                    cur.execute(
                        """UPDATE meta_launch_ad_sets SET lifecycle_status='active',
                           updated_at=NOW(), last_error=NULL
                           WHERE batch_id=%s AND client_id=%s AND meta_ad_set_id=ANY(%s)""",
                        (req.batch_id, batch_client_id, ad_set_ids),
                    )
                    activated_ad_sets.extend(ad_set_runs)
                    activated_campaigns.append(grouping_key)
                else:
                    needs_reconciliation.append(grouping_key)
                    activation_errors[grouping_key] = activation_error
                    reconciliation_ad_sets.extend(key for key, _meta_id in ad_set_runs)
                    cur.execute(
                        """UPDATE meta_launch_campaigns SET lifecycle_status='reconciling',
                           last_error=%s, updated_at=NOW()
                           WHERE batch_id=%s AND client_id=%s AND grouping_key=%s
                             AND hierarchy_hash=%s""",
                        (activation_error[:500], req.batch_id, batch_client_id, grouping_key, req.hierarchy_hash),
                    )
                    cur.execute(
                        """UPDATE meta_campaign_runs SET status='RECONCILING',
                           last_error=%s,
                           updated_at=NOW()
                           WHERE batch_id=%s AND grouping_key=%s AND workspace_id=%s""",
                        (activation_error[:500], req.batch_id, grouping_key, batch_client_id),
                    )
                    cur.execute(
                        """UPDATE meta_ad_set_runs SET status='RECONCILING',
                           last_error=%s, updated_at=NOW()
                           WHERE campaign_run_id IN (
                               SELECT id FROM meta_campaign_runs
                               WHERE batch_id=%s AND grouping_key=%s AND workspace_id=%s
                           )""",
                        (activation_error[:500], req.batch_id, grouping_key, batch_client_id),
                    )
                    cur.execute(
                        """UPDATE meta_launch_ad_sets SET lifecycle_status='reconciling',
                           last_error=%s, updated_at=NOW()
                           WHERE batch_id=%s AND client_id=%s AND campaign_entity_key IN (
                               SELECT entity_key FROM meta_launch_campaigns
                               WHERE batch_id=%s AND client_id=%s AND grouping_key=%s
                           )""",
                        (activation_error[:500], req.batch_id, batch_client_id,
                         req.batch_id, batch_client_id, grouping_key),
                    )
                conn.commit()
        conn.commit()
    for campaign_key in activated_campaigns:
        emit_progress_event(
            req.batch_id, "CAMPAIGN", campaign_key, MetaEntityStatus.ACTIVE,
            client_id=batch_client_id,
        )
    for ad_set_key, _meta_id in activated_ad_sets:
        emit_progress_event(
            req.batch_id, "AD_SET", ad_set_key, MetaEntityStatus.ACTIVE,
            client_id=batch_client_id,
        )
    for campaign_key in needs_reconciliation:
        emit_progress_event(
            req.batch_id,
            "CAMPAIGN",
            campaign_key,
            MetaEntityStatus.NEEDS_RECONCILIATION,
            activation_errors[campaign_key],
            client_id=batch_client_id,
        )
    for ad_set_key in reconciliation_ad_sets:
        emit_progress_event(
            req.batch_id,
            "AD_SET",
            ad_set_key,
            MetaEntityStatus.NEEDS_RECONCILIATION,
            client_id=batch_client_id,
        )
    return {
        "status": "active" if not needs_reconciliation else "needs_reconciliation",
        "batch_id": req.batch_id,
        "activated_campaigns": activated_campaigns,
        "needs_reconciliation": needs_reconciliation,
        "activation_errors": activation_errors,
        "approved_by": user.user_id,
        "hierarchy_hash": req.hierarchy_hash,
        "confirmed_budget_minor_units": req.confirmed_budget_minor_units,
        "activation_recorded": True,
    }


@router.post("/launch-batch", status_code=status.HTTP_201_CREATED)
@endpoint_errors
def launch_batch(
    req: LaunchBatchRequest,
    request: Request = None,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Launch a batch to Meta, record the employee contact, and trigger dispatches."""
    init_flow_tables()
    if not req.employee_email or "@" not in req.employee_email:
        raise HTTPException(status_code=400, detail="A valid employee email is required for notification tracking.")
    normalized_platforms: list[str] = []
    for raw_p in req.target_platforms:
        if not raw_p or not str(raw_p).strip():
            raise HTTPException(status_code=400, detail="Platform name cannot be empty.")
        norm = normalize_platform(str(raw_p))
        if norm.flagged or norm.canonical_value not in {"facebook", "instagram"}:
            raise HTTPException(status_code=400, detail=f"Unsupported platform '{raw_p}'. Launch Gate currently supports Meta only (Facebook and Instagram).")
        normalized_platforms.append(norm.canonical_value)
    normalized_platforms = list(dict.fromkeys(normalized_platforms))
    req.client_id = resolve_campaign_client(user, req.client_id, req.batch_id, req.destination_url or "", req.ad_text or req.headline or "")
    if req.batch_id and not req.campaigns:
        with get_session() as conn:
            stored_campaigns = load_batch_campaigns(conn, req.batch_id, req.client_id)
        if stored_campaigns is not None:
            req.campaigns = stored_campaigns
            req.target_platforms = list(dict.fromkeys(
                ad.platform
                for campaign in stored_campaigns
                for ad_set in campaign.ad_sets
                for ad in ad_set.ads
            )) or req.target_platforms
            normalized_platforms = list(req.target_platforms)
    if req.campaigns:
        hierarchy = build_launch_hierarchy(req)
        if not req.hierarchy_preview_hash:
            raise HTTPException(status_code=409, detail="Explicit campaign hierarchy requires an approved preview hash.")
        if req.hierarchy_preview_hash != hierarchy["hierarchy_hash"]:
            raise HTTPException(status_code=409, detail="Launch hierarchy changed after preview; obtain a new hierarchy preview and approval.")
    else:
        hierarchy = build_launch_hierarchy(req)
        if req.meta_launch:
            if not req.hierarchy_preview_hash:
                raise HTTPException(status_code=409, detail="Meta launch requires an approved hierarchy preview hash.")
            if req.hierarchy_preview_hash != hierarchy["hierarchy_hash"]:
                raise HTTPException(status_code=409, detail="Launch hierarchy changed after preview; obtain a new hierarchy preview and approval.")
    from apps.worker.mcp_client import meta_live_writes_enabled
    if req.meta_launch and not meta_live_writes_enabled():
        raise HTTPException(
            status_code=409,
            detail="Meta campaign creation requires META_MCP_ENABLE_LIVE=true and META_MCP_ALLOW_WRITES=true.",
        )
    meta_ad_account_id = ""
    campaign_run_ids: dict[str, Any] = {}
    ad_set_run_ids: dict[str, Any] = {}

    if req.meta_launch and req.campaigns and req.batch_id:
        hierarchy_hash = hierarchy["hierarchy_hash"]
        with get_session() as conn:
            with conn.cursor() as cur:
                for campaign in hierarchy["hierarchy"]["campaigns"]:
                    platforms_for_campaign = campaign_platforms(campaign)
                    if not platforms_for_campaign:
                        raise HTTPException(status_code=422, detail=f"Campaign {campaign['campaign_key']} contains no supported Meta ads.")
                    platform_key = platforms_for_campaign[0]
                    campaign_entity_key = f"{req.batch_id}:{req.client_id}:{campaign['campaign_key']}"
                    campaign_idempotency = hashlib.sha256(campaign_entity_key.encode()).hexdigest()
                    campaign_budget_minor_units = (
                        campaign.get("daily_budget_minor_units")
                        or campaign.get("lifetime_budget_minor_units")
                        or sum(
                            ad_set.get("daily_budget_minor_units") or ad_set.get("lifetime_budget_minor_units") or 0
                            for ad_set in campaign["ad_sets"]
                        )
                    )
                    campaign_config = {
                        key: campaign.get(key)
                        for key in (
                            "name", "objective", "budget_strategy", "daily_budget_minor_units",
                            "lifetime_budget_minor_units", "currency", "special_ad_category",
                        )
                    }
                    campaign_config_hash = hashlib.sha256(json.dumps(campaign_config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    campaign_idempotency = hashlib.sha256(
                        f"{req.batch_id}:{req.client_id}:{campaign['campaign_key']}:{campaign_config_hash}".encode()
                    ).hexdigest()
                    cur.execute(
                        "SELECT id, config_hash, meta_id, status, approval_snapshot_hash FROM meta_campaign_runs WHERE batch_id=%s AND workspace_id=%s AND grouping_key=%s FOR UPDATE",
                        (req.batch_id, req.client_id, campaign["campaign_key"]),
                    )
                    campaign_run = cur.fetchone()
                    if campaign_run and campaign_run[1] != campaign_config_hash and campaign_run[4] != hierarchy_hash:
                        raise HTTPException(status_code=409, detail=f"Campaign '{campaign['campaign_key']}' settings changed after its first launch attempt. Use a new campaign_key to create a distinct campaign.")
                    if campaign_run:
                        campaign_run_id = campaign_run[0]
                        cur.execute(
                            """UPDATE meta_campaign_runs SET platform=%s, idempotency_key=%s,
                               approval_snapshot_hash=%s, config_hash=%s, status=CASE WHEN meta_id IS NULL THEN 'PENDING' ELSE status END,
                               updated_at=NOW() WHERE id=%s""",
                            (platform_key, campaign_idempotency, hierarchy_hash, campaign_config_hash, campaign_run_id),
                        )
                    else:
                        cur.execute(
                            """INSERT INTO meta_campaign_runs
                               (batch_id, workspace_id, grouping_key, platform, idempotency_key,
                                approval_snapshot_hash, config_hash, status)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,'PENDING') RETURNING id""",
                            (req.batch_id, req.client_id, campaign["campaign_key"], platform_key,
                             campaign_idempotency, hierarchy_hash, campaign_config_hash),
                        )
                        campaign_run_id = cur.fetchone()[0]
                    campaign_run_ids[campaign_entity_key] = campaign_run_id
                    cur.execute(
                            """INSERT INTO meta_launch_campaigns
                               (entity_key, batch_id, client_id, platform, grouping_key, idempotency_key, name, budget_minor_units, hierarchy_hash)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (entity_key) DO UPDATE SET name=EXCLUDED.name,
                               budget_minor_units=EXCLUDED.budget_minor_units,
                               hierarchy_hash=EXCLUDED.hierarchy_hash, updated_at=NOW()""",
                            (campaign_entity_key, req.batch_id, req.client_id, platform_key, campaign["campaign_key"], campaign_idempotency,
                             campaign["name"], campaign_budget_minor_units, hierarchy_hash),
                        )
                    for ad_set in campaign["ad_sets"]:
                        ad_set_entity_key = f"{campaign_entity_key}:{ad_set['ad_set_key']}"
                        ad_set_config = {
                            key: ad_set.get(key)
                            for key in (
                                "name", "targeting", "placements", "optimization_goal", "billing_event",
                                "daily_budget_minor_units", "lifetime_budget_minor_units", "schedule",
                            )
                        }
                        ad_set_config_hash = hashlib.sha256(json.dumps(ad_set_config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        ad_set_idempotency = hashlib.sha256(
                            f"{campaign_run_id}:{ad_set['ad_set_key']}:{ad_set_config_hash}".encode()
                        ).hexdigest()
                        cur.execute(
                            "SELECT id, config_hash, meta_id, status, approval_snapshot_hash FROM meta_ad_set_runs WHERE campaign_run_id=%s AND grouping_key=%s FOR UPDATE",
                            (campaign_run_id, ad_set["ad_set_key"]),
                        )
                        ad_set_run = cur.fetchone()
                        if ad_set_run and ad_set_run[1] != ad_set_config_hash and ad_set_run[4] != hierarchy_hash:
                            raise HTTPException(status_code=409, detail=f"Ad set '{ad_set['ad_set_key']}' settings changed after its first launch attempt. Use a new ad_set_key to create a distinct ad set.")
                        if ad_set_run:
                            ad_set_run_id = ad_set_run[0]
                            cur.execute(
                                """UPDATE meta_ad_set_runs SET idempotency_key=%s, approval_snapshot_hash=%s,
                                   config_hash=%s, status=CASE WHEN meta_id IS NULL THEN 'PENDING' ELSE status END,
                                   updated_at=NOW() WHERE id=%s""",
                                (ad_set_idempotency, hierarchy_hash, ad_set_config_hash, ad_set_run_id),
                            )
                        else:
                            cur.execute(
                                """INSERT INTO meta_ad_set_runs
                                   (campaign_run_id, grouping_key, idempotency_key, approval_snapshot_hash,
                                    config_hash, status)
                                   VALUES (%s,%s,%s,%s,%s,'PENDING') RETURNING id""",
                                (campaign_run_id, ad_set["ad_set_key"], ad_set_idempotency, hierarchy_hash, ad_set_config_hash),
                            )
                            ad_set_run_id = cur.fetchone()[0]
                        ad_set_run_ids[ad_set_entity_key] = ad_set_run_id
                        cur.execute(
                                """INSERT INTO meta_launch_ad_sets
                                   (entity_key, campaign_entity_key, batch_id, client_id, platform, grouping_key,
                                    idempotency_key, name, hierarchy_hash)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT (entity_key) DO UPDATE SET name=EXCLUDED.name,
                                   hierarchy_hash=EXCLUDED.hierarchy_hash, updated_at=NOW()""",
                                  (ad_set_entity_key, campaign_entity_key, req.batch_id, req.client_id, platform_key, ad_set["ad_set_key"],
                                 ad_set_idempotency, ad_set["name"], hierarchy_hash),
                            )
            conn.commit()

    launcher_phone = ""

    if req.meta_launch:
        missing_connection = []
        if not os.environ.get("MCP_META_SERVER_URL", "").strip():
            missing_connection.append("MCP_META_SERVER_URL")
        if not os.environ.get("META_SYSTEM_USER_TOKEN", "").strip():
            missing_connection.append("META_SYSTEM_USER_TOKEN")
        if missing_connection:
            raise HTTPException(status_code=503, detail=f"Live Meta configuration is incomplete: {', '.join(missing_connection)}.")
        if req.campaigns:
            for campaign in hierarchy["hierarchy"]["campaigns"]:
                if campaign["special_ad_category"] not in {"NONE", "HOUSING", "EMPLOYMENT", "FINANCIAL_PRODUCTS_SERVICES", "ISSUES_ELECTIONS_POLITICS"}:
                    raise HTTPException(status_code=422, detail=f"Unsupported special ad category in campaign {campaign['campaign_key']}.")
                for ad_set in campaign["ad_sets"]:
                    if not ad_set["targeting_country"] or len(ad_set["targeting_country"]) != 2 or not ad_set["targeting_country"].isalpha():
                        raise HTTPException(status_code=422, detail=f"Ad set {ad_set['ad_set_key']} requires one two-letter targeting country.")
                    for ad in ad_set["ads"]:
                        if not ad["page_id"] or not str(ad["creative_file"]).startswith("https://"):
                            raise HTTPException(status_code=422, detail=f"Ad {ad['ad_key']} requires a Page ID and public HTTPS creative.")
                        if ad["creative_mime_type"] not in {"image/jpeg", "image/png"}:
                            raise HTTPException(status_code=422, detail=f"Ad {ad['ad_key']} requires a PNG or JPEG creative.")
                        if ad["platform"] == "instagram" and not ad["instagram_user_id"]:
                            raise HTTPException(status_code=422, detail=f"Ad {ad['ad_key']} requires an Instagram actor ID.")
        else:
            if not req.meta_page_id or not req.targeting_country or not req.daily_budget_minor_units:
                raise HTTPException(status_code=422, detail="Page ID, targeting country, and daily budget are required for Meta launch.")
            if not req.targeting_country.strip().isalpha() or len(req.targeting_country.strip()) != 2:
                raise HTTPException(status_code=422, detail="Targeting country must be a two-letter country code.")
            if "instagram" in normalized_platforms and not req.instagram_user_id:
                raise HTTPException(status_code=422, detail="Instagram account ID is required when Instagram is selected.")
            if not req.creative_file or not req.creative_file.startswith("https://"):
                raise HTTPException(status_code=422, detail="Meta launch requires an uploaded creative with a public HTTPS URL.")
            if req.creative_mime_type not in {"image/jpeg", "image/png"}:
                raise HTTPException(status_code=422, detail="The current Meta create chain requires a PNG or JPEG image creative.")
            if req.special_ad_category not in {"NONE", "HOUSING", "EMPLOYMENT", "FINANCIAL_PRODUCTS_SERVICES", "ISSUES_ELECTIONS_POLITICS"}:
                raise HTTPException(status_code=422, detail="Unsupported Meta special ad category.")
        with get_session() as account_conn:
            from apps.api.clients import resolve_client_meta_account
            try:
                meta_ad_account_id = resolve_client_meta_account(account_conn, req.client_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    batch_id = req.batch_id or f"batch_{req.client_id[:3]}_{uuid.uuid4().hex[:8]}"
    launch_attempt_id = (req.launch_attempt_id or "").strip() or f"lnch_{batch_id[:12]}_{uuid.uuid4().hex[:8]}"
    contact_id = f"cnt_{uuid.uuid4().hex[:8]}"

    # 1. Ensure valid batch exists in batches table
    with get_session() as conn:
        with conn.cursor() as cur:
            actual_batch_id = req.batch_id or f"batch_{req.client_id[:3]}_{uuid.uuid4().hex[:8]}"
            client_ip = get_client_ip(request) if request is not None else "127.0.0.1"
            cur.execute(
                """
                INSERT INTO batches (batch_id, uploaded_by, filename, sheet_hash, uploader_ip, is_sample)
                VALUES (%s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (batch_id) DO NOTHING;
                """,
                (actual_batch_id, user.user_id, f"{req.client_id}_campaign.csv", f"hash_{uuid.uuid4().hex[:8]}", client_ip),
            )

            cur.execute("SELECT uploaded_by FROM batches WHERE batch_id = %s FOR UPDATE", (actual_batch_id,))
            owner = cur.fetchone()
            if owner and owner[0] != user.user_id:
                cur.execute("SELECT DISTINCT client_id FROM ads WHERE batch_id = %s", (actual_batch_id,))
                batch_clients = [row[0] for row in cur.fetchall()]
                if not batch_clients or req.client_id not in batch_clients:
                    raise HTTPException(status_code=403, detail="This batch is not available to the selected client.")

            # Record contact
            cur.execute("UPDATE batches SET campaign_name = COALESCE(campaign_name, %s) WHERE batch_id = %s",
                        (req.campaign_name.strip()[:120], actual_batch_id))
            cur.execute(
                """
                INSERT INTO campaign_launch_contacts (
                    contact_id, batch_id, client_id, employee_email, employee_phone, uploader_ip
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (batch_id, employee_email) DO UPDATE SET
                    employee_email = EXCLUDED.employee_email,
                    employee_phone = EXCLUDED.employee_phone,
                    uploader_ip = EXCLUDED.uploader_ip,
                    updated_at = NOW();
                """,
                (contact_id, actual_batch_id, req.client_id, req.employee_email, launcher_phone, client_ip),
            )

            # Query real ads for this batch or client
            cur.execute(
                "SELECT ad_id, headline, ad_text, destination_url, row_hash, creative_file, platform, hierarchy_data FROM ads WHERE batch_id = %s AND client_id = %s;",
                (actual_batch_id, req.client_id),
            )
            ads_list = cur.fetchall()

            # A launch may only dispatch an uploaded or explicitly supplied ad.
            if not ads_list:
                if not all([(req.headline or "").strip(), (req.ad_text or "").strip(), (req.destination_url or "").strip()]):
                    raise HTTPException(
                        status_code=422,
                        detail="No real ads were found for this batch. Upload a campaign sheet or provide complete campaign copy and a destination URL before launching.",
                    )
                generated_ad_id = f"ad_{actual_batch_id[:12]}_{uuid.uuid4().hex[:6]}"
                ad_headline = req.headline.strip()
                ad_text = req.ad_text.strip()
                dest_url = req.destination_url.strip()
                canonical = {"platform": normalized_platforms[0], "headline": ad_headline,
                             "ad_text": ad_text, "destination_url": dest_url,
                             "creative_file": req.creative_file or ""}
                cur.execute(
                    """
                    INSERT INTO ads (
                        ad_id, batch_id, client_id, row_number, platform, headline, ad_text, destination_url,
                        creative_file, row_hash, sheet_hash, is_sample
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                    ON CONFLICT (ad_id) DO NOTHING;
                    """,
                    (
                        generated_ad_id, actual_batch_id, req.client_id, 1, normalized_platforms[0],
                        ad_headline, ad_text, dest_url, req.creative_file,
                        hash_normalized_row(canonical), hash_normalized_sheet([canonical]),
                    ),
                )
                from apps.api import outbox
                from apps.api.queue import AdJob
                outbox.persist_ad_jobs_on_connection(conn, [AdJob(batch_id=actual_batch_id, ad_id=generated_ad_id)])
                cur.execute("UPDATE batches SET sheet_hash = %s WHERE batch_id = %s", (hash_normalized_sheet([canonical]), actual_batch_id))
                ads_list = [(generated_ad_id, ad_headline, ad_text, dest_url, hash_normalized_row(canonical), req.creative_file, normalized_platforms[0], None)]

            hierarchy_by_ad_id: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]] = {}
            campaign_parent_ids: dict[str, str] = {}
            ad_set_parent_ids: dict[str, str] = {}
            failed_hierarchy_entities: dict[str, str] = {}
            if req.campaigns:
                hierarchy_campaigns = hierarchy["hierarchy"]["campaigns"]
                ads_list = order_ads_by_hierarchy(hierarchy_campaigns, ads_list)
                for campaign in hierarchy_campaigns:
                    campaign_entity_key = f"{actual_batch_id}:{req.client_id}:{campaign['campaign_key']}"
                    for ad_set in campaign["ad_sets"]:
                        ad_set_entity_key = f"{campaign_entity_key}:{ad_set['ad_set_key']}"
                        for ad in ad_set["ads"]:
                            hierarchy_by_ad_id[str(ad["ad_id"])] = (
                                campaign, ad_set, ad, campaign_entity_key, ad_set_entity_key
                            )
                cur.execute(
                    "SELECT grouping_key, meta_id FROM meta_campaign_runs WHERE batch_id=%s AND workspace_id=%s",
                    (actual_batch_id, req.client_id),
                )
                campaign_parent_ids = {
                    f"{actual_batch_id}:{req.client_id}:{key}": str(value)
                    for key, value in cur.fetchall() if value
                }
                cur.execute(
                    """SELECT campaign.grouping_key, ad_set.grouping_key, ad_set.meta_id
                       FROM meta_ad_set_runs ad_set
                       JOIN meta_campaign_runs campaign ON campaign.id=ad_set.campaign_run_id
                       WHERE campaign.batch_id=%s AND campaign.workspace_id=%s""",
                    (actual_batch_id, req.client_id),
                )
                ad_set_parent_ids = {
                    f"{actual_batch_id}:{req.client_id}:{campaign_key}:{ad_set_key}": str(value)
                    for campaign_key, ad_set_key, value in cur.fetchall() if value
                }
            if req.meta_launch:
                cur.execute(
                    """INSERT INTO meta_launch_approval_audit
                       (authorization_id, batch_id, client_id, hierarchy_hash, approved_by)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (batch_id, client_id, hierarchy_hash) DO UPDATE SET
                           approved_by=EXCLUDED.approved_by, approved_at=NOW()""",
                    (
                        f"auth_{uuid.uuid4().hex}",
                        actual_batch_id,
                        req.client_id,
                        hierarchy["hierarchy_hash"],
                        user.user_id,
                    ),
                )
                conn.commit()

            # Initialize MCP platform tool dispatcher
            mcp_dispatcher = MCPPlatformDispatcher(
                access_token=os.environ.get("META_SYSTEM_USER_TOKEN", "").strip()
            )
            notifications_dispatched: list[dict[str, Any]] = []
            dispatch_results: list[dict[str, Any]] = []

            # Execute MCP tool calls across requested platforms
            alerted_platforms: set[str] = set()
            for ad_row in ads_list:
                ad_id = ad_row[0]
                headline = ad_row[1] if len(ad_row) > 1 else ""
                destination_url = ad_row[3] if len(ad_row) > 3 else ""
                canonical = {"platform": ad_row[6], "headline": ad_row[1] or "", "ad_text": ad_row[2] or "",
                             "destination_url": ad_row[3], "creative_file": ad_row[5] or ""}
                if hash_normalized_row(canonical) != ad_row[4]:
                    raise HTTPException(status_code=409, detail="Ad content changed; save an edit and recheck before authorizing.")
                cur.execute("SELECT state, content_hash, approved_by FROM ad_approvals WHERE ad_id = %s", (ad_id,))
                existing_approval = cur.fetchone()
                if existing_approval:
                    curr_st = existing_approval[0]
                    curr_hash = existing_approval[1]
                    if curr_st == "rejected":
                        raise HTTPException(status_code=409, detail="Ad has been rejected and cannot be dispatched without remediation and re-approval.")
                    if curr_st == "dead_letter":
                        raise HTTPException(status_code=409, detail="Dead-lettered ad cannot be dispatched without remediation and re-approval.")
                    if curr_hash and curr_hash != ad_row[4]:
                        raise HTTPException(status_code=409, detail="Ad content changed; save an edit and recheck before authorizing.")
                if req.batch_id and not req.meta_launch and (
                    not existing_approval
                    or existing_approval[0] not in {"approved", "sent"}
                    or existing_approval[1] != ad_row[4]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Every ad in an existing batch must be approved in review before dry-run dispatch.",
                    )

                cur.execute(
                    """
                    INSERT INTO ad_approvals (ad_id, client_id, state, approved_by, approved_at, content_hash, content_snapshot, updated_at)
                    VALUES (%s, %s, 'approved', %s, NOW(), %s, %s, NOW())
                    ON CONFLICT (ad_id) DO UPDATE SET
                        state = 'approved',
                        approved_by = EXCLUDED.approved_by,
                        approved_at = NOW(),
                        content_hash = EXCLUDED.content_hash,
                        content_snapshot = EXCLUDED.content_snapshot,
                        updated_at = NOW()
                    WHERE ad_approvals.state IN ('pending_review', 'blocked_connector_not_configured');
                    """,
                    (ad_id, req.client_id, user.user_id, ad_row[4], json.dumps(canonical, sort_keys=True)),
                )
                cur.execute("SELECT state, content_hash FROM ad_approvals WHERE ad_id = %s", (ad_id,))
                approval = cur.fetchone()
                if not approval or approval[0] not in {'approved', 'sent'} or approval[1] != ad_row[4]:
                    raise HTTPException(status_code=409, detail="The current ad content requires a recorded human approval before dispatch.")
                cur.execute("INSERT INTO approval_history (ad_id, from_state, to_state, changed_by, content_hash, reason) VALUES (%s,%s,%s,%s,%s,%s)",
                            (ad_id, existing_approval[0] if existing_approval else 'pending_review', 'approved', user.user_id, ad_row[4], 'Explicit campaign authorization'))
                conn.commit()
                row_platforms = (
                    [normalize_platform(str(ad_row[6])).canonical_value]
                    if req.campaigns or not req.meta_launch
                    else normalized_platforms
                )
                for plat in row_platforms:
                    if plat not in {"facebook", "instagram"}:
                        raise HTTPException(status_code=422, detail=f"Ad {ad_id} has unsupported platform in hierarchy.")
                    disp_id = f"disp_{ad_id}_{plat}_{ad_row[4][:16]}"
                    handle = f"@{req.client_id}_{plat}"
                    cur.execute("SELECT status FROM ad_live_dispatches WHERE dispatch_id = %s", (disp_id,))
                    previous_dispatch = cur.fetchone()
                    if previous_dispatch and previous_dispatch[0] in {'active_live', 'paused', 'pending_review', 'scheduled'}:
                        dispatch_results.append({"ad_id": ad_id, "platform": plat, "status": "sent", "error": None})
                        continue

                    cur.execute(
                        "SELECT campaign_id, ad_set_id, creative_id, platform_ad_id FROM meta_launch_objects WHERE ad_id=%s AND platform=%s",
                        (ad_id, plat),
                    )
                    stored_objects = cur.fetchone()
                    existing_ids = dict(zip(("campaign", "ad_set", "creative", "ad"), stored_objects or (None, None, None, None)))
                    if req.meta_launch and req.campaigns:
                        parent_campaign, parent_ad_set, hierarchy_ad, campaign_entity_key, ad_set_entity_key = hierarchy_by_ad_id[ad_id]
                        if campaign_parent_ids.get(campaign_entity_key):
                            existing_ids["campaign"] = campaign_parent_ids[campaign_entity_key]
                        if ad_set_parent_ids.get(ad_set_entity_key):
                            existing_ids["ad_set"] = ad_set_parent_ids[ad_set_entity_key]

                    def persist_meta_stage(stage: str, object_id: str) -> None:
                        columns = {"campaign": "campaign_id", "ad_set": "ad_set_id", "creative": "creative_id", "ad": "platform_ad_id"}
                        column = columns[stage]
                        cur.execute(
                            f"""INSERT INTO meta_launch_objects (ad_id, platform, {column}) VALUES (%s,%s,%s)
                                ON CONFLICT (ad_id, platform) DO UPDATE SET {column}=EXCLUDED.{column}, updated_at=NOW()""",
                            (ad_id, plat, object_id),
                        )
                        if req.campaigns and stage == "campaign":
                            campaign_parent_ids[campaign_entity_key] = object_id
                            cur.execute(
                                "UPDATE meta_campaign_runs SET meta_id=%s, status='CREATED', last_error=NULL, updated_at=NOW() WHERE id=%s",
                                (object_id, campaign_run_ids[campaign_entity_key]),
                            )
                            cur.execute(
                                "UPDATE meta_launch_campaigns SET meta_campaign_id=%s, lifecycle_status='created_paused', updated_at=NOW() WHERE entity_key=%s",
                                (object_id, campaign_entity_key),
                            )
                        if req.campaigns and stage == "ad_set":
                            ad_set_parent_ids[ad_set_entity_key] = object_id
                            cur.execute(
                                "UPDATE meta_ad_set_runs SET meta_id=%s, status='CREATED', last_error=NULL, updated_at=NOW() WHERE id=%s",
                                (object_id, ad_set_run_ids[ad_set_entity_key]),
                            )
                            cur.execute(
                                "UPDATE meta_launch_ad_sets SET meta_ad_set_id=%s, lifecycle_status='created_paused', updated_at=NOW() WHERE entity_key=%s",
                                (object_id, ad_set_entity_key),
                            )
                        conn.commit()

                    def persist_meta_stage_status(stage: str, run_status: str, error: str | None = None) -> None:
                        if not req.campaigns:
                            return
                        safe_error = " ".join(str(error or "").split())[:500] or None
                        if stage == "campaign":
                            cur.execute(
                                "UPDATE meta_campaign_runs SET status=%s, last_error=%s, updated_at=NOW() WHERE id=%s",
                                (run_status, safe_error, campaign_run_ids[campaign_entity_key]),
                            )
                            compatibility_status = {"PENDING": "pending", "CREATING": "creating", "RECONCILING": "reconciling", "CREATED": "created_paused", "FAILED": "failed", "ACTIVE": "active"}[run_status]
                            cur.execute(
                                "UPDATE meta_launch_campaigns SET lifecycle_status=%s, last_error=%s, updated_at=NOW() WHERE entity_key=%s",
                                (compatibility_status, safe_error, campaign_entity_key),
                            )
                        elif stage == "ad_set":
                            cur.execute(
                                "UPDATE meta_ad_set_runs SET status=%s, last_error=%s, updated_at=NOW() WHERE id=%s",
                                (run_status, safe_error, ad_set_run_ids[ad_set_entity_key]),
                            )
                            compatibility_status = {"PENDING": "pending", "CREATING": "creating", "RECONCILING": "reconciling", "CREATED": "created_paused", "FAILED": "failed", "ACTIVE": "active"}[run_status]
                            cur.execute(
                                "UPDATE meta_launch_ad_sets SET lifecycle_status=%s, last_error=%s, updated_at=NOW() WHERE entity_key=%s",
                                (compatibility_status, safe_error, ad_set_entity_key),
                            )
                        conn.commit()
                        emit_progress_event(
                            req.batch_id,
                            "CAMPAIGN" if stage == "campaign" else "AD_SET",
                            campaign_entity_key if stage == "campaign" else ad_set_entity_key,
                            entity_status_for_run(run_status),
                            safe_error,
                            req.client_id,
                        )

                    if req.meta_launch and req.campaigns:
                        campaign_budget_strategy = parent_campaign["budget_strategy"]
                        campaign_daily_budget = parent_campaign.get("daily_budget_minor_units") or 0
                        campaign_lifetime_budget = parent_campaign.get("lifetime_budget_minor_units")
                        ad_set_daily_budget = parent_ad_set.get("daily_budget_minor_units")
                        ad_set_lifetime_budget = parent_ad_set.get("lifetime_budget_minor_units")
                        ad_page_id = hierarchy_ad.get("page_id") or ""
                        ad_actor_id = hierarchy_ad.get("instagram_user_id")
                        ad_targeting = parent_ad_set.get("targeting") or {}
                        ad_country = parent_ad_set.get("targeting_country") or ""
                        ad_countries = parent_ad_set.get("targeting", {}).get("geo_locations", {}).get("countries", [])
                        campaign_countries = list(dict.fromkeys(
                            str(country).upper()
                            for ad_set_model in parent_campaign.get("ad_sets", [])
                            for country in (ad_set_model.get("targeting", {}).get("geo_locations", {}).get("countries", []))
                        ))
                        ad_objective = parent_campaign.get("objective") or "OUTCOME_TRAFFIC"
                        ad_special_category = parent_campaign.get("special_ad_category") or "NONE"
                        ad_optimization_goal = parent_ad_set.get("optimization_goal") or "LINK_CLICKS"
                        ad_billing_event = parent_ad_set.get("billing_event") or "IMPRESSIONS"
                        ad_schedule = parent_ad_set.get("schedule") or {}
                        ad_campaign_name = parent_campaign["name"]
                        ad_creative_url = hierarchy_ad.get("creative_file") or ""
                    else:
                        campaign_budget_strategy = "DAILY"
                        campaign_daily_budget = req.daily_budget_minor_units or 0
                        campaign_lifetime_budget = None
                        ad_set_daily_budget = None
                        ad_set_lifetime_budget = None
                        ad_page_id = req.meta_page_id or ""
                        ad_actor_id = req.instagram_user_id
                        ad_targeting = None
                        ad_country = req.targeting_country or ""
                        ad_countries = [ad_country] if ad_country else []
                        campaign_countries = [ad_country] if ad_country else []
                        ad_objective = "OUTCOME_TRAFFIC"
                        ad_special_category = req.special_ad_category or "NONE"
                        ad_optimization_goal = "LINK_CLICKS"
                        ad_billing_event = "IMPRESSIONS"
                        ad_schedule = None
                        ad_campaign_name = req.campaign_name
                        ad_creative_url = req.creative_file or ""

                    prior_parent_failure = None
                    if req.meta_launch and req.campaigns:
                        prior_parent_failure = (
                            failed_hierarchy_entities.get(campaign_entity_key)
                            or failed_hierarchy_entities.get(ad_set_entity_key)
                        )
                    if prior_parent_failure:
                        mcp_resp = MCPResponse(
                            False,
                            plat,
                            "ads_create_ad",
                            {"campaign_id": existing_ids.get("campaign"), "ad_set_id": existing_ids.get("ad_set")},
                            f"meta_chain_incomplete: prior parent creation failed: {prior_parent_failure}",
                        )
                    elif req.meta_launch:
                        mcp_resp = mcp_dispatcher.dispatch_paused_ad(
                        platform=plat,
                        ad_id=ad_id,
                        campaign_name=ad_campaign_name,
                        headline=headline,
                        ad_text=ad_row[2] or "",
                        destination_url=destination_url or "",
                        ad_account_id=meta_ad_account_id,
                        page_id=ad_page_id,
                        instagram_user_id=ad_actor_id,
                        targeting_country=ad_country,
                        daily_budget_minor_units=campaign_daily_budget,
                        image_url=ad_creative_url,
                        special_ad_category=ad_special_category,
                        campaign_objective=ad_objective,
                        campaign_budget_strategy=campaign_budget_strategy,
                        campaign_lifetime_budget_minor_units=campaign_lifetime_budget,
                        campaign_key=campaign_entity_key if req.campaigns else None,
                        ad_set_key=ad_set_entity_key if req.campaigns else None,
                        ad_set_name=parent_ad_set["name"] if req.campaigns else None,
                        targeting_spec=ad_targeting,
                        placements=parent_ad_set.get("placements") if req.campaigns else None,
                        optimization_goal=ad_optimization_goal,
                        billing_event=ad_billing_event,
                        ad_set_daily_budget_minor_units=ad_set_daily_budget,
                        ad_set_lifetime_budget_minor_units=ad_set_lifetime_budget,
                        adset_schedule=ad_schedule,
                        targeting_countries=ad_countries,
                        special_ad_category_countries=campaign_countries,
                        existing_ids=existing_ids,
                        persist_stage=persist_meta_stage,
                        persist_stage_status=persist_meta_stage_status,
                        )
                    else:
                        from apps.worker.sender import send_ad_to_stand_in

                        stand_in_result = send_ad_to_stand_in(
                            ad_id,
                            connection=conn,
                            user_id=user.user_id,
                        )
                        mcp_resp = MCPResponse(
                            success=stand_in_result.get("status") == "sent",
                            platform=plat,
                            tool_name="stand_in_dry_run",
                            result={
                                "platform_ad_id": stand_in_result.get("platform_ad_id"),
                                "status": "paused",
                                "platform_review_status": "Stand-in dry-run accepted",
                                "audit_notes": "Dry-run only; no live Meta object was created.",
                            },
                            error=stand_in_result.get("error"),
                        )

                    # Check success on every connector call; on failure do not write fake rows
                    if not mcp_resp.success or not (mcp_resp.result or {}).get("platform_ad_id"):
                        error_msg = mcp_resp.error or "Platform did not confirm an ad ID. Delivery is unconfirmed."
                        if req.campaigns:
                            safe_error = " ".join(str(error_msg).split())[:500]
                            result_ids = mcp_resp.result or {}
                            if not result_ids.get("campaign_id"):
                                failed_hierarchy_entities[campaign_entity_key] = safe_error
                            elif not result_ids.get("ad_set_id"):
                                failed_hierarchy_entities[ad_set_entity_key] = safe_error
                            cur.execute(
                                "UPDATE meta_launch_campaigns SET lifecycle_status='failed', last_error=%s, updated_at=NOW() WHERE entity_key=%s AND meta_campaign_id IS NULL",
                                (safe_error, campaign_entity_key),
                            )
                            cur.execute(
                                "UPDATE meta_launch_ad_sets SET lifecycle_status='failed', last_error=%s, updated_at=NOW() WHERE entity_key=%s AND meta_ad_set_id IS NULL",
                                (safe_error, ad_set_entity_key),
                            )
                            conn.commit()
                        is_unconfigured = error_msg in {"connector_not_configured", "real_platform_calls_disabled"} or error_msg.startswith("meta_chain_incomplete:")
                        disp_status = "blocked_connector_not_configured" if is_unconfigured else "rejected"
                        if error_msg == "real_platform_calls_disabled":
                            review_status = "Blocked: Live Writes Disabled"
                        elif error_msg == "connector_not_configured":
                            review_status = "Blocked: Connector Not Configured"
                        elif error_msg.startswith("meta_chain_incomplete:"):
                            review_status = "Blocked: Meta creation incomplete"
                        else:
                            review_status = "Rejected / Dispatch Failed"
                        if error_msg == "real_platform_calls_disabled":
                            review_notes = "Real platform writes are disabled by Launch Gate v1 policy."
                        elif error_msg == "connector_not_configured":
                            review_notes = f"Platform connector '{plat}' is not configured."
                        else:
                            review_notes = error_msg
                        dispatch_results.append({
                            "ad_id": ad_id,
                            "platform": plat,
                            "status": "blocked" if is_unconfigured else "failed",
                            "error": review_notes,
                        })

                        # Trigger real-time multi-channel notification for failed or blocked dispatch (consolidated per platform)
                        if plat not in alerted_platforms:
                            alerted_platforms.add(plat)
                            try:
                                alert_res = NotificationDispatcher.dispatch_campaign_alert(
                                    client_id=req.client_id,
                                    recipient_email=req.employee_email,
                                    recipient_phone=launcher_phone,
                                    alert_type="launch_blocked" if is_unconfigured else "platform_rejection",
                                    subject=f"Launch Gate Alert: Campaign '{req.campaign_name}' {review_status} on {plat.title()}",
                                    message_body=(
                                        f"Platform dispatch failure detected for campaign '{req.campaign_name}'.\n\n"
                                        f"Client: {req.client_id}\n"
                                        f"Platform: {plat.title()}\n"
                                        f"Status: {review_status}\n"
                                        f"Details: {review_notes}"
                                    ),
                                    ad_id=ad_id,
                                    batch_id=actual_batch_id,
                                    platform=plat,
                                    launch_attempt_id=launch_attempt_id,
                                )
                                notifications_dispatched.append(alert_res)
                                cur.execute("INSERT INTO send_alerts (alert_id,ad_id,client_id,batch_id,alert_type,severity,message,details,launch_attempt_id) VALUES (%s,%s,%s,%s,%s,'warning',%s,%s,%s)",
                                            (str(uuid.uuid4()), ad_id, req.client_id, actual_batch_id, 'launch_blocked' if is_unconfigured else 'platform_rejection', review_notes, json.dumps(alert_res), launch_attempt_id))
                            except Exception as notif_err:
                                logger.exception("Failed to dispatch alert for ad %s: %s", ad_id, notif_err)

                        cur.execute(
                            """
                            INSERT INTO ad_live_dispatches (
                                dispatch_id, ad_id, batch_id, client_id, platform, handle,
                                status, platform_ad_id, platform_review_status, platform_review_notes, live_preview_url, launch_attempt_id,
                                meta_campaign_id, meta_ad_set_id, meta_creative_id
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s)
                            ON CONFLICT (dispatch_id) DO UPDATE SET
                                status = EXCLUDED.status,
                                platform_ad_id = EXCLUDED.platform_ad_id,
                                platform_review_status = EXCLUDED.platform_review_status,
                                platform_review_notes = EXCLUDED.platform_review_notes,
                                launch_attempt_id = EXCLUDED.launch_attempt_id,
                                meta_campaign_id = EXCLUDED.meta_campaign_id,
                                meta_ad_set_id = EXCLUDED.meta_ad_set_id,
                                meta_creative_id = EXCLUDED.meta_creative_id,
                                last_tracked_at = NOW();
                            """,
                            (disp_id, ad_id, actual_batch_id, req.client_id, plat, handle, disp_status, "", review_status, review_notes,
                             launch_attempt_id, existing_ids.get("campaign"), existing_ids.get("ad_set"), existing_ids.get("creative")),
                        )

                        if req.meta_launch and not is_unconfigured:
                            cur.execute(
                                """
                                INSERT INTO ad_sends (
                                    send_id, ad_id, client_id, batch_id, idempotency_key, content_hash, platform, status, error_message, is_dry_run, created_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'dead_letter', %s, FALSE, NOW())
                                ON CONFLICT (send_id) DO NOTHING;
                                """,
                                (f"send_{disp_id}", ad_id, req.client_id, actual_batch_id, disp_id, ad_row[4], plat, error_msg),
                            )
                        continue

                    # If connector call succeeded:
                    dispatch_results.append({
                        "ad_id": ad_id,
                        "platform": plat,
                        "status": "sent",
                        "error": None,
                    })
                    mcp_result = mcp_resp.result
                    plat_ad_id = mcp_result.get("platform_ad_id")
                    live_url = mcp_result.get("live_preview_url")
                    audit_notes = mcp_result.get("audit_notes")
                    platform_status = mcp_result.get("status", "pending_review")
                    if platform_status not in {'active_live', 'paused', 'pending_review', 'scheduled'}:
                        platform_status = 'pending_review'
                    platform_review = mcp_result.get("platform_review_status") or "Not checked"

                    cur.execute(
                        """
                        INSERT INTO ad_live_dispatches (
                            dispatch_id, ad_id, batch_id, client_id, platform, handle,
                            status, platform_ad_id, platform_review_status, platform_review_notes, live_preview_url,
                            launch_attempt_id, meta_campaign_id, meta_ad_set_id, meta_creative_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (dispatch_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            platform_ad_id = EXCLUDED.platform_ad_id,
                            platform_review_status = EXCLUDED.platform_review_status,
                            platform_review_notes = EXCLUDED.platform_review_notes,
                            launch_attempt_id = EXCLUDED.launch_attempt_id,
                            meta_campaign_id = EXCLUDED.meta_campaign_id,
                            meta_ad_set_id = EXCLUDED.meta_ad_set_id,
                            meta_creative_id = EXCLUDED.meta_creative_id,
                            last_tracked_at = NOW();
                        """,
                        (disp_id, ad_id, actual_batch_id, req.client_id, plat, handle, platform_status, plat_ad_id, platform_review, audit_notes, live_url,
                         launch_attempt_id, mcp_result.get("campaign_id"), mcp_result.get("ad_set_id"), mcp_result.get("creative_id")),
                    )

                    if req.meta_launch:
                        cur.execute("INSERT INTO ad_sends (send_id,ad_id,client_id,batch_id,idempotency_key,content_hash,platform,status,platform_ad_id,is_dry_run) VALUES (%s,%s,%s,%s,%s,%s,%s,'sent',%s,FALSE) ON CONFLICT (send_id) DO NOTHING",
                                    (f"send_{disp_id}",ad_id,req.client_id,actual_batch_id,disp_id,ad_row[4],plat,plat_ad_id))
                        perf_resp = mcp_dispatcher.call_tool(
                        platform=plat,
                        tool_name="ads_get_ad_entities",
                        arguments={
                            "ad_account_id": meta_ad_account_id,
                            "level": "ad",
                            "object_ids": [plat_ad_id],
                            "fields": ["amount_spent", "impressions", "clicks", "ctr", "omni_purchase", "omni_purchase_values"],
                            "client_conversation_id": mcp_result.get("client_conversation_id") or uuid.uuid4().hex[:20],
                            "advertiser_request": "Review and update the campaign preview, then confirm launch on the selected Meta platform(s).",
                            "advertiser_job": "create_and_launch_new_ads",
                        },
                        )
                    else:
                        perf_resp = None
                    if perf_resp and perf_resp.success:
                        perf_res = perf_resp.result
                        perf_rows = perf_res if isinstance(perf_res, list) else (perf_res.get("ad_entities") or perf_res.get("data") or [])
                        perf_ad = next((item for item in perf_rows if isinstance(item, dict) and str(item.get("id")) == str(plat_ad_id)), None)
                        if perf_ad:
                            account_currency = os.environ.get("META_AD_ACCOUNT_CURRENCY", "").strip().upper()
                            spend = _metric_number(perf_ad.get("amount_spent"))
                            purchase_values = _metric_number(perf_ad.get("omni_purchase_values"))
                            sales_usd = purchase_values if account_currency == "USD" else None
                            spend_usd = spend if account_currency == "USD" else None
                            purchases = _metric_number(perf_ad.get("omni_purchase"))
                            cur.execute(
                                """
                                INSERT INTO ad_performance_tracking (
                                    ad_id, batch_id, client_id, platform, handle,
                                    inbound_sales_usd, inbound_orders, impressions, clicks, ctr_percent, roas, spend_usd,
                                    has_rule_violation, violation_details, last_event
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, NULL, %s)
                                ON CONFLICT (ad_id, platform) DO UPDATE SET
                                    platform = EXCLUDED.platform,
                                    handle = EXCLUDED.handle,
                                    inbound_sales_usd = EXCLUDED.inbound_sales_usd,
                                    inbound_orders = EXCLUDED.inbound_orders,
                                    impressions = EXCLUDED.impressions,
                                    clicks = EXCLUDED.clicks,
                                    ctr_percent = EXCLUDED.ctr_percent,
                                    roas = EXCLUDED.roas,
                                    spend_usd = EXCLUDED.spend_usd,
                                    updated_at = NOW();
                                """,
                                (
                                    ad_id, actual_batch_id, req.client_id, plat, handle,
                                    sales_usd,
                                    int(purchases) if purchases is not None else None,
                                    int(_metric_number(perf_ad.get("impressions"))) if _metric_number(perf_ad.get("impressions")) is not None else None,
                                    int(_metric_number(perf_ad.get("clicks"))) if _metric_number(perf_ad.get("clicks")) is not None else None,
                                    _metric_number(perf_ad.get("ctr")),
                                    round(sales_usd / spend_usd, 2) if sales_usd is not None and spend_usd and spend_usd > 0 else None,
                                    spend_usd,
                                    f"Meta Ads insights (RPC: {perf_resp.rpc_id})",
                                ),
                            )
                ad_results = [item["status"] for item in dispatch_results if item["ad_id"] == ad_id]
                if ad_results and all(value == "blocked" for value in ad_results):
                    cur.execute("UPDATE ad_approvals SET state = 'blocked_connector_not_configured', rejection_reason = %s WHERE ad_id = %s AND state = 'approved'", ("Meta connector not configured", ad_id))
                elif ad_results and all(value == "sent" for value in ad_results):
                    cur.execute("UPDATE ad_approvals SET state = 'sending' WHERE ad_id = %s AND state = 'approved'", (ad_id,))
                    cur.execute("UPDATE ad_approvals SET state = 'sent', sent_at = NOW() WHERE ad_id = %s AND state = 'sending'", (ad_id,))
                elif "failed" in ad_results:
                    cur.execute("UPDATE ad_approvals SET state = 'dead_letter', dead_letter_reason = %s WHERE ad_id = %s AND state = 'approved'", ("See per-platform dispatch errors", ad_id))
        conn.commit()

    from apps.api.uploads import _dispatch_pending_jobs
    _dispatch_pending_jobs()
    return {
        "status": (
            "launched"
            if dispatch_results and all(item["status"] == "sent" for item in dispatch_results)
            else "blocked"
            if dispatch_results and all(item["status"] == "blocked" for item in dispatch_results)
            else "partial"
            if dispatch_results and any(item["status"] == "sent" for item in dispatch_results)
            else "failed"
        ),
        "batch_id": actual_batch_id,
        "launch_attempt_id": launch_attempt_id,
        "client_id": req.client_id,
        "campaign_name": req.campaign_name,
        "notification_contact": {
            "employee_email": req.employee_email,
            "employee_phone": launcher_phone,
            "dispatch_results": notifications_dispatched,
        },
        "results": dispatch_results,
        "notifications": notifications_dispatched,
        "target_platforms": normalized_platforms,
        "message": "Campaign attempt recorded. See per-platform results for actual delivery status.",
    }


@router.get("/mcp-traces")
@endpoint_errors
def get_mcp_traces(
    client_id: str | None = Query(None),
    batch_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Return real recorded traces of MCP tool calls, or empty if none recorded."""
    init_flow_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            allowed = list(user.assigned_client_ids)
            if client_id:
                require_client_access(user, client_id)
                cur.execute(
                    """
                    SELECT
                        dispatch_id, client_id, platform, handle, status, platform_review_status,
                        platform_review_notes, live_preview_url, dispatched_at
                    FROM ad_live_dispatches
                    WHERE client_id = %s AND (%s::text IS NULL OR batch_id = %s)
                    ORDER BY dispatched_at DESC
                    LIMIT 50;
                    """,
                    (client_id, batch_id, batch_id),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        dispatch_id, client_id, platform, handle, status, platform_review_status,
                        platform_review_notes, live_preview_url, dispatched_at
                    FROM ad_live_dispatches
                    WHERE client_id = ANY(%s) AND (%s::text IS NULL OR batch_id = %s)
                    ORDER BY dispatched_at DESC
                    LIMIT 50;
                    """,
                    (allowed, batch_id, batch_id),
                )
            rows = cur.fetchall()

    traces = [
        {
            "server": f"mcp_{r[2]}",
            "tool": "Meta Ads create/activation chain",
            "status": r[4],
            "duration_ms": None,
            "handle": r[3],
            "platform": r[2],
            "timestamp": r[8].strftime("%H:%M:%S") if r[8] else "",
            "payload_summary": r[6] or "",
        }
        for r in rows
    ]

    return {
        "status": "recorded" if traces else "no_dispatches",
        "mcp_active_servers": None,
        "total_dispatches": len(traces),
        "traces": traces,
        "server_health": {},
    }


@router.get("/platform-performance")
@endpoint_errors
def get_platform_performance(
    include_samples: bool | None = None,
    batch_id: str | None = None,
    client_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Return aggregated live performance, SVG bar graph data, donut chart data, and inbound sales."""
    init_flow_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            if client_id:
                require_client_access(user, client_id)
            allowed = [client_id] if client_id else list(user.assigned_client_ids)
            if include_samples is None:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM ads WHERE client_id = ANY(%s) AND is_sample = FALSE);",
                    (allowed,),
                )
                real_row = cur.fetchone()
                has_real = bool(real_row[0]) if real_row else False
                include_samples = False

            if include_samples:
                cur.execute(
                    """
                    SELECT
                        apt.platform,
                        SUM(apt.inbound_sales_usd),
                        SUM(apt.spend_usd),
                        SUM(apt.inbound_orders),
                        SUM(apt.impressions),
                        SUM(apt.clicks)
                    FROM ad_performance_tracking apt
                    WHERE apt.client_id = ANY(%s) AND (%s::text IS NULL OR apt.batch_id = %s)
                    GROUP BY apt.platform;
                    """,
                    (allowed, batch_id, batch_id),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        apt.platform,
                        SUM(apt.inbound_sales_usd),
                        SUM(apt.spend_usd),
                        SUM(apt.inbound_orders),
                        SUM(apt.impressions),
                        SUM(apt.clicks)
                    FROM ad_performance_tracking apt
                    JOIN ads a ON apt.ad_id = a.ad_id
                    WHERE apt.client_id = ANY(%s) AND (%s::text IS NULL OR apt.batch_id = %s) AND a.is_sample = FALSE
                    GROUP BY apt.platform;
                    """,
                    (allowed, batch_id, batch_id),
                )
            rows = cur.fetchall()

    platform_stats: list[dict[str, Any]] = []
    total_sales = 0.0
    total_spend = 0.0
    total_orders = 0
    total_imp = 0

    default_platforms = ["facebook", "instagram"]
    found_map = {r[0]: r for r in rows}
    all_platforms = list(default_platforms)
    for r in rows:
        if r[0] not in all_platforms:
            all_platforms.append(r[0])

    for plat in all_platforms:
        r = found_map.get(plat)
        sales = float(r[1]) if r and r[1] is not None else None
        spend = float(r[2]) if r and r[2] is not None else None
        orders = int(r[3]) if r and r[3] is not None else None
        imp = int(r[4]) if r and r[4] is not None else None
        clicks = int(r[5]) if r and r[5] is not None else None
        roas = round(sales / spend, 2) if sales is not None and spend and spend > 0 else None
        ctr = round(clicks / imp * 100, 2) if clicks is not None and imp and imp > 0 else None
        tracked = any(value is not None for value in (sales, spend, orders, imp, clicks))
        total_sales += sales or 0
        total_spend += spend or 0
        total_orders += orders or 0
        total_imp += imp or 0

        platform_stats.append({
            "platform": plat,
            "display_name": "Meta / Facebook" if plat == "facebook" else plat.title(),
            "inbound_sales_usd": sales,
            "spend_usd": spend,
            "orders": orders,
            "impressions": imp,
            "clicks": clicks,
            "roas": roas,
            "ctr_percent": ctr,
            "tracked": tracked,
        })

    # Enrich platform performance with RevenueTrackingSpecialistNode
    rev_node = RevenueTrackingSpecialistNode()
    for stat in platform_stats:
        if stat["inbound_sales_usd"] is not None and stat["spend_usd"] is not None:
            analysis = rev_node.analyze_performance(
                ad_id=f"platform_{stat['platform']}",
                platform=stat["platform"],
                metrics_data=stat,
            )
            stat["profitability_status"] = analysis.profitability_status
            stat["ai_recommendation"] = analysis.recommendation
            stat["ai_insights"] = analysis.ai_insights
        else:
            stat["profitability_status"] = "UNTRACKED"
            stat["ai_recommendation"] = f"No live performance data tracked yet for {stat['display_name']}."
            stat["ai_insights"] = None

    any_tracked = any(p["tracked"] for p in platform_stats)
    blended_roas = round(total_sales / total_spend, 2) if (any(p["inbound_sales_usd"] is not None for p in platform_stats) and total_spend > 0) else None

    summary_analysis = None
    if any(p["inbound_sales_usd"] is not None for p in platform_stats) and any(p["spend_usd"] is not None for p in platform_stats):
        summary_analysis = rev_node.analyze_performance(
            ad_id="portfolio_blended",
            platform="all_platforms",
            metrics_data={
                "inbound_sales_usd": total_sales,
                "spend_usd": total_spend,
                "inbound_orders": total_orders,
                "impressions": total_imp,
                "roas": blended_roas,
            },
        )

    # Donut chart data computed from real recorded dispatches
    with get_session() as conn:
        with conn.cursor() as cur:
            allowed = list(user.assigned_client_ids)
            sample_clause = "" if include_samples else "AND is_sample = FALSE"
            cur.execute(
                f"""
                SELECT status, COUNT(*)
                FROM ad_live_dispatches
                WHERE client_id = ANY(%s) AND (%s::text IS NULL OR batch_id = %s) {sample_clause}
                GROUP BY status;
                """,
                (allowed, batch_id, batch_id),
            )
            status_counts = dict(cur.fetchall())

    donut_data = []
    if status_counts.get("active_live", 0) > 0:
        donut_data.append({"label": "Active & Serving", "value": status_counts["active_live"], "color": "#10b981"})
    if status_counts.get("pending_review", 0) > 0:
        donut_data.append({"label": "Under Platform Review", "value": status_counts["pending_review"], "color": "#f59e0b"})
    if status_counts.get("failed_dispatch", 0) > 0:
        donut_data.append({"label": "Failed / Dead Letter", "value": status_counts["failed_dispatch"], "color": "#f43f5e"})
    if status_counts.get("blocked_connector_not_configured", 0) > 0:
        donut_data.append({"label": "Blocked (Unconfigured)", "value": status_counts["blocked_connector_not_configured"], "color": "#94a3b8"})
    if status_counts.get("scheduled", 0) > 0:
        donut_data.append({"label": "Scheduled / Staged", "value": status_counts["scheduled"], "color": "#8b5cf6"})

    return {
        "summary": {
            "total_sales_usd": total_sales if any(p["inbound_sales_usd"] is not None for p in platform_stats) else None,
            "total_spend_usd": total_spend if any(p["spend_usd"] is not None for p in platform_stats) else None,
            "blended_roas": blended_roas,
            "total_orders": total_orders if any(p["orders"] is not None for p in platform_stats) else None,
            "total_impressions": total_imp if any(p["impressions"] is not None for p in platform_stats) else None,
            "active_campaigns": len([p for p in platform_stats if p["tracked"] and (p["impressions"] or 0) > 0]),
            "tracked": any_tracked,
            "profitability_status": summary_analysis.profitability_status if summary_analysis else None,
            "ai_recommendation": summary_analysis.recommendation if summary_analysis else None,
            "ai_insights": summary_analysis.ai_insights if summary_analysis else None,
        },
        "platform_breakdown": platform_stats,
        "donut_data": donut_data,
    }


@router.get("/violations")
@endpoint_errors
def get_platform_violations(
    trigger_alert: bool = False,
    client_id: str | None = None,
    batch_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return live tracking of platform review rejections and policy citations from database."""
    init_flow_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            if client_id:
                require_client_access(user, client_id)
            allowed = [client_id] if client_id else list(user.assigned_client_ids)
            cur.execute(
                """
                SELECT
                    d.dispatch_id, d.ad_id, d.client_id, d.platform, d.handle,
                    COALESCE(a.headline, ''), COALESCE(a.ad_text, ''),
                    d.platform_review_notes, d.dispatched_at, d.status, d.batch_id
                FROM ad_live_dispatches d
                LEFT JOIN ads a ON d.ad_id = a.ad_id
                WHERE d.client_id = ANY(%s) AND (%s::text IS NULL OR d.batch_id = %s) AND d.status IN ('rejected', 'blocked_connector_not_configured', 'paused')
                ORDER BY d.dispatched_at DESC
                LIMIT 50;
                """,
                (allowed, batch_id, batch_id),
            )
            rows = cur.fetchall()

            contacts_map: dict[str, tuple[str, str]] = {}
            if rows:
                cids = list({r[10] for r in rows})
                cur.execute(
                    """
                    SELECT batch_id, employee_email, employee_phone
                    FROM campaign_launch_contacts
                    WHERE batch_id = ANY(%s)
                    ORDER BY created_at DESC;
                    """,
                    (cids,),
                )
                for cr in cur.fetchall():
                    if cr[0] not in contacts_map:
                        contacts_map[cr[0]] = (cr[1], cr[2])

    issue_node = IssueTrackingSpecialistNode()
    violations: list[dict[str, Any]] = []

    for r in rows:
        cid = r[2]
        contact = contacts_map.get(r[10], (None, None))
        disp_status = r[9] if len(r) > 9 and r[9] else "rejected"
        crawler_feedback = r[7] or "Dispatch failed: connector_not_configured"

        audit_res = issue_node.audit_live_status(
            ad_id=r[1],
            client_id=cid,
            platform=r[3],
            dispatch_status=disp_status,
            crawler_feedback=crawler_feedback,
            contact_email=contact[0],
            contact_phone=contact[1],
            trigger_alert=trigger_alert,
        )

        violations.append({
            "violation_id": f"viol_{r[0]}",
            "batch_id": r[10],
            "notification_contact": {"employee_email": contact[0] or "", "employee_phone": contact[1] or ""},
            "ad_id": r[1],
            "client_id": cid,
            "platform": r[3],
            "handle": r[4],
            "headline": r[5],
            "ad_text": r[6],
            "platform_rule_code": "CONNECTOR-POLICY",
            "platform_rule_name": "Connector Failure or Platform Rejection",
            "severity": audit_res.severity,
            "status": "flagged_by_platform",
            "crawler_feedback": crawler_feedback,
            "flagged_tokens": [],
            "llm_root_cause": audit_res.root_cause,
            "proposed_ai_fix": {
                "revised_headline": r[5],
                "revised_text": audit_res.proposed_fix,
            },
            "timestamp": r[8].strftime("%Y-%m-%d %H:%M") if r[8] else "recently",
            "alert_dispatched": audit_res.alert_dispatched,
            "notification_result": audit_res.notification_result,
        })

    return violations


@router.get("/launcher-contact")
@endpoint_errors
def get_launcher_contact(
    client_id: str | None = None,
    batch_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve the latest registered launcher notification contact for this client or user."""
    init_flow_tables()
    cid = client_id or (list(user.assigned_client_ids)[0] if user.assigned_client_ids else "")
    require_client_access(user, cid)

    with get_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT employee_email, employee_phone, updated_at
                FROM campaign_launch_contacts
                WHERE client_id = %s AND (%s::text IS NULL OR batch_id = %s)
                ORDER BY updated_at DESC
                LIMIT 1;
                """,
                (cid, batch_id, batch_id),
            )
            row = cur.fetchone()
            if row:
                return {
                    "client_id": cid,
                    "employee_email": row[0],
                    "employee_phone": row[1],
                    "updated_at": row[2].isoformat() if row[2] else None,
                }
            return {
                "client_id": cid,
                "employee_email": "",
                "employee_phone": "",
                "updated_at": None,
            }


@router.post("/test-alert")
@endpoint_errors
def dispatch_test_alert(
    req: ManualAlertTestRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Dispatch a test SMTP email to verify launcher contact deliverability."""
    init_flow_tables()
    client_id = req.client_id or (list(user.assigned_client_ids)[0] if user.assigned_client_ids else "")
    require_client_access(user, client_id)

    email = req.recipient_email.strip()
    phone = req.recipient_phone.strip()

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid recipient email is required.")

    # Save/update launcher contact in database
    with get_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO campaign_launch_contacts (contact_id, client_id, employee_email, employee_phone, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (contact_id) DO NOTHING;
                """,
                (f"cnt_test_{uuid.uuid4().hex[:8]}", client_id, email, phone),
            )
            conn.commit()

    # Dispatch via NotificationDispatcher
    alert_res = NotificationDispatcher.dispatch_campaign_alert(
        client_id=client_id,
        recipient_email=email,
        recipient_phone=phone,
        alert_type="manual_test",
        subject=f"Launch Gate Alert Test: Multi-Channel Contact Verification for {client_id}",
        message_body=(
            f"Launch Gate Emergency Alert Test:\n\n"
            f"Recipient Email: {email}\n"
            f"Recipient Phone: {phone}\n"
            f"Client Account: {client_id}\n"
            f"Triggered by User: {user.email}\n"
            f"Status: Emergency alert delivery has been queued for processing.\n\n"
            f"If your campaign experiences platform blockers or policy suspensions, an SMTP email alert will be routed here."
        ),
    )

    return {
        "status": "success",
        "client_id": client_id,
        "recipient_email": email,
        "recipient_phone": phone,
        "dispatch_result": alert_res,
        "email_status": alert_res.get("email_status"),
        "message": (
            "Test alert queued for delivery. "
            f"Email status: {alert_res.get('email_status')}."
        ),
    }


@router.post("/hitl-remediate")
@endpoint_errors
def hitl_remediate(
    req: HitlRemediateRequest,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Apply HITL-approved AI fix, send an SMTP alert, and clear platform rejection."""
    init_flow_tables()

    from apps.api.edits import perform_ad_edit
    from apps.api.approvals import get_ad_record
    with get_session() as conn:
        ad = get_ad_record(conn, req.ad_id)
        if not ad:
            raise HTTPException(status_code=404, detail="Ad not found.")
        require_client_access(user, ad["client_id"])
        contact = conn.execute("SELECT employee_email, employee_phone FROM campaign_launch_contacts WHERE batch_id=%s AND client_id=%s ORDER BY updated_at DESC LIMIT 1", (ad["batch_id"], ad["client_id"])).fetchone()
        email = (req.notification_email or (contact[0] if contact else "")).strip()
        if req.confirm_employee_notification and "@" not in email:
            raise HTTPException(status_code=422, detail="Enter the batch notification email before requesting an alert.")
        phone = contact[1] if contact else ""
        if req.notification_email:
            conn.execute("INSERT INTO campaign_launch_contacts (contact_id,batch_id,client_id,employee_email,employee_phone) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (batch_id,employee_email) DO UPDATE SET updated_at=NOW()", (str(uuid.uuid4()),ad["batch_id"],ad["client_id"],email,phone))
        result = perform_ad_edit(connection=conn, ad_id=req.ad_id, user=user,
                                reason="Human requested remediation and recheck",
                                headline=req.revised_headline, ad_text=req.revised_text)
    from apps.api.uploads import _dispatch_pending_jobs
    _dispatch_pending_jobs()
    notification = {"email_status": "not_requested"}
    if req.confirm_employee_notification:
        notification = NotificationDispatcher.dispatch_campaign_alert(
            client_id=ad["client_id"], recipient_email=email, recipient_phone=phone,
            alert_type="remediation_requested", subject=f"Launch Gate: Ad {req.ad_id} awaiting review",
            message_body="Revised copy was saved for worker checks and human review. No platform relaunch was performed.",
            ad_id=req.ad_id, batch_id=ad.get("batch_id"), platform=ad["platform"])
    return {**result, "status": "pending_review", "message": "Revised copy saved for checks and human approval.",
            "notification_dispatched": {"employee_email": email, "employee_phone": phone,
                                        "email_status": notification["email_status"], "dispatch_result": notification},
            "notification_result": notification}


@router.get("/all-campaigns")
@endpoint_errors
def get_all_campaigns(
    client_id: str | None = Query(None),
    batch_id: str | None = Query(None),
    include_samples: bool | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return all live and staged campaigns across clients with live revenue, status, and health indicators."""
    if not isinstance(client_id, str):
        client_id = None
    if not isinstance(batch_id, str):
        batch_id = None
    init_flow_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            allowed = list(user.assigned_client_ids)
            if include_samples is None:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM ads WHERE client_id = ANY(%s) AND is_sample = FALSE);",
                    (allowed,),
                )
                real_row = cur.fetchone()
                has_real = bool(real_row[0]) if real_row else False
                include_samples = False

            sample_filter = "" if include_samples else "a.is_sample = FALSE"
            where_clauses = ["1=1"]
            if sample_filter:
                where_clauses.append(sample_filter)
            params = []
            if client_id:
                require_client_access(user, client_id)
                where_clauses.append("a.client_id = %s")
                params.append(client_id)
            else:
                where_clauses.append("a.client_id = ANY(%s)")
                params.append(allowed)
            if batch_id:
                where_clauses.append("a.batch_id = %s")
                params.append(batch_id)

            where_str = " AND ".join(where_clauses)
            if where_str:
                where_str = "WHERE " + where_str
            else:
                where_str = ""

            cur.execute(
                f"""
                SELECT
                    a.ad_id, a.client_id, a.platform, a.headline, a.destination_url,
                    COALESCE(d.handle, 'Not configured') as handle,
                    COALESCE(d.status, 'pending_review') as delivery_status,
                    COALESCE(d.platform_review_status, 'Not submitted') as review_status,
                    p.inbound_sales_usd as sales,
                    p.spend_usd as spend,
                    p.roas as roas,
                    p.impressions as impressions,
                    p.has_rule_violation as has_violation,
                    (p.ad_id IS NOT NULL) as tracked,
                    a.is_sample,
                    p.inbound_orders as inbound_orders
                FROM ads a
                JOIN batches b ON a.batch_id = b.batch_id
                LEFT JOIN ad_live_dispatches d ON a.ad_id = d.ad_id
                LEFT JOIN ad_performance_tracking p ON a.ad_id = p.ad_id
                    AND LOWER(p.platform) = LOWER(COALESCE(d.platform, a.platform))
                {where_str}
                ORDER BY b.created_at DESC, a.row_number ASC
                LIMIT 100;
                """,
                tuple(params),
            )
            rows = cur.fetchall()

    campaigns = []
    for r in rows:
        is_tracked = bool(r[13])
        sales = float(r[8]) if (is_tracked and r[8] is not None) else None
        spend = float(r[9]) if (is_tracked and r[9] is not None) else None
        roas = float(r[10]) if (is_tracked and r[10] is not None) else None
        impressions = int(r[11]) if (is_tracked and r[11] is not None) else None
        inbound_orders = int(r[15]) if (is_tracked and len(r) > 15 and r[15] is not None) else None
        campaigns.append({
            "ad_id": r[0],
            "client_id": r[1],
            "client_name": r[1].replace("_", " ").title(),
            "platform": r[2] or "instagram",
            "headline": r[3],
            "destination_url": r[4],
            "handle": r[5],
            "delivery_status": r[6],
            "review_status": r[7],
            "inbound_sales_usd": sales,
            "spend_usd": spend,
            "roas": roas,
            "impressions": impressions,
            "inbound_orders": inbound_orders,
            "has_rule_violation": bool(r[12]) if is_tracked else False,
            "tracked": is_tracked,
            "is_sample": bool(r[14]),
        })
    return campaigns


@router.get("/revenue-analytics")
@endpoint_errors
def get_revenue_analytics(
    client_id: str | None = None,
    batch_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Return Daily, Weekly, and Monthly revenue breakdown by client across all platforms."""
    init_flow_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            allowed = list(user.assigned_client_ids)
            if client_id:
                require_client_access(user, client_id)
            if client_id:
                query_clients = [client_id]
            else:
                query_clients = allowed

            cur.execute(
                """
                SELECT
                    c.client_id,
                    SUM(p.inbound_sales_usd) as total_sales,
                    SUM(p.spend_usd) as total_spend,
                    SUM(p.inbound_orders) as total_orders,
                    COUNT(p.ad_id) as tracking_count
                FROM clients c
                LEFT JOIN ad_performance_tracking p ON c.client_id = p.client_id AND (%s::text IS NULL OR p.batch_id = %s)
                WHERE c.client_id = ANY(%s)
                GROUP BY c.client_id;
                """,
                (batch_id, batch_id, query_clients),
            )
            client_rows = cur.fetchall()

    clients_data = []
    for r in client_rows:
        cid = r[0]
        has_perf = (r[4] or 0) > 0 and r[1] is not None
        sales = float(r[1]) if has_perf else None
        spend = float(r[2]) if r[2] is not None else None
        orders = int(r[3]) if r[3] is not None else None
        roas = round(sales / spend, 2) if (has_perf and spend and spend > 0) else (0.0 if (has_perf and sales == 0.0) else None)
        clients_data.append({
            "client_id": cid,
            "client_name": cid.replace("_", " ").title(),
            "daily_sales": None,
            "weekly_sales": None,
            "monthly_sales": sales,
            "spend_usd": spend,
            "orders": orders,
            "top_platform": "Not tracked" if not has_perf else "Meta",
            "blended_roas": roas,
            "tracked": has_perf,
        })

    # Calculate time-series aggregations from recorded performance
    total_sales_all = sum(c["monthly_sales"] or 0 for c in clients_data)
    total_spend_all = sum(c["spend_usd"] or 0 for c in clients_data)

    return {
        "daily": [],
        "weekly": [],
        "monthly": [],
        "clients": clients_data,
    }


@router.get("/chat-history")
@endpoint_errors
def get_chat_history(
    request: Request,
    user: CurrentUser = Depends(get_request_user),
) -> list[dict[str, Any]]:
    """Return history owned by the verified guest session or authenticated user's existing scope."""
    init_flow_tables()
    client_ip = get_client_ip(request)
    with get_session() as conn:
        with conn.cursor() as cur:
            if user.guest_session_id:
                cur.execute(
                    """SELECT b.batch_id, COALESCE(MAX(a.client_id), ''), COUNT(DISTINCT a.ad_id), b.created_at,
                              MAX(c.employee_email), MAX(b.campaign_name), MAX(b.filename)
                       FROM batches b LEFT JOIN ads a ON b.batch_id=a.batch_id
                       LEFT JOIN campaign_launch_contacts c ON b.batch_id=c.batch_id
                       WHERE b.guest_session_id=%s AND b.is_sample=FALSE
                       GROUP BY b.batch_id,b.created_at ORDER BY b.created_at DESC LIMIT 25""",
                    (user.guest_session_id,),
                )
            else:
                cur.execute(
                """
                SELECT b.batch_id,
                       COALESCE(MAX(a.client_id), MAX(c.client_id), '') as client_id,
                       COUNT(DISTINCT a.ad_id) as ad_count,
                       b.created_at,
                       MAX(c.employee_email) as employee_email,
                       MAX(b.campaign_name), MAX(b.filename)
                FROM batches b
                LEFT JOIN ads a ON b.batch_id = a.batch_id AND a.client_id = ANY(%s) AND a.is_sample = FALSE
                LEFT JOIN campaign_launch_contacts c ON b.batch_id = c.batch_id AND c.client_id = ANY(%s)
                WHERE b.is_sample = FALSE
                  AND (b.uploader_ip = %s OR (%s IN ('127.0.0.1', '::1', 'localhost') AND b.uploader_ip IN ('127.0.0.1', '::1', 'localhost')))
                GROUP BY b.batch_id, b.created_at
                HAVING COUNT(DISTINCT a.ad_id) > 0
                ORDER BY b.created_at DESC
                LIMIT 25;
                """,
                (list(user.assigned_client_ids), list(user.assigned_client_ids), client_ip, client_ip),
                )
            rows = cur.fetchall()

    if not rows:
        return []

    return [
        {
            "batch_id": r[0],
            "title": f"{r[5] or (r[6] or 'Campaign').rsplit('.', 1)[0]} · {str(r[0])[-6:]}",
            "client_id": r[1] or "",
            "client_name": r[1].replace("_", " ").title() if r[1] else "General",
            "ad_count": int(r[2]) if r[2] else 0,
            "status": "recorded",
            "created_at": str(r[3])[:16] if r[3] else "Recent",
            "employee_email": r[4],
        }
        for r in rows
    ]


@router.get("/clients", summary="List clients for active campaigns and selector")
@endpoint_errors
def get_campaign_flow_clients(
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, str]]:
    """Return registered clients available to the user from the database."""
    init_flow_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            allowed = list(user.assigned_client_ids)
            if allowed:
                cur.execute(
                    "SELECT client_id, name FROM clients WHERE client_id = ANY(%s) ORDER BY name ASC;",
                    (allowed,),
                )
            else:
                cur.execute("SELECT client_id, name FROM clients ORDER BY name ASC;")
            rows = cur.fetchall()
    return [{"client_id": r[0], "name": r[1].replace("_", " ").title()} for r in rows]
