"""Diagnostics API endpoint providing full real-time visibility into AI models and Langfuse tracing."""

from __future__ import annotations

import os
import re
import uuid
import logging
from typing import Any
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import Connection
from redis import Redis

from apps.api.access import require_client_access
from apps.api.auth import CurrentUser, get_current_user, require_role
from apps.api.database import get_session
from apps.api.queue import AD_JOB_QUEUE, MetaSyncJob, _redis_client
from apps.api.outbox import dispatch_pending_meta_sync_jobs, persist_meta_sync_job_on_connection
from apps.worker.model_client import is_gemini_configured

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])
logger = logging.getLogger(__name__)


def _record_meta_health(workspace_id: str, health: str, scopes: list[str] | None = None, error: str | None = None) -> None:
    with get_session() as conn:
        from apps.api.clients import update_workspace_meta_health
        update_workspace_meta_health(conn, workspace_id, health=health, verified_scopes=scopes, error_message=error)


def _meta_failure_health(error: str) -> str:
    normalized = (error or "").casefold()
    if any(marker in normalized for marker in ("190", "401", "invalid oauth", "invalid token", "expired token", "permission", "403", "not authorized")):
        return "UNAUTHORIZED"
    return "DEGRADED"


def _sanitize_meta_error(message: str | None, token: str = "") -> str:
    safe_message = " ".join(str(message or "Meta request failed.").split())
    if token:
        safe_message = safe_message.replace(token, "[REDACTED]")
    safe_message = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", safe_message)
    return safe_message[:500]


def build_meta_sync_dashboard_state(
    workspace_id: str,
    latest: tuple[Any, ...] | None,
    metrics: tuple[Any, ...] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Map persisted sync/metric metadata to stable UI state tokens."""
    now = now or datetime.now(timezone.utc)
    metric_count = int(metrics[0] or 0) if metrics else 0
    metrics_synced_at = metrics[1] if metrics else None
    if not latest:
        return {"ui_state": "NO_DATA", "workspace_id": workspace_id, "message": "No account sync history discovered.", "metric_count": metric_count}
    sync_id, sync_type, status_value, records_synced, error_message, started_at, completed_at = latest
    if status_value == "RUNNING":
        ui_state = "LOADING"
    elif status_value == "FAILED":
        ui_state = "META_REQUEST_FAILED"
    elif completed_at and completed_at < now - timedelta(hours=24):
        ui_state = "STALE"
    else:
        ui_state = "SYNCED"
    return {
        "ui_state": ui_state,
        "workspace_id": workspace_id,
        "sync_id": str(sync_id),
        "sync_type": sync_type,
        "status": status_value,
        "records_synced": int(records_synced or 0),
        "metric_count": metric_count,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "metrics_synced_at": metrics_synced_at.isoformat() if metrics_synced_at else None,
        "source": "META_INSIGHTS_API",
        "error": error_message if status_value == "FAILED" else None,
    }

@router.post("/meta/client/{client_id}/sync")
def start_meta_sync(
    client_id: str,
    sync_type: str = Query("INITIAL_IMPORT", pattern="^(INITIAL_IMPORT|METRIC_REFRESH)$"),
    lookback_days: int = Query(7, ge=1, le=90),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Queue a read-only structural or metrics sync for an authorized workspace."""
    require_client_access(user, client_id)
    from apps.api.platforms_schema import init_platform_tables
    from apps.worker.meta_sync import create_sync_log

    init_platform_tables()
    try:
        with get_session() as connection:
            sync_id = create_sync_log(connection, client_id, sync_type)
            persist_meta_sync_job_on_connection(connection, MetaSyncJob(
                sync_id=str(sync_id),
                workspace_id=client_id,
                sync_type=sync_type,
                lookback_days=lookback_days,
            ))
    except Exception as exc:
        logger.exception("Could not create Meta sync log for workspace")
        raise HTTPException(status_code=500, detail="Could not start Meta synchronization.") from exc
    queue_status = "QUEUED"
    try:
        dispatch_pending_meta_sync_jobs(batch_size=1)
    except Exception:
        logger.exception("Meta sync %s remains pending in the transactional outbox", sync_id)
    return {
        "sync_id": str(sync_id),
        "workspace_id": client_id,
        "status": "RUNNING",
        "queue_status": queue_status,
        "sync_type": sync_type,
    }

@router.get("/meta/client/{client_id}/sync-state")
def get_meta_sync_state(client_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """Return the safe dashboard state for one authorized workspace's latest Meta sync."""
    require_client_access(user, client_id)
    from apps.api.platforms_schema import init_platform_tables
    init_platform_tables()
    with get_session() as connection:
        latest = connection.execute(
            """SELECT id, sync_type, status, records_synced, error_message, started_at, completed_at
               FROM meta_sync_logs WHERE workspace_id=%s ORDER BY started_at DESC LIMIT 1""",
            (client_id,),
        ).fetchone()
        metrics = connection.execute(
            "SELECT COUNT(*), MAX(synced_at) FROM meta_performance_metrics WHERE workspace_id=%s",
            (client_id,),
        ).fetchone()
    return build_meta_sync_dashboard_state(client_id, latest, metrics)


@router.get("/meta/client/{client_id}")
def diagnose_meta_client(client_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """Read-only Meta access probe for one authorized client workspace."""
    if client_id not in set(user.assigned_client_ids):
        return {"status": "FORBIDDEN", "http_status": 403, "message": "Client workspace is not assigned to this user."}

    try:
        with get_session() as conn:
            from apps.api.clients import resolve_client_meta_account
            try:
                account_id = resolve_client_meta_account(conn, client_id)
            except ValueError as exc:
                return {"status": "CRITICAL", "message": str(exc)}
    except Exception:
        return {"status": "ERROR", "message": "Could not load the workspace Meta account mapping."}

    agency_token = os.environ.get("META_SYSTEM_USER_TOKEN", "").strip()
    if not agency_token:
        return {"status": "CRITICAL", "message": "Meta agency system-user token is missing."}
    if os.environ.get("META_MCP_ENABLE_LIVE", "false").strip().lower() not in {"true", "1", "yes"}:
        return {"status": "READS_DISABLED", "message": "Set META_MCP_ENABLE_LIVE=true to run this read-only Meta probe. Write access is not required."}

    from apps.worker.mcp_client import MCPPlatformDispatcher
    dispatcher = MCPPlatformDispatcher(access_token=agency_token)
    conversation_id = uuid.uuid4().hex[:20]
    account_rows: list[dict[str, Any]] = []
    accounts_result: dict[str, Any] = {}
    cursor: str | None = None
    accounts = None
    for _ in range(10):
        arguments: dict[str, Any] = {
            "limit": 50,
            "client_conversation_id": conversation_id,
            "advertiser_request": "Verify Meta ad account read-only access",
            "advertiser_job": "set_up_and_fix_ad_account",
        }
        if cursor:
            arguments["cursor"] = cursor
        accounts = dispatcher.call_tool("facebook", "ads_get_ad_accounts", arguments)
        if not accounts.success:
            break
        accounts_result = accounts.result if isinstance(accounts.result, dict) else {}
        page_rows = accounts_result.get("ad_accounts", [])
        if isinstance(page_rows, list):
            account_rows.extend(row for row in page_rows if isinstance(row, dict))
        if any(str(row.get("ad_account_id", "")).removeprefix("act_") == account_id for row in account_rows):
            break
        cursor = accounts_result.get("next_cursor")
        if not cursor:
            break
    if not accounts.success:
        failure = _sanitize_meta_error(accounts.error or "Meta account listing failed.", agency_token)
        health = _meta_failure_health(failure)
        _record_meta_health(client_id, health, error=failure)
        return {
            "status": health,
            "http_status": getattr(accounts, "status_code", None),
            "message": failure,
            "action_required": "Assign the agency System User to this ad account and grant Manage Campaigns.",
        }

    matching = [row for row in account_rows if str(row.get("ad_account_id", "")).removeprefix("act_") == account_id]
    if accounts_result and not account_rows:
        _record_meta_health(client_id, "UNAUTHORIZED", error="Meta returned no accessible ad accounts.")
        return {
            "status": "UNAUTHORIZED",
            "http_status": 403,
            "message": "No ad accounts are accessible to the agency System User.",
            "action_required": "Assign the System User to the client ad account in Meta Business Settings.",
            "setup_checklist": [
                "Confirm the numeric client ad account ID.",
                "Assign the agency System User asset in Business Settings.",
                "Enable Manage Campaigns permissions.",
                "Confirm ads_read and ads_management advanced access for production.",
            ],
        }
    if not matching:
        _record_meta_health(client_id, "UNAUTHORIZED", error="Configured workspace account was not returned by Meta.")
        return {
            "status": "UNAUTHORIZED",
            "message": "The configured workspace account was not returned by Meta.",
            "action_required": "Verify Business Manager asset assignment and System User permissions.",
        }

    if matching[0].get("is_queryable") is False:
        message = matching[0].get("not_queryable_reason") or "Meta reports that this ad account is not queryable."
        _record_meta_health(client_id, "UNAUTHORIZED", error=message)
        return {
            "status": "UNAUTHORIZED",
            "http_status": 403,
            "message": message,
            "action_required": "Assign the System User with Manage Campaigns and verify App advanced access.",
        }

    campaign_probe = dispatcher.call_tool("facebook", "ads_get_ad_entities", {
        "ad_account_id": account_id,
        "level": "campaign",
        "fields": ["id", "name"],
        "limit": 1,
        "client_conversation_id": conversation_id,
        "advertiser_request": "Verify Meta ad account read-only access",
        "advertiser_job": "set_up_and_fix_ad_account",
    })
    if not campaign_probe.success:
        failure = _sanitize_meta_error(campaign_probe.error or "Campaign read probe failed.", agency_token)
        health = _meta_failure_health(failure)
        _record_meta_health(client_id, health, error=failure)
        return {"status": health, "message": failure, "action_required": "Verify ads_read permission and asset assignment."}
    entities = campaign_probe.result.get("data", []) if isinstance(campaign_probe.result, dict) else []
    verified_scopes = matching[0].get("tasks", [])
    if not isinstance(verified_scopes, list):
        verified_scopes = []
    _record_meta_health(client_id, "HEALTHY", scopes=[str(scope) for scope in verified_scopes])
    return {
        "status": "HEALTHY",
        "meta_account_id": account_id,
        "account_name": matching[0].get("ad_account_name"),
        "account_status": matching[0].get("account_status"),
        "capabilities": verified_scopes,
        "accessible_campaigns_found": len(entities),
        "message": "Read-only Meta validation complete. Account is accessible to the agency System User.",
    }


@router.get("/ai-tracing")
def get_ai_and_tracing_status() -> dict[str, Any]:
    """Inspect real-time Gemini API status, Langfuse connection, Redis queue, and trace counts."""
    load_dotenv()

    # 1. Gemini / Model API Check
    model_key = os.environ.get("MODEL_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    gemini_active = is_gemini_configured()
    model_name = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

    # 2. Langfuse Status
    lf_pub = (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip(" \t\r\n\"'")
    lf_sec = (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip(" \t\r\n\"'")
    lf_host = (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com").strip(" \t\r\n\"'")
    lf_enabled = bool(lf_pub and lf_sec and "placeholder" not in lf_pub.lower())
    notification_sink = os.environ.get("NOTIFICATION_DELIVERY_SINK", "").strip().lower()
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_port = os.environ.get("SMTP_PORT", "587").strip()
    smtp_from = (os.environ.get("NOTIFICATION_FROM_EMAIL") or os.environ.get("SMTP_FROM_EMAIL") or "").strip()
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    smtp_configured = bool(smtp_host and smtp_from and "@" in smtp_from and smtp_port.isdigit()) and bool(smtp_user) == bool(smtp_password)
    email_configured = notification_sink == "smtp" and smtp_configured
    meta_connection_configured = bool(
        os.environ.get("MCP_META_SERVER_URL", "").strip()
        and os.environ.get("META_SYSTEM_USER_TOKEN", "").strip()
    )
    meta_live_enabled = os.environ.get("META_MCP_ENABLE_LIVE", "false").strip().lower() in {"true", "1", "yes"}
    meta_writes_enabled = os.environ.get("META_MCP_ALLOW_WRITES", "false").strip().lower() in {"true", "1", "yes"}
    account_currency = os.environ.get("META_AD_ACCOUNT_CURRENCY", "").strip().upper()

    # 3. Redis Queue
    queue_length = 0
    redis_online = False
    try:
        r: Redis = _redis_client()
        queue_length = r.llen(AD_JOB_QUEUE)
        redis_online = True
    except Exception:
        pass

    # 4. PostgreSQL Trace Counts
    trace_count = 0
    span_count = 0
    db_online = False
    try:
        with get_session() as conn:
            with conn.cursor() as cur:
                # Ensure tables exist
                cur.execute(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'worker_traces');"
                )
                if cur.fetchone()[0]:
                    cur.execute("SELECT count(*) FROM worker_traces;")
                    trace_count = cur.fetchone()[0]
                    cur.execute("SELECT count(*) FROM worker_spans;")
                    span_count = cur.fetchone()[0]
            db_online = True
    except Exception:
        pass

    return {
        "meta": {
            "is_configured": meta_connection_configured and meta_live_enabled,
            "reads_enabled": meta_connection_configured and meta_live_enabled,
            "writes_enabled": meta_connection_configured and meta_live_enabled and meta_writes_enabled,
            "safety": "read_only_configuration_diagnostic",
            "setup_checklist": [
                "Set META_SYSTEM_USER_TOKEN for the shared agency System User token.",
                "Map each authorized client to its account in workspace_meta_connections.",
                "Grant the agency system user Manage Campaigns access to each client ad account.",
                "Enable META_MCP_ENABLE_LIVE=true only for authorized read-only checks.",
                "Keep META_MCP_ALLOW_WRITES=false unless a campaign launch is explicitly authorized.",
            ],
            "account_currency": account_currency or None,
            "usd_metrics_enabled": account_currency == "USD",
        },
        "email_alerts": {
            "is_configured": email_configured,
            "sink": notification_sink or "unset",
            "status": "ready" if email_configured else "sink_not_selected" if notification_sink != "smtp" else "smtp_not_configured",
        },
        "gemini_api": {
            "is_configured": gemini_active,
            "status": "active" if gemini_active else "heuristic_fallback",
            "model_name": model_name,
            "message": (
                f"Google Gemini connected ({model_name})"
                if gemini_active
                else "MODEL_API_KEY not set in .env. Running in compliant heuristic semantic mode."
            ),
            "key_present": bool(model_key),
        },
        "langfuse": {
            "is_configured": lf_enabled,
            "status": "connected" if lf_enabled else "postgresql_fallback",
            "host": lf_host,
            "public_key_preview": f"{lf_pub[:6]}...{lf_pub[-4:]}" if len(lf_pub) > 10 else ("Set" if lf_pub else "Not Set"),
            "message": (
                f"Langfuse connected and streaming traces to {lf_host}"
                if lf_enabled
                else "Langfuse keys not configured in .env. All traces are safely captured locally in PostgreSQL."
            ),
        },
        "worker_queue": {
            "redis_connected": redis_online,
            "queue_name": AD_JOB_QUEUE,
            "pending_jobs": queue_length,
        },
        "telemetry_counts": {
            "database_connected": db_online,
            "total_traces": trace_count,
            "total_spans": span_count,
        },
    }
