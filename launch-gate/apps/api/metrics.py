"""Metrics Aggregation & Reporting API (PRD §4.7, CLAUDE.md §4.7).

Aggregates operational, quality, reviewer, financial, and release gate metrics:
1. Problems by type: findings grouped by check_type (mechanical, page, wording) and rule_id.
2. Ads blocked: counts and percentages of ads blocked or requiring fix.
3. Reviewer time: average, median, min, max seconds spent in review per ad and batch.
4. False alarms: reviewer-dismissed findings count, false-alarm rate, and rule breakdown.
5. Cost per batch: model and tool execution cost in USD, step counts, and cost per ad.
6. Simulated rejection rates: platform failure rate without the gate vs. with the gate.
7. Scoped strictly to the user's assigned clients (Needs: 7, PRD §4.8).
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apps.api.access import require_client_access
from apps.api.auth import CurrentUser, get_current_user
from apps.api.batches import (
    get_ads_findings,
    get_ads_verdicts,
    get_batch_ads,
    get_batch_record,
    get_batch_rejected_rows,
)
from apps.api.database import get_session
from apps.api.findings import get_dismissed_findings_map

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])


# --- Calculation Helpers ---

def calculate_reviewer_time_stats(
    approvals: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate reviewer time summary statistics (average, median, min, max seconds)."""
    durations: list[float] = []

    for appr in approvals:
        created_str = appr.get("created_at")
        action_str = appr.get("approved_at") or appr.get("rejected_at") or appr.get("updated_at")

        if not created_str or not action_str:
            continue

        try:
            created_dt = (
                datetime.fromisoformat(str(created_str))
                if isinstance(created_str, str)
                else created_str
            )
            action_dt = (
                datetime.fromisoformat(str(action_str))
                if isinstance(action_str, str)
                else action_str
            )
            diff = (action_dt - created_dt).total_seconds()
            durations.append(max(0.0, float(diff)))
        except Exception:
            continue

    if not durations:
        return {
            "reviewed_count": 0,
            "avg_seconds": 0.0,
            "median_seconds": 0.0,
            "min_seconds": 0.0,
            "max_seconds": 0.0,
            "formatted_avg": "0s",
        }

    avg_sec = round(statistics.mean(durations), 2)
    med_sec = round(statistics.median(durations), 2)
    min_sec = round(min(durations), 2)
    max_sec = round(max(durations), 2)

    def _fmt(sec: float) -> str:
        if sec < 60:
            return f"{round(sec)}s"
        m, s = divmod(int(round(sec)), 60)
        return f"{m}m {s}s"

    return {
        "reviewed_count": len(durations),
        "avg_seconds": avg_sec,
        "median_seconds": med_sec,
        "min_seconds": min_sec,
        "max_seconds": max_sec,
        "formatted_avg": _fmt(avg_sec),
    }


def calculate_batch_metrics(
    connection: Any,
    batch_id: str,
    allowed_clients: list[str],
) -> dict[str, Any]:
    """Calculate complete PRD §4.7 metrics for a batch, scoped to assigned clients."""
    batch_rec = get_batch_record(connection, batch_id)
    if batch_rec is None:
        raise HTTPException(status_code=404, detail="batch not found")

    # 1. Fetch visible ads
    visible_ads = get_batch_ads(connection, batch_id, allowed_clients)
    ad_ids = [ad["ad_id"] for ad in visible_ads]
    total_ads = len(visible_ads)

    # 2. Rejected rows during ingest
    rejected_rows = get_batch_rejected_rows(connection, batch_id)
    rejected_rows_count = len(rejected_rows)

    # 3. Findings & Dismissals (False Alarms)
    findings_by_ad = get_ads_findings(connection, ad_ids) if ad_ids else {}
    dismissed_map = get_dismissed_findings_map(connection, ad_ids) if ad_ids else {}

    total_findings_count = sum(len(flist) for flist in findings_by_ad.values())
    dismissed_count = len(dismissed_map)
    active_findings_count = max(0, total_findings_count - dismissed_count)

    false_alarm_rate = (
        round(dismissed_count / total_findings_count, 4)
        if total_findings_count > 0
        else 0.0
    )

    # 4. Problems by type breakdown
    problems_by_type = {
        "mechanical": 0,
        "page": 0,
        "wording": 0,
    }
    rules_breakdown: dict[str, int] = {}
    problems_by_field: dict[str, int] = {}
    top_dismissed_rules: dict[str, int] = {}
    dismissed_by_check_type = {
        "mechanical": 0,
        "page": 0,
        "wording": 0,
    }

    for flist in findings_by_ad.values():
        for f in flist:
            ctype = str(f.get("check_type", "mechanical")).lower()
            if ctype in problems_by_type:
                problems_by_type[ctype] += 1

            rid = f.get("rule_id")
            if rid:
                rules_breakdown[rid] = rules_breakdown.get(rid, 0) + 1

            fld = f.get("field") or "general"
            problems_by_field[fld] = problems_by_field.get(fld, 0) + 1

            if f.get("is_dismissed"):
                if ctype in dismissed_by_check_type:
                    dismissed_by_check_type[ctype] += 1
                if rid:
                    top_dismissed_rules[rid] = top_dismissed_rules.get(rid, 0) + 1

    top_rules = sorted(
        [{"rule_id": k, "count": v} for k, v in rules_breakdown.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    top_dismissed_rules_list = sorted(
        [{"rule_id": k, "count": v} for k, v in top_dismissed_rules.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    cleaned_ads_count = sum(
        1 for aid, flist in findings_by_ad.items()
        if flist and all(f.get("is_dismissed") for f in flist)
    )
    avg_findings_per_ad = round(total_findings_count / total_ads, 2) if total_ads > 0 else 0.0

    # 5. Verdicts and Ads Blocked
    verdicts = get_ads_verdicts(connection, batch_id, visible_ads, findings_by_ad)
    verdict_counts = {
        "ready": 0,
        "fix needed": 0,
        "blocked": 0,
        "needs a person": 0,
        "pending": 0,
    }
    for v in verdicts.values():
        norm_v = (v or "pending").lower()
        if norm_v in verdict_counts:
            verdict_counts[norm_v] += 1
        else:
            verdict_counts["pending"] += 1

    blocked_count = verdict_counts["blocked"]
    fix_needed_count = verdict_counts["fix needed"]
    total_blocked = blocked_count + fix_needed_count
    blocked_rate = round(total_blocked / total_ads, 4) if total_ads > 0 else 0.0

    # 6. Approvals and Reviewer Time
    approvals_list: list[dict[str, Any]] = []
    approval_state_counts = {
        "pending_review": 0,
        "approved": 0,
        "rejected": 0,
        "sending": 0,
        "sent": 0,
        "dead_letter": 0,
    }

    if ad_ids:
        try:
            cur_appr = connection.execute(
                """
                SELECT ad_id, state, created_at, approved_at, rejected_at, updated_at
                FROM ad_approvals
                WHERE ad_id = ANY(%s);
                """,
                (ad_ids,),
            )
            for row in cur_appr.fetchall():
                st = str(row[1])
                approval_state_counts[st] = approval_state_counts.get(st, 0) + 1
                approvals_list.append({
                    "ad_id": str(row[0]),
                    "state": st,
                    "created_at": row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]),
                    "approved_at": row[3].isoformat() if row[3] and hasattr(row[3], "isoformat") else str(row[3]) if row[3] else None,
                    "rejected_at": row[4].isoformat() if row[4] and hasattr(row[4], "isoformat") else str(row[4]) if row[4] else None,
                    "updated_at": row[5].isoformat() if row[5] and hasattr(row[5], "isoformat") else str(row[5]) if row[5] else None,
                })
        except Exception:
            pass

    reviewer_time_stats = calculate_reviewer_time_stats(approvals_list)

    rejection_reasons: dict[str, int] = {}
    if ad_ids:
        try:
            cur_reasons = connection.execute(
                """
                SELECT rejection_reason, COUNT(*)
                FROM ad_approvals
                WHERE ad_id = ANY(%s) AND state = 'rejected' AND rejection_reason IS NOT NULL
                GROUP BY rejection_reason;
                """,
                (ad_ids,),
            )
            for r in cur_reasons.fetchall():
                if r[0]:
                    rejection_reasons[str(r[0])] = int(r[1])
        except Exception:
            pass

    # 7. Worker Tracing Cost
    total_cost_usd = 0.0
    total_steps = 0
    traces_count = 0
    step_capped_ads_count = 0
    if ad_ids:
        try:
            cur_cost = connection.execute(
                """
                SELECT COALESCE(SUM(total_cost_usd), 0.0), COALESCE(SUM(step_count), 0), COUNT(*)
                FROM worker_traces
                WHERE batch_id = %s AND client_id = ANY(%s);
                """,
                (batch_id, allowed_clients),
            )
            r_cost = cur_cost.fetchone()
            if r_cost:
                total_cost_usd = float(r_cost[0])
                total_steps = int(r_cost[1])
                traces_count = int(r_cost[2])
        except Exception:
            pass

        try:
            cur_capped = connection.execute(
                """
                SELECT COUNT(*)
                FROM worker_traces
                WHERE batch_id = %s AND client_id = ANY(%s) AND step_count >= 5;
                """,
                (batch_id, allowed_clients),
            )
            r_cap = cur_capped.fetchone()
            if r_cap:
                step_capped_ads_count = int(r_cap[0])
        except Exception:
            pass

    avg_cost_per_ad_usd = round(total_cost_usd / total_ads, 6) if total_ads > 0 else 0.0
    avg_steps_per_ad = round(total_steps / total_ads, 2) if total_ads > 0 else 0.0

    # 8. Stand-in Platform Send Results
    platform_accepted = 0
    platform_rejected = 0
    if ad_ids:
        try:
            cur_sends = connection.execute(
                """
                SELECT platform_status, COUNT(*)
                FROM ad_sends
                WHERE batch_id = %s AND client_id = ANY(%s)
                GROUP BY platform_status;
                """,
                (batch_id, allowed_clients),
            )
            for row in cur_sends.fetchall():
                pstat = str(row[0]).lower() if row[0] else ""
                if pstat == "accepted":
                    platform_accepted = int(row[1])
                elif pstat == "rejected":
                    platform_rejected = int(row[1])
        except Exception:
            pass

    total_send_attempts = 0
    retried_ads_count = 0
    if ad_ids:
        try:
            cur_attempts = connection.execute(
                """
                SELECT COALESCE(SUM(attempt_count), 0),
                       COUNT(DISTINCT ad_id) FILTER (WHERE attempt_count > 1)
                FROM ad_sends
                WHERE batch_id = %s AND client_id = ANY(%s);
                """,
                (batch_id, allowed_clients),
            )
            r_att = cur_attempts.fetchone()
            if r_att:
                total_send_attempts = int(r_att[0])
                retried_ads_count = int(r_att[1])
        except Exception:
            total_send_attempts = platform_accepted + platform_rejected

    total_sent = platform_accepted + platform_rejected
    retry_rate = round(retried_ads_count / total_sent, 4) if total_sent > 0 else 0.0
    acceptance_rate = round(platform_accepted / total_sent, 4) if total_sent > 0 else 0.0

    # 9. Simulated Rejection Rate (PRD §4.7)
    # Without gate: all ads that had blocked/fix needed verdicts + rejected rows would fail on platform
    simulated_fails_without_gate = total_blocked + rejected_rows_count
    total_evaluated_sheet_rows = total_ads + rejected_rows_count
    without_gate_rejection_rate = (
        round(simulated_fails_without_gate / total_evaluated_sheet_rows, 4)
        if total_evaluated_sheet_rows > 0
        else 0.0
    )

    # With gate: Launch Gate protects the platform. Only approved ads were sent.
    with_gate_rejection_rate = (
        round(platform_rejected / total_sent, 4)
        if total_sent > 0
        else 0.0
    )

    ads_prevented_from_rejection = total_blocked
    rejections_avoided_count = simulated_fails_without_gate - platform_rejected
    effectiveness_pct = (
        round((rejections_avoided_count / simulated_fails_without_gate) * 100.0, 2)
        if simulated_fails_without_gate > 0
        else 100.0
    )

    # 10. CI Release Gate Thresholds (PRD §4.7 / Line 27: merge blocked if false alarms > 5%)
    false_alarm_threshold = 0.05
    dead_letter_threshold = 0.05
    dl_count = approval_state_counts.get("dead_letter", 0)
    dl_rate = round(dl_count / total_ads, 4) if total_ads > 0 else 0.0

    fa_passed = false_alarm_rate <= false_alarm_threshold
    dl_passed = dl_rate <= dead_letter_threshold
    gate_passed = fa_passed and dl_passed

    ci_release_gate = {
        "status": "passed" if gate_passed else "failed",
        "passed": gate_passed,
        "false_alarm_passed": fa_passed,
        "dead_letter_passed": dl_passed,
        "checks": [
            {
                "name": "False Alarm Rate <= 5%",
                "target": "<= 5.0%",
                "actual": f"{round(false_alarm_rate * 100, 2)}%",
                "passed": fa_passed,
            },
            {
                "name": "Dead-Letter Rate <= 5%",
                "target": "<= 5.0%",
                "actual": f"{round(dl_rate * 100, 2)}%",
                "passed": dl_passed,
            },
        ],
    }

    return {
        "batch_id": batch_id,
        "filename": batch_rec.get("filename"),
        "uploaded_by": batch_rec.get("uploaded_by"),
        "created_at": batch_rec.get("created_at"),
        "clients": sorted({ad["client_id"] for ad in visible_ads}),
        "totals": {
            "total_ads": total_ads,
            "rejected_rows_count": rejected_rows_count,
            "total_sheet_rows": total_evaluated_sheet_rows,
        },
        "verdicts": verdict_counts,
        "problems_by_type": {
            "total_findings": total_findings_count,
            "active_findings": active_findings_count,
            "dismissed_findings": dismissed_count,
            "by_check_type": problems_by_type,
            "by_field": problems_by_field,
            "avg_findings_per_ad": avg_findings_per_ad,
            "top_rules": top_rules,
        },
        "ads_blocked": {
            "blocked_count": blocked_count,
            "fix_needed_count": fix_needed_count,
            "total_blocked": total_blocked,
            "blocked_rate": blocked_rate,
        },
        "reviewer_time": reviewer_time_stats,
        "reviewer_actions": {
            "approved_count": approval_state_counts.get("approved", 0) + approval_state_counts.get("sending", 0) + approval_state_counts.get("sent", 0),
            "rejected_count": approval_state_counts.get("rejected", 0),
            "pending_count": approval_state_counts.get("pending_review", 0),
            "rejection_reasons": rejection_reasons,
        },
        "false_alarms": {
            "dismissed_count": dismissed_count,
            "total_findings": total_findings_count,
            "false_alarm_rate": false_alarm_rate,
            "by_check_type": dismissed_by_check_type,
            "top_dismissed_rules": top_dismissed_rules_list,
            "cleaned_ads_count": cleaned_ads_count,
        },
        "cost": {
            "total_cost_usd": round(total_cost_usd, 6),
            "avg_cost_per_ad_usd": avg_cost_per_ad_usd,
            "total_steps": total_steps,
            "avg_steps_per_ad": avg_steps_per_ad,
            "traces_count": traces_count,
            "step_capped_ads_count": step_capped_ads_count,
        },
        "simulated_rejection_rates": {
            "without_gate_rejection_rate": without_gate_rejection_rate,
            "with_gate_rejection_rate": with_gate_rejection_rate,
            "ads_prevented_from_rejection": ads_prevented_from_rejection,
            "rejections_avoided_count": rejections_avoided_count,
            "protection_effectiveness_percent": effectiveness_pct,
        },
        "approval_states": approval_state_counts,
        "platform_sends": {
            "total_sends": total_sent,
            "platform_accepted": platform_accepted,
            "platform_rejected": platform_rejected,
            "acceptance_rate": acceptance_rate,
            "total_attempts": total_send_attempts,
            "retried_ads_count": retried_ads_count,
            "retry_rate": retry_rate,
            "dead_letter_count": dl_count,
            "dead_letter_rate": dl_rate,
        },
        "dead_letter": {
            "count": dl_count,
            "rate": dl_rate,
            "has_dead_letter": dl_count > 0,
        },
        "ci_release_gate": ci_release_gate,
    }


def calculate_global_metrics(
    connection: Any,
    allowed_clients: list[str],
    client_id: str | None = None,
) -> dict[str, Any]:
    """Calculate aggregated cross-batch metrics over time, strictly scoped to allowed clients."""
    target_clients = [client_id] if client_id else allowed_clients
    if not target_clients:
        return {
            "total_batches": 0,
            "total_ads": 0,
            "total_cost_usd": 0.0,
            "overall_blocked_rate": 0.0,
            "overall_false_alarm_rate": 0.0,
            "problems_by_type": {"mechanical": 0, "page": 0, "wording": 0},
            "batches": [],
        }

    # Find visible batches
    try:
        cur_b = connection.execute(
            """
            SELECT DISTINCT b.batch_id, b.filename, b.created_at
            FROM batches b
            JOIN ads a ON b.batch_id = a.batch_id
            WHERE a.client_id = ANY(%s)
            ORDER BY b.created_at DESC;
            """,
            (target_clients,),
        )
        batch_rows = cur_b.fetchall()
    except Exception:
        batch_rows = []

    batch_summaries: list[dict[str, Any]] = []
    total_ads_sum = 0
    total_blocked_sum = 0
    total_findings_sum = 0
    total_dismissed_sum = 0
    total_cost_sum = 0.0
    total_dead_letter_sum = 0
    global_problems = {"mechanical": 0, "page": 0, "wording": 0}

    for b in batch_rows:
        bid = str(b[0])
        try:
            m = calculate_batch_metrics(connection, bid, target_clients)
            batch_summaries.append({
                "batch_id": bid,
                "filename": m.get("filename"),
                "created_at": m.get("created_at"),
                "total_ads": m["totals"]["total_ads"],
                "total_blocked": m["ads_blocked"]["total_blocked"],
                "blocked_rate": m["ads_blocked"]["blocked_rate"],
                "false_alarm_rate": m["false_alarms"]["false_alarm_rate"],
                "total_cost_usd": m["cost"]["total_cost_usd"],
                "avg_reviewer_seconds": m["reviewer_time"]["avg_seconds"],
                "without_gate_rejection_rate": m["simulated_rejection_rates"]["without_gate_rejection_rate"],
                "dead_letter_count": m["dead_letter"]["count"],
            })

            total_ads_sum += m["totals"]["total_ads"]
            total_blocked_sum += m["ads_blocked"]["total_blocked"]
            total_findings_sum += m["problems_by_type"]["total_findings"]
            total_dismissed_sum += m["false_alarms"]["dismissed_count"]
            total_cost_sum += m["cost"]["total_cost_usd"]
            total_dead_letter_sum += m.get("dead_letter", {}).get("count", 0)

            for k in ("mechanical", "page", "wording"):
                global_problems[k] += m["problems_by_type"]["by_check_type"].get(k, 0)
        except Exception as exc:
            logger.debug("Failed calculating metrics for batch %s: %s", bid, exc)

    overall_blocked_rate = round(total_blocked_sum / total_ads_sum, 4) if total_ads_sum > 0 else 0.0
    overall_false_alarm_rate = round(total_dismissed_sum / total_findings_sum, 4) if total_findings_sum > 0 else 0.0
    global_dead_letter_rate = round(total_dead_letter_sum / total_ads_sum, 4) if total_ads_sum > 0 else 0.0

    global_fa_passed = overall_false_alarm_rate <= 0.05
    global_dl_passed = global_dead_letter_rate <= 0.05
    global_gate_passed = global_fa_passed and global_dl_passed

    global_release_gate = {
        "status": "passed" if global_gate_passed else "failed",
        "passed": global_gate_passed,
        "false_alarm_passed": global_fa_passed,
        "dead_letter_passed": global_dl_passed,
        "checks": [
            {
                "name": "False Alarm Rate <= 5%",
                "target": "<= 5.0%",
                "actual": f"{round(overall_false_alarm_rate * 100, 2)}%",
                "passed": global_fa_passed,
            },
            {
                "name": "Dead-Letter Rate <= 5%",
                "target": "<= 5.0%",
                "actual": f"{round(global_dead_letter_rate * 100, 2)}%",
                "passed": global_dl_passed,
            },
        ],
    }

    return {
        "clients": target_clients,
        "total_batches": len(batch_summaries),
        "total_ads": total_ads_sum,
        "total_cost_usd": round(total_cost_sum, 6),
        "avg_cost_per_batch_usd": round(total_cost_sum / len(batch_summaries), 4) if batch_summaries else 0.0,
        "avg_cost_per_ad_usd": round(total_cost_sum / total_ads_sum, 6) if total_ads_sum > 0 else 0.0,
        "overall_blocked_rate": overall_blocked_rate,
        "overall_false_alarm_rate": overall_false_alarm_rate,
        "problems_by_type": global_problems,
        "dead_letter": {
            "count": total_dead_letter_sum,
            "rate": global_dead_letter_rate,
            "has_dead_letter": total_dead_letter_sum > 0,
        },
        "ci_release_gate": global_release_gate,
        "batches": batch_summaries,
    }


# --- API Routes ---

@router.get("/batches/{batch_id}/metrics", status_code=status.HTTP_200_OK)
def get_batch_metrics_endpoint(
    batch_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve complete PRD §4.7 metrics for a batch, scoped to assigned clients."""
    with get_session() as conn:
        batch_rec = get_batch_record(conn, batch_id)
        if batch_rec is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")

        # Client scoping check (Needs: 7)
        allowed_clients = list(user.assigned_client_ids)
        visible_ads = get_batch_ads(conn, batch_id, allowed_clients)

        # Distinguish 404 vs 403
        try:
            cur_tot = conn.execute("SELECT COUNT(*) FROM ads WHERE batch_id = %s", (batch_id,))
            total_in_batch = int(cur_tot.fetchone()[0])
        except Exception:
            total_in_batch = len(visible_ads)

        if total_in_batch > 0 and len(visible_ads) == 0:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

        return calculate_batch_metrics(conn, batch_id, allowed_clients)


@router.get("/batches/{batch_id}/metrics/summary", status_code=status.HTTP_200_OK)
def get_batch_metrics_summary_endpoint(
    batch_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve concise KPI summary of batch metrics."""
    with get_session() as conn:
        m = get_batch_metrics_endpoint(batch_id, user)
        return {
            "batch_id": batch_id,
            "filename": m.get("filename"),
            "total_ads": m["totals"]["total_ads"],
            "blocked_count": m["ads_blocked"]["total_blocked"],
            "blocked_rate": m["ads_blocked"]["blocked_rate"],
            "false_alarm_rate": m["false_alarms"]["false_alarm_rate"],
            "total_cost_usd": m["cost"]["total_cost_usd"],
            "avg_cost_per_ad_usd": m["cost"]["avg_cost_per_ad_usd"],
            "reviewer_avg_seconds": m["reviewer_time"]["avg_seconds"],
            "without_gate_rejection_rate": m["simulated_rejection_rates"]["without_gate_rejection_rate"],
            "rejections_avoided_count": m["simulated_rejection_rates"]["rejections_avoided_count"],
            "dead_letter_count": m["dead_letter"]["count"],
            "release_gate_status": m["ci_release_gate"]["status"],
            "release_gate_passed": m["ci_release_gate"]["passed"],
        }


@router.get("/batches/{batch_id}/metrics/export", status_code=status.HTTP_200_OK)
def export_batch_metrics_endpoint(
    batch_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Export complete batch metrics for auditing and reporting."""
    return get_batch_metrics_endpoint(batch_id, user)


@router.get("/metrics/release-gate", status_code=status.HTTP_200_OK)
def get_release_gate_metrics_endpoint(
    client_id: str | None = Query(default=None, description="Optional filter by client ID"),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve release gate evaluation report against PRD thresholds (false alarm rate <= 5%)."""
    with get_session() as conn:
        allowed_clients = list(user.assigned_client_ids)
        if client_id:
            require_client_access(user, client_id)

        m = calculate_global_metrics(conn, allowed_clients, client_id=client_id)
        return {
            "status": m["ci_release_gate"]["status"],
            "passed": m["ci_release_gate"]["passed"],
            "false_alarm_rate": m["overall_false_alarm_rate"],
            "false_alarm_threshold": 0.05,
            "false_alarm_passed": m["ci_release_gate"]["false_alarm_passed"],
            "dead_letter_rate": m["dead_letter"]["rate"],
            "dead_letter_threshold": 0.05,
            "dead_letter_passed": m["ci_release_gate"]["dead_letter_passed"],
            "evaluated_batches_count": m["total_batches"],
            "evaluated_ads_count": m["total_ads"],
            "checks": m["ci_release_gate"]["checks"],
            "batches": m["batches"],
        }


@router.get("/metrics", status_code=status.HTTP_200_OK)
def get_global_metrics_endpoint(
    client_id: str | None = Query(default=None, description="Optional filter by client ID"),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve aggregated cross-batch metrics over time scoped strictly to assigned clients."""
    with get_session() as conn:
        allowed_clients = list(user.assigned_client_ids)

        if client_id:
            # Enforce client access
            require_client_access(user, client_id)

        return calculate_global_metrics(conn, allowed_clients, client_id=client_id)

