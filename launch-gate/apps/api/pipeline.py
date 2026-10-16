"""Interactive Dataflow Pipeline and Architecture step decoder API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from apps.api.auth import CurrentUser, get_current_user
from apps.api.database import get_session
from apps.api.platforms_schema import init_platform_tables

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/dataflow")
def get_dataflow_architecture_breakdown(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Return real-time state, throughput counts, and decoding metadata for all 12 pipeline steps."""
    init_platform_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            allowed = list(user.assigned_client_ids)

            # Batches & Ingest (N1)
            cur.execute("SELECT COUNT(DISTINCT batch_id) FROM ads WHERE client_id = ANY(%s);", (allowed,))
            batches_count = cur.fetchone()[0]

            # Total Ads
            cur.execute("SELECT COUNT(*) FROM ads WHERE client_id = ANY(%s);", (allowed,))
            total_ads = cur.fetchone()[0]

            # Spans / Findings (N2, N3, N4)
            cur.execute(
                """
                SELECT check_type, COUNT(*)
                FROM findings f
                JOIN ads a ON f.ad_id = a.ad_id
                WHERE a.client_id = ANY(%s)
                GROUP BY check_type;
                """,
                (allowed,),
            )
            findings_by_type = {r[0]: int(r[1]) for r in cur.fetchall()}

            # Approvals (N5 / Review)
            cur.execute(
                """
                SELECT state, COUNT(*)
                FROM ad_approvals
                WHERE client_id = ANY(%s)
                GROUP BY state;
                """,
                (allowed,),
            )
            approvals_map = {r[0]: int(r[1]) for r in cur.fetchall()}

            # Dispatches (Platform Live)
            cur.execute(
                """
                SELECT COUNT(*)
                FROM ad_live_dispatches
                WHERE client_id = ANY(%s);
                """,
                (allowed,),
            )
            dispatches_count = cur.fetchone()[0]

            # Attribution & Sales
            cur.execute(
                """
                SELECT COALESCE(SUM(inbound_sales_usd), 0.0),
                       COALESCE(SUM(inbound_orders), 0),
                       COALESCE(COUNT(*) FILTER (WHERE has_rule_violation = TRUE), 0)
                FROM ad_performance_tracking
                WHERE client_id = ANY(%s);
                """,
                (allowed,),
            )
            sales_row = cur.fetchone()
            total_sales = float(sales_row[0])
            total_orders = int(sales_row[1])
            violations_count = int(sales_row[2])

            # Connected Social Accounts
            cur.execute(
                """
                SELECT platform, COUNT(*)
                FROM platform_accounts
                WHERE client_id = ANY(%s)
                GROUP BY platform;
                """,
                (allowed,),
            )
            accounts_by_plat = {r[0]: int(r[1]) for r in cur.fetchall()}

    # Decode each step matching docs/Launch_Gate_Dataflow_Architecture.md
    steps = [
        {
            "step_id": "N1",
            "name": "Ingest & Normalize",
            "tag": "[B R1], [D5]",
            "category": "ingest",
            "status": "active",
            "engine": "Python CSV/XLSX Parser + Hasher",
            "description": "Parses messy sheets (CSV/XLSX), normalizes column aliases, isolates invalid rows, and deduplicates sheets by SHA-256 hash.",
            "metrics": {
                "batches_ingested": batches_count,
                "total_rows_parsed": total_ads,
            },
            "icon": "upload",
            "accent": "blue",
        },
        {
            "step_id": "QUEUE",
            "name": "Redis Job Distribution",
            "tag": "[N] Redis",
            "category": "queue",
            "status": "active",
            "engine": "Redis Queue (launch_gate:ad_jobs)",
            "description": "Distributes individual ad verification tasks to concurrent worker pool with zero race conditions.",
            "metrics": {
                "dispatched_jobs": total_ads,
                "fan_out_concurrency": 4,
            },
            "icon": "layers",
            "accent": "indigo",
        },
        {
            "step_id": "N2",
            "name": "Mechanical Checks",
            "tag": "[B R2]",
            "category": "compliance",
            "status": "active",
            "engine": "Deterministic Code (Regex & Config)",
            "description": "Verifies UTM query syntax & client casing, enforces platform character limits (Instagram/TikTok/Meta/X), checks creative aspect ratio.",
            "metrics": {
                "findings_flagged": findings_by_type.get("utm_naming", 0) + findings_by_type.get("char_limit", 0) + findings_by_type.get("creative_ratio", 0),
                "checks_run": ["UTM syntax", "Text limits", "Headline limits", "Creative size/ratio"],
            },
            "icon": "cpu",
            "accent": "cyan",
        },
        {
            "step_id": "N3",
            "name": "Page Offer & Snapshot",
            "tag": "[B R3]",
            "category": "page_verification",
            "status": "active",
            "engine": "Headless Browser (Read-only sandbox)",
            "description": "Loads destination landing pages, confirms UTM parameters survive redirects, extracts prices/discounts/expiry dates to match ad claims.",
            "metrics": {
                "findings_flagged": findings_by_type.get("page_offer", 0) + findings_by_type.get("utm_redirect", 0),
                "sandbox_mode": "Strict read-only (forms & purchases blocked)",
            },
            "icon": "globe",
            "accent": "emerald",
        },
        {
            "step_id": "N4",
            "name": "Wording & Brand Rule Retrieval",
            "tag": "[B R4], [D6]",
            "category": "semantic_ai",
            "status": "active",
            "engine": "RAG Vector Store + Google Gemini / Heuristic",
            "description": "Strictly retrieves brand book and claims register entries for THIS client only. Verifies rule citations and detects forbidden claims.",
            "metrics": {
                "findings_flagged": findings_by_type.get("wording", 0),
                "model_status": "Google Gemini (gemini-flash-latest) + Heuristic Fallback",
            },
            "icon": "sparkles",
            "accent": "purple",
        },
        {
            "step_id": "N5",
            "name": "Verdict Engine",
            "tag": "[B R5]",
            "category": "verdict",
            "status": "active",
            "engine": "State Aggregator & Spend Cap Guard",
            "description": "Computes final ad health: ready, fix_needed, blocked, or needs_person. Enforces $0.01 per-row budget cap.",
            "metrics": {
                "ready_count": approvals_map.get("approved", 0),
                "pending_review": approvals_map.get("pending_review", 0),
                "rejected_count": approvals_map.get("rejected", 0),
            },
            "icon": "shield-check",
            "accent": "amber",
        },
        {
            "step_id": "APPROVAL",
            "name": "Human Approval Gate",
            "tag": "[B R6], [D13]",
            "category": "human_gate",
            "status": "active",
            "engine": "Cryptographic Hash Lock (SHA-256)",
            "description": "Requires human reviewer authorization with client approval evidence attached. Locks ad payload with immutable hash.",
            "metrics": {
                "total_approved": approvals_map.get("approved", 0) + approvals_map.get("sent", 0),
                "evidence_required": True,
            },
            "icon": "user-check",
            "accent": "emerald",
        },
        {
            "step_id": "DISPATCH",
            "name": "Social Handle Automated Login & Launch",
            "tag": "[B R6], [B R8]",
            "category": "dispatch_agent",
            "status": "active",
            "engine": "Automated Multi-Platform Dispatch Agent",
            "description": "Authenticates into client handles (Instagram, Facebook, TikTok, Snapchat, X), provisions campaign parameters, and pushes to platform gateway.",
            "metrics": {
                "connected_handles": accounts_by_plat,
                "campaigns_dispatched": dispatches_count,
            },
            "icon": "send",
            "accent": "pink",
        },
        {
            "step_id": "REVIEW",
            "name": "Platform Policy Review Status",
            "tag": "[B R6]",
            "category": "platform_review",
            "status": "active",
            "engine": "Live Platform Bot Verification",
            "description": "Receives real-time feedback from platform review bots (Meta Brand Safety, TikTok Ad Review, X Quality Score).",
            "metrics": {
                "active_live": dispatches_count - violations_count,
                "policy_warnings": violations_count,
            },
            "icon": "activity",
            "accent": "blue",
        },
        {
            "step_id": "ATTRIBUTION",
            "name": "Inbound Sales & Attribution",
            "tag": "[B R7]",
            "category": "sales_attribution",
            "status": "active",
            "engine": "UTM-Attributed Conversion Tracker",
            "description": "Continuously tracks verified customer purchases, inbound orders, revenue, ROAS, and click-through rates generated by live ads.",
            "metrics": {
                "total_inbound_sales_usd": total_sales,
                "total_orders": total_orders,
            },
            "icon": "dollar-sign",
            "accent": "emerald",
        },
        {
            "step_id": "ALERTS",
            "name": "Policy Alerts & Reconciler",
            "tag": "[B R6], [PRD §4.6]",
            "category": "alerts",
            "status": "active",
            "engine": "Dead-Letter & Alert Event Dispatcher",
            "description": "Raises high-priority notifications for ads flagged by platforms post-launch or stuck in dead-letter queues, enabling immediate review.",
            "metrics": {
                "active_alerts": violations_count,
            },
            "icon": "alert-triangle",
            "accent": "red",
        },
    ]

    return {
        "architecture_version": "2.0",
        "total_pipeline_steps": len(steps),
        "steps": steps,
        "summary": {
            "total_ads_tracked": total_ads,
            "total_sales_generated_usd": total_sales,
            "connected_platforms_count": len(accounts_by_plat),
            "clean_compliance_rate": f"{round(((total_ads - violations_count) / total_ads * 100), 1) if total_ads > 0 else 100}%",
        },
    }
