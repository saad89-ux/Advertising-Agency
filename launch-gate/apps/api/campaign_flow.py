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
from apps.api.notifications import NotificationDispatcher
from apps.api.client_ip import get_client_ip
from apps.api.ingest import hash_normalized_row, hash_normalized_sheet
from apps.api.platforms_schema import init_platform_tables
from apps.worker.agent_team import IssueTrackingSpecialistNode, RevenueTrackingSpecialistNode
from apps.worker.mcp_client import MCPPlatformDispatcher
from packages.rules.platform_normalization import normalize_platform

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaign-flow", tags=["campaign-flow"])


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
"""


def init_flow_tables():
    init_platform_tables()
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


class LaunchBatchRequest(BaseModel):
    client_id: str = ""
    batch_id: str | None = None
    campaign_name: str = "Multi-Platform Growth Burst"
    employee_email: str
    employee_phone: str = ""
    target_platforms: list[str] = Field(min_length=1, description="At least one target platform must be selected")
    headline: str | None = None
    ad_text: str | None = None
    destination_url: str | None = None
    creative_file: str | None = None


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
    client_id = resolve_campaign_client(user, req.client_id, req.batch_id,
                 str(req.ad_overrides.get("destination_url") or ""), req.input_text)

    if background:
        from apps.api.review_jobs import submit
        fingerprint = req.model_dump()
        # Invalidate cached checks when persisted ad content changes.
        if req.batch_id:
            with get_session() as conn:
                fingerprint["content_versions"] = conn.execute(
                    "SELECT ad_id, row_hash FROM ads WHERE batch_id=%s AND client_id=%s ORDER BY ad_id",
                    (req.batch_id, client_id)).fetchall()
        return submit(user.user_id, fingerprint, lambda: preflight_review(req, user=user, background=False))

    raw_text = (req.input_text or "").strip()
    overrides = req.ad_overrides or {}

    if req.batch_id:
        from packages.contracts import AdContract
        from apps.api.preflight import review_ad
        with get_session() as conn:
            rows = conn.execute("SELECT ad_id, platform, headline, ad_text, destination_url, creative_file, row_hash, sheet_hash, row_number FROM ads WHERE batch_id = %s AND client_id = %s", (req.batch_id, client_id)).fetchall()
        if not rows:
            raise HTTPException(status_code=422, detail="No ads for this client in the uploaded batch.")
        checks = []
        for row in rows:
            ad = AdContract(ad_id=row[0], batch_id=req.batch_id, client_id=client_id, platform=row[1], headline=row[2], ad_text=row[3], destination_url=row[4], creative_file=row[5] or None, row_hash=row[6], sheet_hash=row[7], row_number=row[8])
            checks.extend({**check, "ad_id": ad.ad_id, "platform": ad.platform} for check in review_ad(ad))
        return {"status": "checks_failed" if any(c["status"] == "fail" for c in checks) else "ready_for_review", "checks": checks, "missing_items": [], "campaign_summary": {}, "client_id": client_id}

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
    return {"status": "checks_failed" if any(c["status"] == "fail" for c in checks) else "ready_for_review",
            "client_id": client_id, "checks": checks, "missing_items": [],
            "campaign_summary": {"headline": headline, "ad_text": ad_text,
                                 "destination_url": dest_url, "creative_file": overrides.get("creative_file")},
            "message": "Review the recorded checks before authorizing. Unavailable checks require human review."}


@router.post("/launch-batch", status_code=status.HTTP_201_CREATED)
@endpoint_errors
def launch_batch(
    req: LaunchBatchRequest,
    request: Request = None,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Launch a batch to Meta, record the employee contact, and trigger dispatches."""
    init_flow_tables()
    req.client_id = resolve_campaign_client(user, req.client_id, req.batch_id, req.destination_url or "", req.ad_text or req.headline or "")

    if not req.employee_email or "@" not in req.employee_email:
        raise HTTPException(status_code=400, detail="A valid employee email is required for notification tracking.")
    launcher_phone = ""

    normalized_platforms: list[str] = []
    for raw_p in req.target_platforms:
        if not raw_p or not str(raw_p).strip():
            raise HTTPException(status_code=400, detail="Platform name cannot be empty.")
        norm = normalize_platform(str(raw_p))
        if norm.flagged or not norm.canonical_value:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported platform '{raw_p}'. Launch Gate currently supports Meta only (Facebook and Instagram).",
            )
        if norm.canonical_value not in {"facebook", "instagram"}:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported platform '{raw_p}'. Launch Gate currently supports Meta only (Facebook and Instagram).",
            )
        if norm.canonical_value not in normalized_platforms:
            normalized_platforms.append(norm.canonical_value)

    if not normalized_platforms:
        raise HTTPException(status_code=400, detail="At least one valid target platform must be selected.")

    batch_id = req.batch_id or f"batch_{req.client_id[:3]}_{uuid.uuid4().hex[:8]}"
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
                "SELECT ad_id, headline, ad_text, destination_url, row_hash, creative_file, platform FROM ads WHERE batch_id = %s AND client_id = %s;",
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
                ads_list = [(generated_ad_id, ad_headline, ad_text, dest_url, hash_normalized_row(canonical), req.creative_file, normalized_platforms[0])]

            # Initialize MCP platform tool dispatcher
            mcp_dispatcher = MCPPlatformDispatcher()
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
                cur.execute(
                    """
                    INSERT INTO ad_approvals (ad_id, client_id, state)
                    VALUES (%s, %s, 'pending_review')
                    ON CONFLICT (ad_id) DO NOTHING;
                    """,
                    (ad_id, req.client_id),
                )
                cur.execute(
                    """
                    UPDATE ad_approvals
                    SET state = 'approved',
                        approved_by = %s,
                        approved_at = NOW(),
                        content_hash = %s,
                        updated_at = NOW()
                    WHERE ad_id = %s AND state IN ('pending_review', 'blocked_connector_not_configured', 'dead_letter');
                    """,
                    (
                        user.user_id,
                        ad_row[4],
                        ad_id,
                    ),
                )
                cur.execute("SELECT state, content_hash FROM ad_approvals WHERE ad_id = %s", (ad_id,))
                approval = cur.fetchone()
                if not approval or approval[0] not in {'approved', 'sent'} or approval[1] != ad_row[4]:
                    raise HTTPException(status_code=409, detail="The current ad content requires a recorded human approval before dispatch.")
                cur.execute("INSERT INTO approval_history (ad_id, from_state, to_state, changed_by, content_hash, reason) VALUES (%s,%s,%s,%s,%s,%s)",
                            (ad_id, 'pending_review', 'approved', user.user_id, ad_row[4], 'Explicit campaign authorization'))
                cur.execute("UPDATE ad_approvals SET content_snapshot = %s WHERE ad_id = %s", (json.dumps(canonical, sort_keys=True), ad_id))
                conn.commit()
                for plat in normalized_platforms:
                    disp_id = f"disp_{ad_id}_{plat}_{ad_row[4][:16]}"
                    handle = f"@{req.client_id}_{plat}"
                    cur.execute("SELECT status FROM ad_live_dispatches WHERE dispatch_id = %s", (disp_id,))
                    previous_dispatch = cur.fetchone()
                    if previous_dispatch and previous_dispatch[0] in {'active_live', 'paused', 'pending_review', 'scheduled'}:
                        dispatch_results.append({"ad_id": ad_id, "platform": plat, "status": "sent", "error": None})
                        continue

                    # Execute MCP tool call
                    mcp_resp = mcp_dispatcher.call_tool(
                        platform=plat,
                        tool_name=f"{plat}_dispatch_ad",
                        arguments={
                            "ad_id": ad_id,
                            "headline": headline,
                            "ad_text": ad_row[2],
                            "creative_file": ad_row[5],
                            "idempotency_key": disp_id,
                            "destination_url": destination_url,
                            "handle": handle,
                        },
                    )

                    # Check success on every connector call; on failure do not write fake rows
                    if not mcp_resp.success or not (mcp_resp.result or {}).get("platform_ad_id"):
                        error_msg = mcp_resp.error or "Platform did not confirm an ad ID. Delivery is unconfirmed."
                        is_unconfigured = error_msg in {"connector_not_configured", "real_platform_calls_disabled"}
                        disp_status = "blocked_connector_not_configured" if is_unconfigured else "rejected"
                        if error_msg == "real_platform_calls_disabled":
                            review_status = "Blocked: Live Writes Disabled"
                        elif error_msg == "connector_not_configured":
                            review_status = "Blocked: Connector Not Configured"
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
                                    platform=plat,
                                )
                                notifications_dispatched.append(alert_res)
                                cur.execute("INSERT INTO send_alerts (alert_id,ad_id,client_id,batch_id,alert_type,severity,message,details) VALUES (%s,%s,%s,%s,%s,'warning',%s,%s)",
                                            (str(uuid.uuid4()), ad_id, req.client_id, actual_batch_id, 'launch_blocked' if is_unconfigured else 'platform_rejection', review_notes, json.dumps(alert_res)))
                            except Exception as notif_err:
                                logger.exception("Failed to dispatch alert for ad %s: %s", ad_id, notif_err)

                        cur.execute(
                            """
                            INSERT INTO ad_live_dispatches (
                                dispatch_id, ad_id, batch_id, client_id, platform, handle,
                                status, platform_ad_id, platform_review_status, platform_review_notes, live_preview_url
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                            ON CONFLICT (dispatch_id) DO UPDATE SET
                                status = EXCLUDED.status,
                                platform_ad_id = EXCLUDED.platform_ad_id,
                                platform_review_status = EXCLUDED.platform_review_status,
                                platform_review_notes = EXCLUDED.platform_review_notes,
                                last_tracked_at = NOW();
                            """,
                            (disp_id, ad_id, actual_batch_id, req.client_id, plat, handle, disp_status, disp_id, review_status, review_notes),
                        )

                        if not is_unconfigured:
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
                            status, platform_ad_id, platform_review_status, platform_review_notes, live_preview_url
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (dispatch_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            platform_review_status = EXCLUDED.platform_review_status,
                            platform_review_notes = EXCLUDED.platform_review_notes,
                            last_tracked_at = NOW();
                        """,
                        (disp_id, ad_id, actual_batch_id, req.client_id, plat, handle, platform_status, plat_ad_id, platform_review, audit_notes, live_url),
                    )

                    cur.execute("INSERT INTO ad_sends (send_id,ad_id,client_id,batch_id,idempotency_key,content_hash,platform,status,platform_ad_id,is_dry_run) VALUES (%s,%s,%s,%s,%s,%s,%s,'sent',%s,FALSE) ON CONFLICT (send_id) DO NOTHING",
                                (f"send_{disp_id}",ad_id,req.client_id,actual_batch_id,disp_id,ad_row[4],plat,plat_ad_id))
                    perf_resp = mcp_dispatcher.call_tool(
                        platform=plat,
                        tool_name=f"{plat}_track_performance",
                        arguments={"platform_ad_id": plat_ad_id},
                    )
                    if perf_resp.success:
                        perf_res = perf_resp.result
                        cur.execute(
                            """
                            INSERT INTO ad_performance_tracking (
                                ad_id, batch_id, client_id, platform, handle,
                                inbound_sales_usd, inbound_orders, impressions, clicks, ctr_percent, roas, spend_usd,
                                has_rule_violation, violation_details, last_event
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, NULL, %s)
                            ON CONFLICT (ad_id) DO UPDATE SET
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
                                perf_res.get("inbound_sales_usd"),
                                perf_res.get("inbound_orders"),
                                perf_res.get("impressions"),
                                perf_res.get("clicks"),
                                perf_res.get("ctr_percent"),
                                perf_res.get("roas"),
                                perf_res.get("spend_usd"),
                                f"MCP tracking active (RPC: {perf_resp.rpc_id})",
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
            "tool": f"{r[2]}_dispatch_ad",
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
                WHERE client_id = ANY(%s) {sample_clause}
                GROUP BY status;
                """,
                (allowed,),
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
            f"Status: Emergency alert notification delivery channel verified.\n\n"
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
            "Test alert dispatched successfully. "
            f"SMTP Email Status: {alert_res.get('email_status')}."
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
            ad_id=req.ad_id, platform=ad["platform"])
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
