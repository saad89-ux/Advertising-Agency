"""Reviewer ad edit handling with reason persistence and bounded re-check loops.

Implements PRD 4.6, D13, D17 and Architecture §1:
- Reviewer edits ad copy/headline/URL with mandatory reason.
- Edits, reasons, timestamps, and content snapshots are permanently saved.
- Ad row hash is updated.
- Approval state resets to 'pending_review' (invalidating prior approvals).
- Enforces bounded re-check loop (capped by MAX_EDIT_LOOPS).
- Re-enqueues ad into the outbox/queue for worker re-evaluation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api.access import require_client_access
from apps.api.ads import create_ads_table
from apps.api.approvals import (
    ApprovalState,
    build_canonical_content_snapshot,
    get_ad_record,
    get_or_create_ad_approval,
    transition_ad_state,
)
from apps.api.auth import CurrentUser, get_current_user, require_role
from apps.api.database import get_session
from apps.api.ingest import hash_normalized_row
from apps.api.outbox import create_ad_job_outbox_table
from apps.api.queue import AdJob, enqueue_ad_jobs
from apps.api.settings import max_edit_loops

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ads", tags=["edits"])


# --- DDL Statements ---

CREATE_AD_EDITS_TABLE = """
CREATE TABLE IF NOT EXISTS ad_edits (
    edit_id BIGSERIAL PRIMARY KEY,
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id),
    edited_by TEXT NOT NULL REFERENCES users (user_id),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    previous_content TEXT NOT NULL,
    new_content TEXT NOT NULL,
    previous_row_hash TEXT NOT NULL,
    new_row_hash TEXT NOT NULL,
    edit_iteration INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
"""


def create_ad_edits_table() -> None:
    """Ensure dependencies exist, then create ad_edits audit storage."""
    create_ads_table()
    create_ad_job_outbox_table()
    with get_session() as conn:
        conn.execute(CREATE_AD_EDITS_TABLE)


# --- Re-check Queue Trigger ---

def rearm_ad_for_recheck(connection: Any, batch_id: str, ad_id: str) -> None:
    """Re-arm an outbox entry and enqueue the ad for bounded re-check."""
    try:
        connection.execute(
            """
            INSERT INTO ad_job_outbox (batch_id, ad_id, created_at, queued_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP, NULL)
            ON CONFLICT (batch_id, ad_id) DO UPDATE SET queued_at = NULL, created_at = CURRENT_TIMESTAMP;
            """,
            (batch_id, ad_id),
        )
    except Exception as exc:
        logger.debug("Skipping outbox table re-arm: %s", exc)

    try:
        enqueue_ad_jobs([AdJob(batch_id=batch_id, ad_id=ad_id)])
    except Exception as exc:
        logger.debug("Redis queue dispatch deferred: %s", exc)


# --- Core Edit Logic ---

def count_ad_edits(connection: Any, ad_id: str) -> int:
    """Count how many times an ad has been edited."""
    try:
        cur = connection.execute(
            "SELECT COUNT(*) FROM ad_edits WHERE ad_id = %s;",
            (ad_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def get_ad_edits_history(connection: Any, ad_id: str) -> list[dict[str, Any]]:
    """Retrieve all edit records for an ad ordered by iteration."""
    try:
        cur = connection.execute(
            """
            SELECT edit_id, ad_id, batch_id, client_id, edited_by, reason,
                   previous_content, new_content, previous_row_hash, new_row_hash,
                   edit_iteration, created_at
            FROM ad_edits
            WHERE ad_id = %s
            ORDER BY edit_iteration ASC, created_at ASC;
            """,
            (ad_id,),
        )
        edits = []
        for r in cur.fetchall():
            prev_content = r[6]
            if isinstance(prev_content, str):
                try:
                    prev_content = json.loads(prev_content)
                except Exception:
                    pass
            new_content = r[7]
            if isinstance(new_content, str):
                try:
                    new_content = json.loads(new_content)
                except Exception:
                    pass

            edits.append({
                "edit_id": int(r[0]),
                "ad_id": str(r[1]),
                "batch_id": str(r[2]),
                "client_id": str(r[3]),
                "edited_by": str(r[4]),
                "reason": str(r[5]),
                "previous_content": prev_content,
                "new_content": new_content,
                "previous_row_hash": str(r[8]),
                "new_row_hash": str(r[9]),
                "edit_iteration": int(r[10]),
                "created_at": r[11].isoformat() if hasattr(r[11], "isoformat") else str(r[11]),
            })
        return edits
    except Exception:
        return []


def perform_ad_edit(
    connection: Any,
    ad_id: str,
    user: CurrentUser,
    reason: str,
    headline: str | None = None,
    ad_text: str | None = None,
    destination_url: str | None = None,
    creative_file: str | None = None,
) -> dict[str, Any]:
    """Execute reviewer edit, enforce bounds, save reason, update hash, and reset state."""
    clean_reason = reason.strip() if reason else ""
    if not clean_reason:
        raise HTTPException(
            status_code=422,
            detail="edit reason is required and cannot be blank",
        )

    ad = get_ad_record(connection, ad_id)
    if ad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

    # Client assignment check (Needs: 7)
    require_client_access(user, ad["client_id"])

    # State check: Cannot edit an ad in sending or sent state
    approval = get_or_create_ad_approval(connection, ad_id, ad["client_id"])
    current_state = approval.get("state")
    if current_state in (ApprovalState.SENDING.value, ApprovalState.SENT.value):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot edit ad in '{current_state}' state",
        )

    # Bounded re-check loop enforcement (PRD D17)
    current_edit_count = count_ad_edits(connection, ad_id)
    loop_cap = max_edit_loops()
    if current_edit_count >= loop_cap:
        raise HTTPException(
            status_code=422,
            detail=f"maximum edit loop cap ({loop_cap}) reached for ad {ad_id}; cannot perform further edits",
        )
    edit_iteration = current_edit_count + 1

    # Compare content
    previous_snapshot = build_canonical_content_snapshot(ad)
    previous_row_hash = ad["row_hash"]

    updated_headline = headline if headline is not None else ad["headline"]
    updated_ad_text = ad_text if ad_text is not None else ad["ad_text"]
    updated_url = destination_url if destination_url is not None else ad["destination_url"]
    updated_creative = creative_file if creative_file is not None else (ad.get("creative_file") or "")

    new_snapshot = {
        "platform": ad["platform"],
        "headline": str(updated_headline or ""),
        "ad_text": str(updated_ad_text or ""),
        "destination_url": str(updated_url or ""),
        "creative_file": str(updated_creative or ""),
    }

    # At least one field must change
    if new_snapshot == previous_snapshot:
        raise HTTPException(
            status_code=422,
            detail="no changes detected in edited fields",
        )

    new_row_hash = hash_normalized_row(new_snapshot)

    # 1. Update ads table
    connection.execute(
        """
        UPDATE ads
        SET headline = %s,
            ad_text = %s,
            destination_url = %s,
            creative_file = %s,
            row_hash = %s
        WHERE ad_id = %s;
        """,
        (updated_headline, updated_ad_text, updated_url, updated_creative, new_row_hash, ad_id),
    )

    # 2. Record edit in ad_edits table
    connection.execute(
        """
        INSERT INTO ad_edits (
            ad_id, batch_id, client_id, edited_by, reason,
            previous_content, new_content, previous_row_hash, new_row_hash, edit_iteration
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        (
            ad_id,
            ad["batch_id"],
            ad["client_id"],
            user.user_id,
            clean_reason,
            json.dumps(previous_snapshot),
            json.dumps(new_snapshot),
            previous_row_hash,
            new_row_hash,
            edit_iteration,
        ),
    )

    # 3. Reset approval state to 'pending_review' (PRD D13)
    # If the ad was previously approved or rejected, reset to pending_review
    if current_state != ApprovalState.PENDING_REVIEW.value:
        transition_ad_state(
            connection,
            ad_id=ad_id,
            to_state=ApprovalState.PENDING_REVIEW.value,
            changed_by=user.user_id,
            reason=f"Ad edited: {clean_reason}",
        )

    # 4. Re-arm outbox job for worker re-check in bounded loop (PRD D17)
    rearm_ad_for_recheck(connection, batch_id=ad["batch_id"], ad_id=ad_id)

    return {
        "ad_id": ad_id,
        "batch_id": ad["batch_id"],
        "client_id": ad["client_id"],
        "headline": updated_headline,
        "ad_text": updated_ad_text,
        "destination_url": updated_url,
        "creative_file": updated_creative,
        "previous_row_hash": previous_row_hash,
        "new_row_hash": new_row_hash,
        "state": ApprovalState.PENDING_REVIEW.value,
        "edit_iteration": edit_iteration,
        "reason": clean_reason,
        "edited_by": user.user_id,
        "recheck_queued": True,
    }


# --- Request Models ---

class EditAdRequest(BaseModel):
    headline: str | None = Field(default=None, description="Updated headline text")
    ad_text: str | None = Field(default=None, description="Updated ad copy body")
    destination_url: str | None = Field(default=None, description="Updated destination link / UTMs")
    creative_file: str | None = Field(default=None, description="Updated creative asset file name")
    reason: str = Field(..., min_length=1, description="Mandatory reason for the edit (PRD 4.6)")


# --- API Endpoints ---

@router.post("/{ad_id}/edit", status_code=status.HTTP_200_OK)
def edit_ad(
    ad_id: str,
    req: EditAdRequest,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Edit ad copy or headline, save reason, update row hash, and trigger re-check loop."""
    with get_session() as conn:
        return perform_ad_edit(
            connection=conn,
            ad_id=ad_id,
            user=user,
            reason=req.reason,
            headline=req.headline,
            ad_text=req.ad_text,
            destination_url=req.destination_url,
            creative_file=req.creative_file,
        )


@router.get("/{ad_id}/edits", status_code=status.HTTP_200_OK)
def get_ad_edits(
    ad_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve full audit history of edits and reasons for an ad."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])
        edits = get_ad_edits_history(conn, ad_id)
        return {
            "ad_id": ad_id,
            "total_edits": len(edits),
            "max_edit_loops": max_edit_loops(),
            "edits": edits,
        }


@router.post("/{ad_id}/recheck", status_code=status.HTTP_200_OK)
def trigger_ad_recheck(
    ad_id: str,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Execute bounded re-check flow on an edited ad (PRD D17, Needs: 34, 41)."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])

        approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        current_state = approval.get("state")
        if current_state in (ApprovalState.SENDING.value, ApprovalState.SENT.value):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"cannot re-check ad in '{current_state}' state",
            )

        from apps.worker.recheck import recheck_ad
        result = recheck_ad(ad_id, connection=conn)

        return {
            "ad_id": result.ad.ad_id,
            "batch_id": result.ad.batch_id,
            "client_id": result.ad.client_id,
            "verdict": result.verdict.status.value,
            "verdict_summary": result.verdict.summary,
            "findings_count": len(result.findings),
            "findings": [f.model_dump(mode="json") for f in result.findings],
            "edit_iteration": result.edit_iteration,
            "max_loops": result.max_loops,
            "is_loop_capped": result.is_loop_capped,
            "needs_person": result.needs_person,
            "step_count": result.step_count,
            "trace_id": result.trace_id,
            "row_hash": result.new_row_hash,
            "is_ready_for_approval": result.verdict.status.value == "ready",
        }
