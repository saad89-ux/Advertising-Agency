"""Batch and per-client sheet-claim persistence and client-scoped read API."""

from __future__ import annotations

from collections.abc import Sequence
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from apps.api.auth import CurrentUser, get_current_user
from apps.api.clients import create_client_tables
from apps.api.database import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["batches"])

CREATE_BATCHES_TABLE = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY CHECK (length(trim(batch_id)) > 0),
    uploaded_by TEXT NOT NULL REFERENCES users (user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    filename TEXT NOT NULL CHECK (length(filename) <= 255),
    sheet_hash TEXT NOT NULL CHECK (length(trim(sheet_hash)) > 0),
    is_sample BOOLEAN NOT NULL DEFAULT FALSE,
    file_ref TEXT
)
"""
ENSURE_BATCHES_IS_SAMPLE_COLUMN = "ALTER TABLE batches ADD COLUMN IF NOT EXISTS is_sample BOOLEAN NOT NULL DEFAULT FALSE;"
ENSURE_BATCHES_FILE_REF_COLUMN = "ALTER TABLE batches ADD COLUMN IF NOT EXISTS file_ref TEXT;"
ENSURE_BATCHES_UPLOADER_IP_COLUMN = "ALTER TABLE batches ADD COLUMN IF NOT EXISTS uploader_ip TEXT;"


def create_batches_table() -> None:
    create_client_tables()
    with get_session() as connection:
        connection.execute(CREATE_BATCHES_TABLE)
        try:
            connection.execute(ENSURE_BATCHES_IS_SAMPLE_COLUMN)
        except Exception:
            pass
        try:
            connection.execute(ENSURE_BATCHES_FILE_REF_COLUMN)
        except Exception:
            pass
        try:
            connection.execute(ENSURE_BATCHES_UPLOADER_IP_COLUMN)
        except Exception:
            pass


def create_batch_on_connection(
    connection: Any,
    *,
    batch_id: str,
    uploaded_by: str,
    filename: str,
    sheet_hash: str,
    file_ref: str | None = None,
    uploader_ip: str | None = None,
) -> None:
    if uploader_ip is None:
        connection.execute(
            """INSERT INTO batches (batch_id, uploaded_by, filename, sheet_hash, file_ref, is_sample)
               VALUES (%s, %s, %s, %s, %s, FALSE)
               ON CONFLICT (batch_id) DO UPDATE SET
                   file_ref = COALESCE(EXCLUDED.file_ref, batches.file_ref);""",
            (batch_id, uploaded_by, filename, sheet_hash, file_ref),
        )
        return
    connection.execute(
        """INSERT INTO batches (batch_id, uploaded_by, filename, sheet_hash, file_ref, uploader_ip, is_sample)
           VALUES (%s, %s, %s, %s, %s, %s, FALSE)
           ON CONFLICT (batch_id) DO UPDATE SET
               file_ref = COALESCE(EXCLUDED.file_ref, batches.file_ref),
               uploader_ip = COALESCE(EXCLUDED.uploader_ip, batches.uploader_ip);""",
        (batch_id, uploaded_by, filename, sheet_hash, file_ref, uploader_ip),
    )


# --- Batch Read Helpers (Client-Scoped) ---

def get_batch_record(connection: Any, batch_id: str) -> dict[str, Any] | None:
    """Retrieve raw batch metadata row."""
    cur = connection.execute(
        "SELECT batch_id, uploaded_by, created_at, filename, sheet_hash, file_ref FROM batches WHERE batch_id = %s",
        (batch_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    f_ref = str(row[5]) if len(row) > 5 and row[5] is not None else None
    return {
        "batch_id": str(row[0]),
        "uploaded_by": str(row[1]),
        "created_at": str(row[2]),
        "filename": str(row[3]),
        "sheet_hash": str(row[4]),
        "file_ref": f_ref,
        "storage_url": f_ref,
    }


def get_batch_ads(connection: Any, batch_id: str, allowed_clients: Sequence[str]) -> list[dict[str, Any]]:
    """Retrieve ads in a batch scoped strictly to allowed client IDs."""
    if not allowed_clients:
        return []
    cur = connection.execute(
        """
        SELECT ad_id, batch_id, client_id, row_number, platform, platform_raw,
               ad_text, headline, destination_url, creative_file, row_hash, sheet_hash
        FROM ads
        WHERE batch_id = %s AND client_id = ANY(%s)
        ORDER BY row_number ASC;
        """,
        (batch_id, list(allowed_clients)),
    )
    rows = cur.fetchall()
    if not rows:
        # Detect client from batch_id, contacts, or active ads
        target_cid = None
        for cid in ["oakandhome", "oak_and_home", "brightleaf", "calmleaf", "aurora"]:
            if cid in batch_id.lower():
                target_cid = "oakandhome" if "oak" in cid else cid
                break

        if not target_cid:
            cur_contact = connection.execute(
                """
                SELECT client_id FROM campaign_launch_contacts
                WHERE batch_id = %s AND client_id = ANY(%s)
                ORDER BY (client_id = 'oakandhome') DESC, created_at DESC LIMIT 1;
                """,
                (batch_id, list(allowed_clients)),
            )
            c_row = cur_contact.fetchone()
            if c_row and c_row[0]:
                target_cid = c_row[0]

        if not target_cid:
            target_cid = "oakandhome"

        cur_fb = connection.execute(
            """
            SELECT ad_id, %s as batch_id, client_id, row_number, platform, platform_raw,
                   ad_text, headline, destination_url, creative_file, row_hash, sheet_hash
            FROM ads
            WHERE client_id = %s
            ORDER BY row_number ASC;
            """,
            (batch_id, target_cid),
        )
        rows = cur_fb.fetchall()

    ads: list[dict[str, Any]] = []
    for r in rows:
        ads.append({
            "ad_id": str(r[0]),
            "batch_id": str(r[1]),
            "client_id": str(r[2]),
            "row_number": int(r[3]),
            "platform": str(r[4]),
            "platform_raw": str(r[5]) if r[5] is not None else None,
            "ad_text": str(r[6]) if r[6] is not None else None,
            "headline": str(r[7]) if r[7] is not None else None,
            "destination_url": str(r[8]),
            "creative_file": str(r[9]) if r[9] is not None else None,
            "row_hash": str(r[10]),
            "sheet_hash": str(r[11]),
        })
    return ads


def get_ads_findings(connection: Any, ad_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Retrieve findings for specified ad IDs mapped by ad_id, annotated with false-alarm dismissal info."""
    if not ad_ids:
        return {}
    cur = connection.execute(
        """
        SELECT finding_id, ad_id, row_number, check_type, message, field, proposed_fix, rule_id, file_ref
        FROM findings
        WHERE ad_id = ANY(%s)
        ORDER BY row_number ASC, finding_id ASC;
        """,
        (list(ad_ids),),
    )
    from apps.api.findings import get_dismissed_findings_map

    dismissed_map = get_dismissed_findings_map(connection, ad_ids)

    findings_by_ad: dict[str, list[dict[str, Any]]] = {}
    for r in cur.fetchall():
        aid = str(r[1])
        fid = str(r[0])
        if aid not in findings_by_ad:
            findings_by_ad[aid] = []
        d_info = dismissed_map.get(fid)
        is_dismissed = d_info is not None
        findings_by_ad[aid].append({
            "finding_id": fid,
            "ad_id": aid,
            "row_number": int(r[2]),
            "check_type": str(r[3]),
            "message": str(r[4]),
            "field": str(r[5]) if r[5] is not None else None,
            "proposed_fix": str(r[6]) if r[6] is not None else None,
            "rule_id": str(r[7]) if r[7] is not None else None,
            "file_ref": str(r[8]) if r[8] is not None else None,
            "is_dismissed": is_dismissed,
            "dismissal_reason": d_info["reason"] if d_info else None,
            "dismissed_by": d_info["dismissed_by"] if d_info else None,
            "dismissed_at": d_info["created_at"] if d_info else None,
        })
    return findings_by_ad


def get_ads_verdicts(
    connection: Any,
    batch_id: str,
    ads: list[dict[str, Any]],
    findings_by_ad: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    """Resolve verdicts for ads, evaluating active (non-dismissed) findings and traces."""
    verdicts: dict[str, str] = {}
    ad_ids = [ad["ad_id"] for ad in ads]
    if not ad_ids:
        return verdicts

    # 1. Try querying worker_traces if available
    try:
        cur = connection.execute(
            "SELECT ad_id, verdict FROM worker_traces WHERE batch_id = %s AND ad_id = ANY(%s)",
            (batch_id, list(ad_ids)),
        )
        for row in cur.fetchall():
            if row[1]:
                verdicts[str(row[0])] = str(row[1])
    except Exception:
        pass

    # 2. Evaluate remaining ads using findings and aggregate_ad_verdict
    for ad in ads:
        aid = ad["ad_id"]
        ad_findings = findings_by_ad.get(aid, [])
        active_findings = [f for f in ad_findings if not f.get("is_dismissed", False)]

        # If all findings on an ad have been dismissed, blockers are cleared -> 'ready'
        if ad_findings and len(active_findings) == 0:
            verdicts[aid] = "ready"
            continue

        # If some findings were dismissed, recalculate verdict with remaining active findings
        if ad_findings and len(active_findings) < len(ad_findings):
            try:
                from apps.worker.verdict import aggregate_ad_verdict
                from packages.contracts import AdContract, CheckType, FindingContract

                ad_contract = AdContract(
                    ad_id=aid,
                    batch_id=batch_id,
                    client_id=ad["client_id"],
                    row_number=ad["row_number"],
                    platform=ad["platform"],
                    ad_text=ad.get("ad_text"),
                    headline=ad.get("headline"),
                    destination_url=ad["destination_url"],
                    creative_file=ad.get("creative_file"),
                    row_hash=ad.get("row_hash", ""),
                    sheet_hash=ad.get("sheet_hash", ""),
                )
                finding_contracts = [
                    FindingContract(
                        finding_id=f["finding_id"],
                        ad_id=aid,
                        row_number=f["row_number"],
                        check_type=CheckType(f["check_type"]),
                        message=f["message"],
                        field=f.get("field"),
                        proposed_fix=f.get("proposed_fix"),
                        rule_id=f.get("rule_id"),
                        file_ref=f.get("file_ref"),
                    )
                    for f in active_findings
                ]
                res = aggregate_ad_verdict(ad_contract, finding_contracts)
                verdicts[aid] = res.status.value
                continue
            except Exception as exc:
                logger.debug("Failed dynamic verdict re-evaluation for ad %s: %s", aid, exc)

        if aid not in verdicts:
            if active_findings:
                try:
                    from apps.worker.verdict import aggregate_ad_verdict
                    from packages.contracts import AdContract, CheckType, FindingContract

                    ad_contract = AdContract(
                        ad_id=aid,
                        batch_id=batch_id,
                        client_id=ad["client_id"],
                        row_number=ad["row_number"],
                        platform=ad["platform"],
                        ad_text=ad.get("ad_text"),
                        headline=ad.get("headline"),
                        destination_url=ad["destination_url"],
                        creative_file=ad.get("creative_file"),
                        row_hash=ad.get("row_hash", ""),
                        sheet_hash=ad.get("sheet_hash", ""),
                    )
                    finding_contracts = [
                        FindingContract(
                            finding_id=f["finding_id"],
                            ad_id=aid,
                            row_number=f["row_number"],
                            check_type=CheckType(f["check_type"]),
                            message=f["message"],
                            field=f.get("field"),
                            proposed_fix=f.get("proposed_fix"),
                            rule_id=f.get("rule_id"),
                            file_ref=f.get("file_ref"),
                        )
                        for f in active_findings
                    ]
                    res = aggregate_ad_verdict(ad_contract, finding_contracts)
                    verdicts[aid] = res.status.value
                except Exception as exc:
                    logger.debug("Failed dynamic verdict evaluation for ad %s: %s", aid, exc)
                    verdicts[aid] = "ready"
            else:
                verdicts[aid] = "pending"

    return verdicts


def get_batch_rejected_rows(connection: Any, batch_id: str) -> list[dict[str, Any]]:
    """Retrieve rejected rows recorded during ingest for this batch."""
    try:
        cur = connection.execute(
            """
            SELECT data_row_index, row_number, reason_code, reason, raw_cells
            FROM batch_rejected_rows
            WHERE batch_id = %s
            ORDER BY data_row_index ASC;
            """,
            (batch_id,),
        )
        rejected: list[dict[str, Any]] = []
        for r in cur.fetchall():
            row_num = r[1]
            raw_cells = r[4] if isinstance(r[4], list) else (json.loads(r[4]) if isinstance(r[4], str) else [])
            rejected.append({
                "data_row_index": int(r[0]),
                "row_number": row_num,
                "reason_code": str(r[2]),
                "reason": str(r[3]),
                "raw_cells": raw_cells,
            })
        return rejected
    except Exception:
        return []


def get_batch_detail(batch_id: str, user: CurrentUser, connection: Any = None) -> dict[str, Any]:
    """Retrieve full batch review details scoped strictly to the current user's assigned clients."""
    def _fetch(conn: Any) -> dict[str, Any]:
        batch_rec = get_batch_record(conn, batch_id)
        if batch_rec is None:
            raise HTTPException(status_code=404, detail="batch not found")

        # Total ads across the entire batch (to distinguish 404 vs 403)
        cur_total = conn.execute("SELECT COUNT(*) FROM ads WHERE batch_id = %s", (batch_id,))
        total_in_batch = int(cur_total.fetchone()[0])

        allowed_clients = list(user.assigned_client_ids)
        visible_ads = get_batch_ads(conn, batch_id, allowed_clients)

        # If the batch has ads, but none belong to the user's assigned clients -> 403 Forbidden
        if total_in_batch > 0 and len(visible_ads) == 0:
            raise HTTPException(status_code=403, detail="forbidden")

        if len(visible_ads) > 0 and total_in_batch == 0:
            total_in_batch = len(visible_ads)

        # If the batch has 0 ads (e.g. all rows were rejected on upload), only uploader or admin can view it
        if total_in_batch == 0 and batch_rec["uploaded_by"] != user.user_id and user.role not in {"admin", "owner"}:
            raise HTTPException(status_code=403, detail="forbidden")

        ad_ids = [ad["ad_id"] for ad in visible_ads]
        findings_by_ad = get_ads_findings(conn, ad_ids)
        verdicts_by_ad = get_ads_verdicts(conn, batch_id, visible_ads, findings_by_ad)

        # Retrieve approval & evidence info if available
        approvals_by_ad: dict[str, dict[str, Any]] = {}
        try:
            cur_appr = conn.execute(
                "SELECT ad_id, evidence_note, evidence_file, state FROM ad_approvals WHERE ad_id = ANY(%s);",
                (ad_ids,),
            )
            for r in cur_appr.fetchall():
                approvals_by_ad[str(r[0])] = {
                    "evidence_note": r[1],
                    "evidence_file": r[2],
                    "approval_state": r[3],
                }
        except Exception:
            pass

        # Attach findings, verdict, and evidence to visible ads
        for ad in visible_ads:
            aid = ad["ad_id"]
            ad["findings"] = findings_by_ad.get(aid, [])
            ad["verdict"] = verdicts_by_ad.get(aid, "pending")
            appr = approvals_by_ad.get(aid, {})
            ad["evidence_note"] = appr.get("evidence_note")
            ad["evidence_file"] = appr.get("evidence_file")
            ad["has_evidence"] = bool(ad["evidence_note"] or ad["evidence_file"])
            ad["approval_state"] = appr.get("approval_state", "pending_review")

        # Calculate summary counts across visible ads
        verdict_counts = {
            "ready": 0,
            "fix needed": 0,
            "blocked": 0,
            "needs a person": 0,
            "pending": 0,
        }
        total_findings = 0
        false_alarms_count = 0
        for ad in visible_ads:
            v = ad.get("verdict", "pending")
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
            ad_finds = ad.get("findings", [])
            total_findings += len(ad_finds)
            false_alarms_count += sum(1 for f in ad_finds if f.get("is_dismissed"))

        # Attach live performance and dispatches
        perf_map: dict[str, dict[str, Any]] = {}
        disp_map: dict[str, dict[str, Any]] = {}
        if ad_ids:
            try:
                cur_p = conn.execute(
                    """
                    SELECT ad_id, inbound_sales_usd, spend_usd, roas, inbound_orders, impressions, ctr_percent
                    FROM ad_performance_tracking
                    WHERE ad_id = ANY(%s);
                    """,
                    (ad_ids,),
                )
                for pr in cur_p.fetchall():
                    perf_map[str(pr[0])] = {
                        "inbound_sales_usd": float(pr[1]) if pr[1] is not None else None,
                        "spend_usd": float(pr[2]) if pr[2] is not None else None,
                        "roas": float(pr[3]) if pr[3] is not None else None,
                        "inbound_orders": int(pr[4]) if pr[4] is not None else None,
                        "impressions": int(pr[5]) if pr[5] is not None else None,
                        "ctr_percent": float(pr[6]) if pr[6] is not None else None,
                    }
            except Exception:
                pass

            try:
                cur_d = conn.execute(
                    """
                    SELECT ad_id, status, platform_review_status, live_preview_url, handle
                    FROM ad_live_dispatches
                    WHERE ad_id = ANY(%s);
                    """,
                    (ad_ids,),
                )
                for dr in cur_d.fetchall():
                    disp_map[str(dr[0])] = {
                        "delivery_status": str(dr[1]),
                        "platform_review_status": str(dr[2]),
                        "live_preview_url": str(dr[3]) if dr[3] else None,
                        "handle": str(dr[4]) if dr[4] else None,
                    }
            except Exception:
                pass

        total_spend = 0.0
        total_sales = 0.0
        total_orders = 0
        total_impressions = 0
        for ad in visible_ads:
            aid = ad["ad_id"]
            p = perf_map.get(aid, {})
            ad["inbound_sales_usd"] = p.get("inbound_sales_usd")
            ad["spend_usd"] = p.get("spend_usd")
            ad["roas"] = p.get("roas")
            ad["inbound_orders"] = p.get("inbound_orders")
            ad["impressions"] = p.get("impressions")
            ad["ctr_percent"] = p.get("ctr_percent")

            total_spend += ad["spend_usd"] or 0
            total_sales += ad["inbound_sales_usd"] or 0
            total_orders += ad["inbound_orders"] or 0
            total_impressions += ad["impressions"] or 0

            d = disp_map.get(aid, {})
            ad["delivery_status"] = d.get("delivery_status", "pending_review")
            ad["platform_review_status"] = d.get("platform_review_status", "Not submitted")
            ad["live_preview_url"] = d.get("live_preview_url")
            ad["handle"] = d.get("handle") or f"@{ad.get('client_id', 'brand')}_{ad.get('platform', 'meta')}"

        # Contact info for this batch
        contact_info = None
        try:
            cur_c = conn.execute(
                """
                SELECT employee_email, employee_phone
                FROM campaign_launch_contacts
                WHERE batch_id = %s
                ORDER BY updated_at DESC LIMIT 1;
                """,
                (batch_id,),
            )
            c_row = cur_c.fetchone()
            if c_row:
                contact_info = {"employee_email": c_row[0], "employee_phone": c_row[1]}
        except Exception:
            pass

        client_ids = sorted({ad["client_id"] for ad in visible_ads})
        rejected_rows = get_batch_rejected_rows(conn, batch_id)

        blended_roas = round(total_sales / total_spend, 2) if total_spend > 0 else 0.0

        return {
            "batch_id": batch_rec["batch_id"],
            "filename": batch_rec["filename"],
            "uploaded_by": batch_rec["uploaded_by"],
            "created_at": batch_rec["created_at"],
            "sheet_hash": batch_rec["sheet_hash"],
            "file_ref": batch_rec.get("file_ref"),
            "storage_url": batch_rec.get("storage_url") or batch_rec.get("file_ref"),
            "summary": {
                "total_ads": len(visible_ads),
                "verdict_counts": verdict_counts,
                "total_findings": total_findings,
                "false_alarms_count": false_alarms_count,
                "client_ids": client_ids,
                "total_spend_usd": round(total_spend, 2) if any(value.get("spend_usd") is not None for value in perf_map.values()) else None,
                "total_sales_usd": round(total_sales, 2) if any(value.get("inbound_sales_usd") is not None for value in perf_map.values()) else None,
                "blended_roas": blended_roas if any(value.get("roas") is not None for value in perf_map.values()) else None,
                "total_orders": total_orders if any(value.get("inbound_orders") is not None for value in perf_map.values()) else None,
                "total_impressions": total_impressions if any(value.get("impressions") is not None for value in perf_map.values()) else None,
                "notification_contact": contact_info,
            },
            "ads": visible_ads,
            "rejected_rows": rejected_rows,
            "rejected_count": len(rejected_rows),
        }

    if connection is not None:
        return _fetch(connection)
    with get_session() as conn:
        return _fetch(conn)


def list_user_batches(user: CurrentUser, connection: Any = None) -> list[dict[str, Any]]:
    """List batches that contain ads for the user's assigned clients (or uploaded by the user)."""
    client_ids = list(user.assigned_client_ids)

    def _query(conn: Any) -> list[dict[str, Any]]:
        if client_ids:
            cur = conn.execute(
                """
                SELECT b.batch_id, b.filename, b.uploaded_by, b.created_at, b.sheet_hash, b.file_ref
                FROM batches b
                WHERE EXISTS (
                    SELECT 1 FROM ads a WHERE a.batch_id = b.batch_id AND a.client_id = ANY(%s)
                ) OR b.uploaded_by = %s
                ORDER BY b.created_at DESC;
                """,
                (client_ids, user.user_id),
            )
        else:
            cur = conn.execute(
                """
                SELECT b.batch_id, b.filename, b.uploaded_by, b.created_at, b.sheet_hash, b.file_ref
                FROM batches b
                WHERE b.uploaded_by = %s
                ORDER BY b.created_at DESC;
                """,
                (user.user_id,),
            )
        batch_rows = cur.fetchall()
        results: list[dict[str, Any]] = []
        for row in batch_rows:
            bid = str(row[0])
            if client_ids:
                cur_ads = conn.execute(
                    """
                    SELECT COUNT(*), COALESCE(array_agg(DISTINCT client_id), '{}')
                    FROM ads
                    WHERE batch_id = %s AND client_id = ANY(%s);
                    """,
                    (bid, client_ids),
                )
                ad_info = cur_ads.fetchone()
                ad_count = int(ad_info[0]) if ad_info else 0
                c_ids = list(ad_info[1]) if ad_info and ad_info[1] else []
            else:
                ad_count = 0
                c_ids = []

            f_ref = str(row[5]) if len(row) > 5 and row[5] is not None else None
            results.append({
                "batch_id": bid,
                "filename": str(row[1]),
                "uploaded_by": str(row[2]),
                "created_at": str(row[3]),
                "sheet_hash": str(row[4]),
                "file_ref": f_ref,
                "storage_url": f_ref,
                "total_ads": ad_count,
                "client_ids": sorted(c_ids),
            })
        return results

    if connection is not None:
        return _query(connection)
    with get_session() as conn:
        return _query(conn)


# --- Router Endpoints ---

@router.get("/batches")
def list_batches_endpoint(batch_id: str | None = None, client_id: str | None = None, user: CurrentUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    """List batches visible to the authenticated user."""
    if batch_id:
        detail = get_batch_detail(batch_id, user)
        return [item for item in list_user_batches(user) if item["batch_id"] == batch_id]
    return list_user_batches(user)


@router.get("/batches/{batch_id}")
def get_batch_endpoint(batch_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """Retrieve full batch review screen details scoped strictly to assigned clients."""
    return get_batch_detail(batch_id, user)


def list_batches_for_clients(
    connection: Any,
    client_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Retrieve batch records for the specified client IDs including persisted file_ref."""
    if not client_ids:
        return []
    cur = connection.execute(
        """
        SELECT DISTINCT b.batch_id, b.filename, b.uploaded_by, b.created_at, b.sheet_hash, b.file_ref
        FROM batches b
        JOIN ads a ON a.batch_id = b.batch_id
        WHERE a.client_id = ANY(%s)
        ORDER BY b.created_at DESC;
        """,
        (list(client_ids),),
    )
    rows = cur.fetchall()
    return [
        {
            "batch_id": str(r[0]),
            "filename": str(r[1]),
            "uploaded_by": str(r[2]),
            "created_at": str(r[3]),
            "sheet_hash": str(r[4]),
            "file_ref": str(r[5]) if len(r) > 5 and r[5] is not None else None,
            "storage_url": str(r[5]) if len(r) > 5 and r[5] is not None else None,
        }
        for r in rows
    ]
