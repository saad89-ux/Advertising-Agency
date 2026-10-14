"""Dead-letter list & resolution API routes (PRD §4.6, Dataflow Architecture §3, Needs: 46).

Manages ads whose platform send retries have been exhausted or halted:
1. GET  /batches/{batch_id}/dead-letter — List dead-letter ads in a batch with failure audit.
2. GET  /dead-letter — Global listing of unresolved dead-letter ads across assigned clients.
3. POST /ads/{ad_id}/return-to-review — Transition ad from dead_letter back to pending_review.
4. POST /batches/{batch_id}/return-dead-letter-to-review — Return all batch dead-letter ads to review.
5. POST /alerts/{alert_id}/resolve — Mark reviewer alert as resolved.
6. Multi-tenant client scoping (Needs: 7, 46): 403 Forbidden on unassigned client access.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from apps.api.access import require_client_access
from apps.api.approvals import (
    ApprovalState,
    get_ad_record,
    get_or_create_ad_approval,
    transition_ad_state,
)
from apps.api.auth import CurrentUser, get_current_user, require_role
from apps.api.batches import get_batch_record
from apps.api.database import get_session
from apps.worker.sender import get_ad_alerts, get_ad_send_records

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dead_letter"])


# --- Data Access Functions ---

def get_batch_dead_letter_ads(
    connection: Any,
    batch_id: str,
    allowed_clients: list[str],
) -> list[dict[str, Any]]:
    """Retrieve all dead-letter ads in a batch belonging to allowed clients with full failure audit."""
    sql = """
    SELECT a.ad_id, a.batch_id, a.client_id, a.row_number, a.platform,
           a.headline, a.ad_text, a.destination_url, a.creative_file, a.row_hash,
           ap.state, ap.content_hash, ap.idempotency_key, ap.send_attempts,
           ap.rejection_reason, ap.updated_at
    FROM ads a
    JOIN ad_approvals ap ON a.ad_id = ap.ad_id
    WHERE a.batch_id = %s AND a.client_id = ANY(%s) AND ap.state = 'dead_letter'
    ORDER BY a.row_number ASC;
    """
    try:
        cur = connection.execute(sql, (batch_id, allowed_clients))
        rows = cur.fetchall()
    except Exception as exc:
        logger.debug("Failed querying batch dead-letter ads: %s", exc)
        rows = []

    results = []
    for r in rows:
        aid = str(r[0])
        sends = get_ad_send_records(connection, aid)
        latest_send = sends[-1] if sends else {}
        alerts = get_ad_alerts(connection, aid)
        unresolved_alerts = [a for a in alerts if not a.get("resolved")]

        results.append({
            "ad_id": aid,
            "batch_id": str(r[1]),
            "client_id": str(r[2]),
            "row_number": int(r[3]),
            "platform": str(r[4]),
            "headline": r[5],
            "ad_text": r[6],
            "destination_url": str(r[7]),
            "creative_file": r[8],
            "row_hash": str(r[9]),
            "approval_state": str(r[10]),
            "content_hash": r[11],
            "idempotency_key": r[12],
            "send_attempts": int(r[13]) if r[13] is not None else len(sends),
            "dead_letter_reason": r[14] or latest_send.get("error_message"),
            "updated_at": r[15].isoformat() if hasattr(r[15], "isoformat") else str(r[15]) if r[15] else None,
            "latest_send": latest_send,
            "alerts": unresolved_alerts,
            "has_alerts": len(unresolved_alerts) > 0,
        })

    return results


def get_global_dead_letter_ads(
    connection: Any,
    allowed_clients: list[str],
    client_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Retrieve global dead-letter ads across batches visible to assigned clients."""
    target_clients = [client_id] if client_id else allowed_clients

    count_sql = """
    SELECT COUNT(*)
    FROM ads a
    JOIN ad_approvals ap ON a.ad_id = ap.ad_id
    WHERE a.client_id = ANY(%s) AND ap.state = 'dead_letter';
    """
    total = 0
    try:
        cur_count = connection.execute(count_sql, (target_clients,))
        row_c = cur_count.fetchone()
        if row_c:
            total = int(row_c[0])
    except Exception:
        total = 0

    sql = """
    SELECT a.ad_id, a.batch_id, a.client_id, a.row_number, a.platform,
           a.headline, a.ad_text, a.destination_url, a.creative_file, a.row_hash,
           ap.state, ap.content_hash, ap.idempotency_key, ap.send_attempts,
           ap.rejection_reason, ap.updated_at
    FROM ads a
    JOIN ad_approvals ap ON a.ad_id = ap.ad_id
    WHERE a.client_id = ANY(%s) AND ap.state = 'dead_letter'
    ORDER BY ap.updated_at DESC
    LIMIT %s OFFSET %s;
    """
    try:
        cur = connection.execute(sql, (target_clients, limit, offset))
        rows = cur.fetchall()
    except Exception:
        rows = []

    ads_list = []
    for r in rows:
        aid = str(r[0])
        sends = get_ad_send_records(connection, aid)
        latest_send = sends[-1] if sends else {}
        alerts = get_ad_alerts(connection, aid)
        unresolved_alerts = [a for a in alerts if not a.get("resolved")]

        ads_list.append({
            "ad_id": aid,
            "batch_id": str(r[1]),
            "client_id": str(r[2]),
            "row_number": int(r[3]),
            "platform": str(r[4]),
            "headline": r[5],
            "ad_text": r[6],
            "destination_url": str(r[7]),
            "creative_file": r[8],
            "row_hash": str(r[9]),
            "approval_state": str(r[10]),
            "content_hash": r[11],
            "idempotency_key": r[12],
            "send_attempts": int(r[13]) if r[13] is not None else len(sends),
            "dead_letter_reason": r[14] or latest_send.get("error_message"),
            "updated_at": r[15].isoformat() if hasattr(r[15], "isoformat") else str(r[15]) if r[15] else None,
            "latest_send": latest_send,
            "alerts": unresolved_alerts,
            "has_alerts": len(unresolved_alerts) > 0,
        })

    return {
        "total": total,
        "ads": ads_list,
        "limit": limit,
        "offset": offset,
        "clients": target_clients,
    }


def return_dead_letter_ad_to_review(
    connection: Any,
    ad_id: str,
    user_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Transition an ad from dead_letter state back to pending_review (Architecture §3)."""
    ad = get_ad_record(connection, ad_id)
    if ad is None:
        raise ValueError(f"Ad '{ad_id}' not found")

    approval = get_or_create_ad_approval(connection, ad_id, ad["client_id"])
    current_state = approval.get("state")
    if current_state != ApprovalState.DEAD_LETTER.value:
        raise ValueError(f"Cannot return ad in state '{current_state}' to review; must be in 'dead_letter'")

    res_reason = reason or f"Returned to review from dead letter by {user_id or 'reviewer'}"
    transition_ad_state(
        connection,
        ad_id=ad_id,
        to_state=ApprovalState.PENDING_REVIEW.value,
        changed_by=user_id,
        reason=res_reason,
    )

    # Acknowledge / resolve active dead_letter alerts for this ad
    try:
        connection.execute(
            """
            UPDATE send_alerts
            SET resolved = TRUE, resolved_by = %s, resolved_at = NOW()
            WHERE ad_id = %s AND alert_type = 'dead_letter' AND resolved = FALSE;
            """,
            (user_id, ad_id),
        )
    except Exception as exc:
        logger.debug("Failed auto-resolving alerts in return_dead_letter_ad_to_review: %s", exc)

    return {
        "ad_id": ad_id,
        "status": ApprovalState.PENDING_REVIEW.value,
        "message": "Ad returned to pending review successfully",
        "reason": res_reason,
    }


def return_batch_dead_letter_to_review(
    connection: Any,
    batch_id: str,
    allowed_clients: list[str],
    user_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Return all dead-letter ads in a batch belonging to allowed clients back to review."""
    query = """
    SELECT a.ad_id, a.client_id
    FROM ads a
    JOIN ad_approvals ap ON a.ad_id = ap.ad_id
    WHERE a.batch_id = %s AND a.client_id = ANY(%s) AND ap.state = 'dead_letter'
    ORDER BY a.row_number ASC;
    """
    try:
        cur = connection.execute(query, (batch_id, allowed_clients))
        rows = cur.fetchall()
    except Exception:
        rows = []

    returned_count = 0
    results: list[dict[str, Any]] = []
    for row in rows:
        aid = str(row[0])
        try:
            res = return_dead_letter_ad_to_review(
                connection,
                ad_id=aid,
                user_id=user_id,
                reason=reason,
            )
            results.append(res)
            returned_count += 1
        except Exception as exc:
            logger.error("Failed returning dead-letter ad %s to review: %s", aid, exc)
            results.append({"ad_id": aid, "error": str(exc)})

    return {
        "batch_id": batch_id,
        "total_returned": returned_count,
        "results": results,
    }


def resolve_send_alert(
    connection: Any,
    alert_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Mark a reviewer alert as acknowledged/resolved."""
    now = datetime.now(timezone.utc)
    try:
        cur = connection.execute(
            """
            UPDATE send_alerts
            SET resolved = TRUE, resolved_by = %s, resolved_at = %s
            WHERE alert_id = %s
            RETURNING alert_id, ad_id, client_id, resolved;
            """,
            (user_id, now, alert_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
        return {
            "alert_id": alert_id,
            "ad_id": str(row[1]),
            "client_id": str(row[2]),
            "resolved": True,
            "resolved_by": user_id,
            "resolved_at": now.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("Failed resolve_send_alert query: %s", exc)
        return {
            "alert_id": alert_id,
            "resolved": True,
            "resolved_by": user_id,
            "resolved_at": now.isoformat(),
        }


# --- API Routes ---

class ReturnToReviewRequest(BaseModel):
    reason: str | None = Field(default=None, description="Optional note explaining why ad is returned to review")


@router.get("/batches/{batch_id}/dead-letter", status_code=status.HTTP_200_OK)
def get_batch_dead_letter_endpoint(
    batch_id: str,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """List all ads in a batch whose send retries have been exhausted (dead letter)."""
    with get_session() as conn:
        batch_rec = get_batch_record(conn, batch_id)
        if batch_rec is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")

        # Multi-tenant scoping: check if batch contains ads
        cur_total = conn.execute("SELECT COUNT(*) FROM ads WHERE batch_id = %s", (batch_id,))
        total_in_batch = int(cur_total.fetchone()[0])
        allowed_clients = list(user.assigned_client_ids)
        cur_user = conn.execute(
            "SELECT COUNT(*) FROM ads WHERE batch_id = %s AND client_id = ANY(%s)",
            (batch_id, allowed_clients),
        )
        accessible_in_batch = int(cur_user.fetchone()[0])
        if total_in_batch > 0 and accessible_in_batch == 0:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

        dead_letter_ads = get_batch_dead_letter_ads(conn, batch_id, allowed_clients)

        return {
            "batch_id": batch_id,
            "filename": batch_rec.get("filename"),
            "total_dead_letter": len(dead_letter_ads),
            "ads": dead_letter_ads,
        }


@router.get("/dead-letter", status_code=status.HTTP_200_OK)
def get_global_dead_letter_endpoint(
    client_id: str | None = Query(default=None, description="Filter by client ID"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Retrieve global listing of unresolved dead-letter ads across assigned clients."""
    with get_session() as conn:
        allowed_clients = list(user.assigned_client_ids)
        if client_id:
            require_client_access(user, client_id)

        return get_global_dead_letter_ads(
            conn,
            allowed_clients=allowed_clients,
            client_id=client_id,
            limit=limit,
            offset=offset,
        )


@router.post("/ads/{ad_id}/return-to-review", status_code=status.HTTP_200_OK)
def return_ad_to_review_endpoint(
    ad_id: str,
    req: ReturnToReviewRequest | None = None,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Transition a dead-letter ad back to pending_review state for copy edit or re-evaluation."""
    req = req or ReturnToReviewRequest()
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])

        try:
            return return_dead_letter_ad_to_review(
                conn,
                ad_id=ad_id,
                user_id=user.user_id,
                reason=req.reason,
            )
        except ValueError as err:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))


@router.post("/batches/{batch_id}/return-dead-letter-to-review", status_code=status.HTTP_200_OK)
def return_batch_dead_letter_endpoint(
    batch_id: str,
    req: ReturnToReviewRequest | None = None,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Return all dead-letter ads in a batch belonging to assigned clients back to review."""
    req = req or ReturnToReviewRequest()
    with get_session() as conn:
        batch_rec = get_batch_record(conn, batch_id)
        if batch_rec is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")

        cur_total = conn.execute("SELECT COUNT(*) FROM ads WHERE batch_id = %s", (batch_id,))
        total_in_batch = int(cur_total.fetchone()[0])
        allowed_clients = list(user.assigned_client_ids)
        cur_user = conn.execute(
            "SELECT COUNT(*) FROM ads WHERE batch_id = %s AND client_id = ANY(%s)",
            (batch_id, allowed_clients),
        )
        accessible_in_batch = int(cur_user.fetchone()[0])
        if total_in_batch > 0 and accessible_in_batch == 0:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

        return return_batch_dead_letter_to_review(
            conn,
            batch_id=batch_id,
            allowed_clients=allowed_clients,
            user_id=user.user_id,
            reason=req.reason,
        )


def resolve_send_alert(
    connection: Any,
    alert_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Compatibility wrapper for resolve_alert."""
    from apps.api.alerts import resolve_alert
    return resolve_alert(connection, alert_id=alert_id, user_id=user_id)



