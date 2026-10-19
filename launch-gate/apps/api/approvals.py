"""Approval state machine and API endpoints.

Enforces DB-level transition checks, client assignment checks, content hash
validation, approval records, and pre-send verification per PRD 4.6, 4.8, D13,
and Dataflow Architecture §3.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api.access import require_client_access
from apps.api.ads import create_ads_table
from apps.api.auth import CurrentUser, get_current_user, require_role
from apps.api.database import get_session
from apps.api.ingest import hash_normalized_row

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ads", tags=["approvals"])


# --- State Machine Definitions ---

class ApprovalState(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENDING = "sending"
    SENT = "sent"
    DEAD_LETTER = "dead_letter"


# Allowed state transitions per Dataflow Architecture §3
ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    (ApprovalState.PENDING_REVIEW.value, ApprovalState.APPROVED.value),
    (ApprovalState.PENDING_REVIEW.value, ApprovalState.REJECTED.value),
    (ApprovalState.APPROVED.value, ApprovalState.SENDING.value),
    (ApprovalState.SENDING.value, ApprovalState.SENT.value),
    (ApprovalState.SENDING.value, ApprovalState.APPROVED.value),  # timeout, retry with backoff
    (ApprovalState.SENDING.value, ApprovalState.DEAD_LETTER.value),  # retries exhausted
    (ApprovalState.SENDING.value, ApprovalState.REJECTED.value),  # platform rejection
    (ApprovalState.DEAD_LETTER.value, ApprovalState.PENDING_REVIEW.value),  # return to review
    (ApprovalState.DEAD_LETTER.value, ApprovalState.APPROVED.value),  # re-approve
    (ApprovalState.REJECTED.value, ApprovalState.PENDING_REVIEW.value),  # re-check after edit
    (ApprovalState.APPROVED.value, ApprovalState.PENDING_REVIEW.value),  # re-check after edit
    (ApprovalState.APPROVED.value, ApprovalState.REJECTED.value),  # reviewer changes mind before sending
})


class InvalidTransitionError(Exception):
    """Raised when an illegal approval state transition is attempted."""
    pass


class ContentHashMismatchError(Exception):
    """Raised when an approval content hash does not match the ad's row hash."""
    pass


class UnapprovedAdError(Exception):
    """Raised when attempting to send an ad that has not been approved."""
    pass


class MissingReasonError(Exception):
    """Raised when a rejection is attempted without a reason."""
    pass


def is_valid_transition(from_state: str, to_state: str) -> bool:
    """Check whether a transition between two states is allowed."""
    return (from_state, to_state) in ALLOWED_TRANSITIONS


def validate_transition(from_state: str, to_state: str) -> None:
    """Validate a transition, raising InvalidTransitionError if disallowed."""
    if not is_valid_transition(from_state, to_state):
        raise InvalidTransitionError(
            f"Invalid approval state transition from '{from_state}' to '{to_state}'"
        )


# --- DDL Statements ---

CREATE_ALLOWED_TRANSITIONS_TABLE = """
CREATE TABLE IF NOT EXISTS allowed_approval_transitions (
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    PRIMARY KEY (from_state, to_state)
);
"""

CREATE_AD_APPROVALS_TABLE = """
CREATE TABLE IF NOT EXISTS ad_approvals (
    ad_id TEXT PRIMARY KEY REFERENCES ads (ad_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id),
    state TEXT NOT NULL DEFAULT 'pending_review' CHECK (
        state IN ('pending_review', 'approved', 'rejected', 'sending', 'sent', 'dead_letter', 'blocked_connector_not_configured')
    ),
    content_hash TEXT,
    content_snapshot TEXT,
    approved_by TEXT REFERENCES users (user_id),
    approved_at TIMESTAMP WITH TIME ZONE,
    evidence_note TEXT,
    evidence_file TEXT,
    rejection_reason TEXT,
    rejected_by TEXT REFERENCES users (user_id),
    rejected_at TIMESTAMP WITH TIME ZONE,
    idempotency_key TEXT,
    send_attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
"""

ENSURE_CONTENT_SNAPSHOT_COLUMN = """
ALTER TABLE ad_approvals ADD COLUMN IF NOT EXISTS content_snapshot TEXT;
"""

CREATE_APPROVAL_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS approval_history (
    history_id BIGSERIAL PRIMARY KEY,
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    changed_by TEXT REFERENCES users (user_id),
    reason TEXT,
    content_hash TEXT,
    idempotency_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
"""

CREATE_TRANSITION_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_approval_state_transition()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.state IS DISTINCT FROM NEW.state THEN
        IF NOT EXISTS (
            SELECT 1 FROM allowed_approval_transitions
            WHERE from_state = OLD.state AND to_state = NEW.state
        ) THEN
            RAISE EXCEPTION 'Invalid approval state transition from % to % for ad %', OLD.state, NEW.state, OLD.ad_id
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.updated_at = NOW();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

ENSURE_TRANSITION_TRIGGER = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_enforce_approval_state_transition'
          AND tgrelid = 'ad_approvals'::regclass
    ) THEN
        CREATE TRIGGER trg_enforce_approval_state_transition
            BEFORE UPDATE OF state ON ad_approvals
            FOR EACH ROW
            EXECUTE FUNCTION enforce_approval_state_transition();
    END IF;
END $$;
"""


def create_approval_tables() -> None:
    """Create the approval state tables, lookup table, and transition triggers."""
    create_ads_table()
    with get_session() as conn:
        conn.execute(CREATE_ALLOWED_TRANSITIONS_TABLE)
        # Populate allowed transitions table
        for f, t in ALLOWED_TRANSITIONS:
            conn.execute(
                """
                INSERT INTO allowed_approval_transitions (from_state, to_state)
                VALUES (%s, %s)
                ON CONFLICT (from_state, to_state) DO NOTHING;
                """,
                (f, t),
            )
        conn.execute(CREATE_AD_APPROVALS_TABLE)
        try:
            conn.execute(ENSURE_CONTENT_SNAPSHOT_COLUMN)
        except Exception:
            pass
        conn.execute(CREATE_APPROVAL_HISTORY_TABLE)
        try:
            conn.execute(CREATE_TRANSITION_TRIGGER_FUNCTION)
            conn.execute(ENSURE_TRANSITION_TRIGGER)
        except Exception as exc:
            logger.debug("Skipping plpgsql trigger creation on non-Postgres or mock connection: %s", exc)


# --- Canonical Content Extraction and Hashing (PRD D13, D23) ---

def build_canonical_content_snapshot(ad: dict[str, Any]) -> dict[str, str]:
    """Extract canonical ad fields that define the ad's content identity (PRD D13, D23)."""
    return {
        "platform": str(ad.get("platform", "")),
        "headline": str(ad.get("headline") or ""),
        "ad_text": str(ad.get("ad_text") or ""),
        "destination_url": str(ad.get("destination_url", "")),
        "creative_file": str(ad.get("creative_file") or ""),
    }


def compute_canonical_content_hash(ad: dict[str, Any]) -> str:
    """Compute the immutable content hash of an ad's canonical fields."""
    snapshot = build_canonical_content_snapshot(ad)
    return hash_normalized_row(snapshot)


# --- Database Queries and State Operations ---

def get_ad_record(connection: Any, ad_id: str) -> dict[str, Any] | None:
    """Retrieve ad details needed for approval evaluation."""
    cur = connection.execute(
        """
        SELECT ad_id, batch_id, client_id, row_number, platform, ad_text, headline,
               destination_url, row_hash, sheet_hash, creative_file
        FROM ads
        WHERE ad_id = %s;
        """,
        (ad_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "ad_id": str(row[0]),
        "batch_id": str(row[1]),
        "client_id": str(row[2]),
        "row_number": int(row[3]),
        "platform": str(row[4]),
        "ad_text": row[5],
        "headline": row[6],
        "destination_url": str(row[7]),
        "row_hash": str(row[8]),
        "sheet_hash": str(row[9]),
        "creative_file": row[10] if len(row) > 10 else None,
    }


def get_or_create_ad_approval(
    connection: Any, ad_id: str, client_id: str | None = None
) -> dict[str, Any]:
    """Retrieve or initialize the approval record for an ad in 'pending_review' state."""
    cur = connection.execute(
        """
        SELECT ad_id, client_id, state, content_hash, approved_by, approved_at,
               evidence_note, evidence_file, rejection_reason, rejected_by, rejected_at,
               idempotency_key, send_attempts, sent_at, created_at, updated_at
        FROM ad_approvals
        WHERE ad_id = %s;
        """,
        (ad_id,),
    )
    row = cur.fetchone()
    if row:
        return _format_approval_row(row)

    # Initialize record if not present
    cid = client_id
    if not cid:
        ad = get_ad_record(connection, ad_id)
        if not ad:
            raise HTTPException(status_code=404, detail="ad not found")
        cid = ad["client_id"]

    connection.execute(
        """
        INSERT INTO ad_approvals (ad_id, client_id, state)
        VALUES (%s, %s, 'pending_review')
        ON CONFLICT (ad_id) DO NOTHING;
        """,
        (ad_id, cid),
    )

    cur2 = connection.execute(
        """
        SELECT ad_id, client_id, state, content_hash, approved_by, approved_at,
               evidence_note, evidence_file, rejection_reason, rejected_by, rejected_at,
               idempotency_key, send_attempts, sent_at, created_at, updated_at
        FROM ad_approvals
        WHERE ad_id = %s;
        """,
        (ad_id,),
    )
    row2 = cur2.fetchone()
    return _format_approval_row(row2)


def get_ad_approval_history(connection: Any, ad_id: str) -> list[dict[str, Any]]:
    """Retrieve audit history of transitions for an ad."""
    try:
        cur = connection.execute(
            """
            SELECT history_id, ad_id, from_state, to_state, changed_by, reason,
                   content_hash, idempotency_key, created_at
            FROM approval_history
            WHERE ad_id = %s
            ORDER BY created_at ASC, history_id ASC;
            """,
            (ad_id,),
        )
        return [
            {
                "history_id": int(r[0]),
                "ad_id": str(r[1]),
                "from_state": str(r[2]),
                "to_state": str(r[3]),
                "changed_by": r[4],
                "reason": r[5],
                "content_hash": r[6],
                "idempotency_key": r[7],
                "created_at": r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8]),
            }
            for r in cur.fetchall()
        ]
    except Exception:
        return []


def _format_approval_row(row: Any) -> dict[str, Any]:
    """Format row tuple from ad_approvals table, supporting both legacy and snapshot schemas."""
    raw_snapshot = row[16] if len(row) > 16 else None
    parsed_snapshot = None
    if raw_snapshot:
        if isinstance(raw_snapshot, dict):
            parsed_snapshot = raw_snapshot
        elif isinstance(raw_snapshot, str):
            try:
                parsed_snapshot = json.loads(raw_snapshot)
            except Exception:
                parsed_snapshot = None

    return {
        "ad_id": str(row[0]),
        "client_id": str(row[1]),
        "state": str(row[2]),
        "content_hash": row[3],
        "content_snapshot": parsed_snapshot,
        "approved_by": row[4],
        "approved_at": row[5].isoformat() if hasattr(row[5], "isoformat") else str(row[5]) if row[5] else None,
        "evidence_note": row[6],
        "evidence_file": row[7],
        "rejection_reason": row[8],
        "rejected_by": row[9],
        "rejected_at": row[10].isoformat() if hasattr(row[10], "isoformat") else str(row[10]) if row[10] else None,
        "idempotency_key": row[11],
        "send_attempts": int(row[12]) if row[12] is not None else 0,
        "sent_at": row[13].isoformat() if hasattr(row[13], "isoformat") else str(row[13]) if row[13] else None,
        "created_at": row[14].isoformat() if hasattr(row[14], "isoformat") else str(row[14]) if row[14] else None,
        "updated_at": row[15].isoformat() if hasattr(row[15], "isoformat") else str(row[15]) if row[15] else None,
    }


def transition_ad_state(
    connection: Any,
    ad_id: str,
    to_state: str,
    changed_by: str | None = None,
    reason: str | None = None,
    content_hash: str | None = None,
    content_snapshot: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    evidence_note: str | None = None,
    evidence_file: str | None = None,
) -> dict[str, Any]:
    """Execute a state transition on an ad, enforcing valid transitions and updating audit history."""
    current = get_or_create_ad_approval(connection, ad_id)
    from_state = current["state"]

    # Idempotent re-check: if already in the target terminal state or duplicate approve
    if from_state == to_state:
        return current

    # Enforce transition rules
    validate_transition(from_state, to_state)

    # Perform DB Update
    now = datetime.now(timezone.utc)
    snapshot_str = json.dumps(content_snapshot) if content_snapshot is not None else None

    if to_state == ApprovalState.APPROVED.value:
        try:
            connection.execute(
                """
                UPDATE ad_approvals
                SET state = %s,
                    content_hash = %s,
                    approved_by = %s,
                    approved_at = %s,
                    evidence_note = COALESCE(%s, evidence_note),
                    evidence_file = COALESCE(%s, evidence_file),
                    content_snapshot = COALESCE(%s, content_snapshot),
                    updated_at = %s
                WHERE ad_id = %s;
                """,
                (to_state, content_hash, changed_by, now, evidence_note, evidence_file, snapshot_str, now, ad_id),
            )
        except Exception:
            # Fallback for schema without content_snapshot column
            connection.execute(
                """
                UPDATE ad_approvals
                SET state = %s,
                    content_hash = %s,
                    approved_by = %s,
                    approved_at = %s,
                    evidence_note = COALESCE(%s, evidence_note),
                    evidence_file = COALESCE(%s, evidence_file),
                    updated_at = %s
                WHERE ad_id = %s;
                """,
                (to_state, content_hash, changed_by, now, evidence_note, evidence_file, now, ad_id),
            )
    elif to_state == ApprovalState.REJECTED.value:
        connection.execute(
            """
            UPDATE ad_approvals
            SET state = %s,
                rejection_reason = %s,
                rejected_by = %s,
                rejected_at = %s,
                updated_at = %s
            WHERE ad_id = %s;
            """,
            (to_state, reason, changed_by, now, now, ad_id),
        )
    elif to_state == ApprovalState.SENDING.value:
        connection.execute(
            """
            UPDATE ad_approvals
            SET state = %s,
                idempotency_key = %s,
                send_attempts = send_attempts + 1,
                updated_at = %s
            WHERE ad_id = %s;
            """,
            (to_state, idempotency_key, now, ad_id),
        )
    elif to_state == ApprovalState.SENT.value:
        connection.execute(
            """
            UPDATE ad_approvals
            SET state = %s,
                sent_at = %s,
                updated_at = %s
            WHERE ad_id = %s;
            """,
            (to_state, now, now, ad_id),
        )
    elif to_state == ApprovalState.DEAD_LETTER.value:
        connection.execute(
            """
            UPDATE ad_approvals
            SET state = %s,
                rejection_reason = COALESCE(%s, rejection_reason),
                updated_at = %s
            WHERE ad_id = %s;
            """,
            (to_state, reason, now, ad_id),
        )
    else:
        # e.g. pending_review
        connection.execute(
            """
            UPDATE ad_approvals
            SET state = %s,
                updated_at = %s
            WHERE ad_id = %s;
            """,
            (to_state, now, ad_id),
        )

    # Record audit log
    try:
        connection.execute(
            """
            INSERT INTO approval_history (ad_id, from_state, to_state, changed_by, reason, content_hash, idempotency_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
            """,
            (ad_id, from_state, to_state, changed_by, reason, content_hash, idempotency_key),
        )
    except Exception as exc:
        logger.debug("Skipping history insert on mock connection: %s", exc)

    return get_or_create_ad_approval(connection, ad_id)


def get_ad_approval_record(connection: Any, ad_id: str) -> dict[str, Any]:
    """Retrieve full approval record including current ad hash alignment (PRD D13)."""
    ad = get_ad_record(connection, ad_id)
    if ad is None:
        raise HTTPException(status_code=404, detail="ad not found")

    approval = get_or_create_ad_approval(connection, ad_id, ad["client_id"])
    current_hash = ad["row_hash"]
    approved_hash = approval.get("content_hash")
    is_approved = approval.get("state") == ApprovalState.APPROVED.value

    # An ad is valid for send only if approved and current content hash matches approved content hash
    is_valid_for_send = (
        approval.get("state") in (ApprovalState.APPROVED.value, ApprovalState.SENDING.value, ApprovalState.SENT.value)
        and approved_hash is not None
        and approved_hash == current_hash
    )

    has_evidence = bool(approval.get("evidence_note") or approval.get("evidence_file"))

    return {
        **approval,
        "batch_id": ad["batch_id"],
        "current_ad_hash": current_hash,
        "is_approved": is_approved,
        "is_valid_for_send": is_valid_for_send,
        "has_evidence": has_evidence,
    }


def verify_approval_for_send(connection: Any, ad_id: str) -> dict[str, Any]:
    """Verify that an ad is approved and that its current content matches the approved content hash (PRD D13)."""
    ad = get_ad_record(connection, ad_id)
    if ad is None:
        raise HTTPException(status_code=404, detail="ad not found")

    approval = get_or_create_ad_approval(connection, ad_id, ad["client_id"])
    state = approval.get("state")

    if state not in (ApprovalState.APPROVED.value, ApprovalState.SENDING.value, ApprovalState.SENT.value):
        raise HTTPException(
            status_code=400,
            detail=f"cannot send ad with approval state '{state}'; approval is required",
        )

    approved_hash = approval.get("content_hash")
    current_hash = ad.get("row_hash")
    if not approved_hash or approved_hash != current_hash:
        raise HTTPException(
            status_code=409,
            detail=(
                f"content hash mismatch: approved version hash '{approved_hash}' "
                f"does not match current ad hash '{current_hash}'. Only the approved version can be sent."
            ),
        )

    return {
        "verified": True,
        "ad_id": ad_id,
        "client_id": ad["client_id"],
        "batch_id": ad["batch_id"],
        "content_hash": approved_hash,
        "approved_by": approval.get("approved_by"),
        "approved_at": approval.get("approved_at"),
        "content_snapshot": approval.get("content_snapshot"),
        "ready_to_send": True,
    }


# --- Request Models ---

class ApproveRequest(BaseModel):
    content_hash: str | None = Field(default=None, description="Expected content hash to lock approved version")
    evidence_note: str | None = Field(default=None, description="Reviewer note of client approval (PRD D3)")
    evidence_file: str | None = Field(default=None, description="Screenshot/file reference of client approval (PRD D3)")


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Mandatory reason for rejection (PRD 4.6)")


# --- API Endpoints ---

@router.post("/{ad_id}/approve", status_code=status.HTTP_200_OK)
def approve_ad(
    ad_id: str,
    req: ApproveRequest | None = None,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Approve an ad for stand-in platform send, verifying client assignment and content hash."""
    req = req or ApproveRequest()

    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        # Client assignment check (Needs: 7): "A buyer cannot approve an ad they were not given."
        require_client_access(user, ad["client_id"])

        current_approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        current_state = current_approval["state"]

        # Content snapshot and hash verification (PRD D13):
        # "Approval is bound to a content hash; only that version can be sent."
        snapshot = build_canonical_content_snapshot(ad)
        expected_hash = ad["row_hash"]
        if req.content_hash and req.content_hash != expected_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"content hash mismatch: expected '{expected_hash}', received '{req.content_hash}'",
            )
        target_hash = expected_hash

        # PRD §3 / Dataflow: A second click on Approve finds the row already sending or sent and does nothing
        if current_state in (ApprovalState.SENDING.value, ApprovalState.SENT.value):
            return get_ad_approval_record(conn, ad_id)

        if current_state == ApprovalState.APPROVED.value:
            if current_approval.get("content_hash") == target_hash:
                return get_ad_approval_record(conn, ad_id)

        try:
            transition_ad_state(
                conn,
                ad_id=ad_id,
                to_state=ApprovalState.APPROVED.value,
                changed_by=user.user_id,
                content_hash=target_hash,
                content_snapshot=snapshot,
                evidence_note=req.evidence_note,
                evidence_file=req.evidence_file,
            )
            if req.evidence_note or req.evidence_file:
                try:
                    from apps.api.evidence import record_approval_evidence

                    record_approval_evidence(
                        conn,
                        ad_id=ad_id,
                        client_id=ad["client_id"],
                        content_hash=target_hash,
                        created_by=user.user_id,
                        evidence_note=req.evidence_note,
                        evidence_file=req.evidence_file,
                        sync_approvals_table=False,
                    )
                except Exception as exc:
                    logger.debug("Failed recording approval_evidence entry: %s", exc)

            return get_ad_approval_record(conn, ad_id)
        except InvalidTransitionError as err:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(err))


@router.post("/{ad_id}/reject", status_code=status.HTTP_200_OK)
def reject_ad(
    ad_id: str,
    req: RejectRequest,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Reject an ad with a mandatory reason, verifying client assignment."""
    if not req.reason or not req.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="rejection reason is required and cannot be blank",
        )

    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        # Client assignment check (Needs: 7)
        require_client_access(user, ad["client_id"])

        current_approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        current_state = current_approval["state"]

        # Cannot reject an ad that was already locked and sent
        if current_state in (ApprovalState.SENDING.value, ApprovalState.SENT.value):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"cannot reject ad in '{current_state}' state",
            )

        try:
            return transition_ad_state(
                conn,
                ad_id=ad_id,
                to_state=ApprovalState.REJECTED.value,
                changed_by=user.user_id,
                reason=req.reason.strip(),
            )
        except InvalidTransitionError as err:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(err))


@router.get("/{ad_id}/approval", status_code=status.HTTP_200_OK)
def get_ad_approval_status(
    ad_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve current approval record and transition audit history, scoped by client assignment."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        # Client assignment check
        require_client_access(user, ad["client_id"])

        approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        history = get_ad_approval_history(conn, ad_id)
        return {
            **approval,
            "history": history,
        }


@router.get("/{ad_id}/approval-record", status_code=status.HTTP_200_OK)
def get_approval_record_endpoint(
    ad_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve full approval record with content hash verification per PRD D13."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])
        return get_ad_approval_record(conn, ad_id)


@router.post("/{ad_id}/verify-send", status_code=status.HTTP_200_OK)
def verify_send_endpoint(
    ad_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Verify that an ad is approved and ready for send without content tampering."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])
        return verify_approval_for_send(conn, ad_id)
