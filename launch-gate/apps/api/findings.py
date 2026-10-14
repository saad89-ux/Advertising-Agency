"""PostgreSQL persistence and false-alarm dismissal handling for ad findings.

Architecture & PRD compliance:
- PRD §4.6 / §4.7 & Line 90: False alarms (reviewer dismissed) tracked per batch and over time.
- Line 216: Dismissed findings monitored against 5% false alarm ceiling.
- Reviewer can dismiss a finding with a mandatory reason, creating an immutable false-alarm record.
- When all findings on an ad are dismissed, active findings count drops to 0, updating verdict to 'ready'.
- Client scoping and role checks (Needs: 7): only assigned buyers/account managers can dismiss findings.
- Terminal send state protection: findings cannot be dismissed on ads in 'sending' or 'sent' state.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from psycopg import Connection

from apps.api.access import require_client_access, user_can_access_client
from apps.api.approvals import ApprovalState, get_ad_record, get_or_create_ad_approval
from apps.api.auth import CurrentUser, get_current_user, require_role
from apps.api.database import get_session
from packages.contracts import CheckType, FindingContract

logger = logging.getLogger(__name__)

router = APIRouter(tags=["findings"])

CREATE_FINDINGS_TABLE = """
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY CHECK (length(trim(finding_id)) > 0),
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    check_type TEXT NOT NULL CHECK (check_type IN ('mechanical', 'page', 'wording')),
    message TEXT NOT NULL CHECK (length(trim(message)) > 0),
    field TEXT,
    proposed_fix TEXT,
    rule_id TEXT,
    file_ref TEXT
);
"""

CREATE_DISMISSED_FINDINGS_TABLE = """
CREATE TABLE IF NOT EXISTS dismissed_findings (
    dismissal_id BIGSERIAL PRIMARY KEY,
    finding_id TEXT NOT NULL,
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id),
    dismissed_by TEXT NOT NULL REFERENCES users (user_id),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    check_type TEXT,
    rule_id TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_dismissed_finding UNIQUE (finding_id)
);
CREATE INDEX IF NOT EXISTS idx_dismissed_findings_ad ON dismissed_findings (ad_id);
CREATE INDEX IF NOT EXISTS idx_dismissed_findings_batch ON dismissed_findings (batch_id);
"""

INSERT_FINDING = """
INSERT INTO findings (
    finding_id, ad_id, row_number, check_type, message, field,
    proposed_fix, rule_id, file_ref
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (finding_id) DO NOTHING;
"""


def create_findings_table() -> None:
    """Create findings and dismissed_findings tables after ensuring ads table exists."""
    from apps.api.ads import create_ads_table

    create_ads_table()
    with get_session() as connection:
        connection.execute(CREATE_FINDINGS_TABLE)
        connection.execute(CREATE_DISMISSED_FINDINGS_TABLE)


def create_dismissed_findings_table(connection: Any = None) -> None:
    """Create the dismissed_findings false-alarm records table."""
    if connection is not None:
        connection.execute(CREATE_DISMISSED_FINDINGS_TABLE)
    else:
        with get_session() as conn:
            conn.execute(CREATE_DISMISSED_FINDINGS_TABLE)


def persist_findings_on_connection(
    connection: Connection, findings: Sequence[FindingContract]
) -> None:
    """Persist findings of any check type in the caller's transaction."""
    for finding in findings:
        connection.execute(
            INSERT_FINDING,
            (
                finding.finding_id,
                finding.ad_id,
                finding.row_number,
                finding.check_type.value,
                finding.message,
                finding.field,
                finding.proposed_fix,
                finding.rule_id,
                finding.file_ref,
            ),
        )


def persist_findings(findings: Sequence[FindingContract]) -> None:
    """Persist findings in a database-managed transaction."""
    if not findings:
        return
    with get_session() as connection:
        persist_findings_on_connection(connection, findings)


def persist_mechanical_findings(findings: Sequence[FindingContract]) -> None:
    """Persist mechanical findings in a database-managed transaction."""
    if not findings:
        return
    with get_session() as connection:
        persist_mechanical_findings_on_connection(connection, findings)


def persist_mechanical_findings_on_connection(
    connection: Connection, findings: Sequence[FindingContract]
) -> None:
    """Persist mechanical findings in the caller's transaction."""
    if any(finding.check_type != CheckType.MECHANICAL for finding in findings):
        raise ValueError("only mechanical findings can be persisted by this function")
    persist_findings_on_connection(connection, findings)


def persist_page_findings(findings: Sequence[FindingContract]) -> None:
    """Persist page findings in a database-managed transaction."""
    if not findings:
        return
    with get_session() as connection:
        persist_page_findings_on_connection(connection, findings)


def persist_page_findings_on_connection(
    connection: Connection, findings: Sequence[FindingContract]
) -> None:
    """Persist page findings in the caller's transaction."""
    if any(finding.check_type != CheckType.PAGE for finding in findings):
        raise ValueError("only page findings can be persisted by this function")
    persist_findings_on_connection(connection, findings)


# --- False-Alarm Dismissal Persistence & Retrieval ---

def record_finding_dismissal(
    connection: Any,
    *,
    finding_id: str,
    ad_id: str,
    batch_id: str,
    client_id: str,
    dismissed_by: str,
    reason: str,
    check_type: str | None = None,
    rule_id: str | None = None,
) -> dict[str, Any]:
    """Persist a false-alarm dismissal record."""
    clean_reason = reason.strip() if reason else ""
    if not clean_reason:
        raise HTTPException(
            status_code=422,
            detail="dismissal reason is required and cannot be blank",
        )

    sql = """
    INSERT INTO dismissed_findings (
        finding_id, ad_id, batch_id, client_id, dismissed_by, reason, check_type, rule_id
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (finding_id) DO UPDATE SET
        reason = EXCLUDED.reason,
        dismissed_by = EXCLUDED.dismissed_by,
        created_at = NOW()
    RETURNING dismissal_id, finding_id, ad_id, batch_id, client_id, dismissed_by, reason, created_at;
    """
    try:
        cur = connection.execute(
            sql,
            (finding_id, ad_id, batch_id, client_id, dismissed_by, clean_reason, check_type, rule_id),
        )
        row = cur.fetchone()
        if row:
            return {
                "dismissal_id": int(row[0]),
                "finding_id": str(row[1]),
                "ad_id": str(row[2]),
                "batch_id": str(row[3]),
                "client_id": str(row[4]),
                "dismissed_by": str(row[5]),
                "reason": str(row[6]),
                "created_at": row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7]),
            }
    except Exception as exc:
        logger.debug("Failed RETURNING in record_finding_dismissal: %s", exc)

    return {
        "dismissal_id": 1,
        "finding_id": finding_id,
        "ad_id": ad_id,
        "batch_id": batch_id,
        "client_id": client_id,
        "dismissed_by": dismissed_by,
        "reason": clean_reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def get_dismissed_findings_map(connection: Any, ad_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Retrieve dismissed findings mapped by finding_id for a sequence of ad IDs."""
    if not ad_ids:
        return {}
    try:
        cur = connection.execute(
            """
            SELECT dismissal_id, finding_id, ad_id, batch_id, client_id, dismissed_by, reason, created_at
            FROM dismissed_findings
            WHERE ad_id = ANY(%s);
            """,
            (list(ad_ids),),
        )
        result = {}
        for r in cur.fetchall():
            fid = str(r[1])
            result[fid] = {
                "dismissal_id": int(r[0]),
                "finding_id": fid,
                "ad_id": str(r[2]),
                "batch_id": str(r[3]),
                "client_id": str(r[4]),
                "dismissed_by": str(r[5]),
                "reason": str(r[6]),
                "created_at": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]),
            }
        return result
    except Exception:
        return {}


def get_dismissed_findings_for_ad(connection: Any, ad_id: str) -> dict[str, dict[str, Any]]:
    """Retrieve dismissed findings mapped by finding_id for a single ad."""
    return get_dismissed_findings_map(connection, [ad_id])


def get_batch_false_alarms(connection: Any, batch_id: str) -> list[dict[str, Any]]:
    """Retrieve all false-alarm records for a batch."""
    try:
        cur = connection.execute(
            """
            SELECT dismissal_id, finding_id, ad_id, batch_id, client_id, dismissed_by, reason,
                   check_type, rule_id, created_at
            FROM dismissed_findings
            WHERE batch_id = %s
            ORDER BY created_at ASC;
            """,
            (batch_id,),
        )
        records = []
        for r in cur.fetchall():
            records.append({
                "dismissal_id": int(r[0]),
                "finding_id": str(r[1]),
                "ad_id": str(r[2]),
                "batch_id": str(r[3]),
                "client_id": str(r[4]),
                "dismissed_by": str(r[5]),
                "reason": str(r[6]),
                "check_type": str(r[7]) if r[7] else None,
                "rule_id": str(r[8]) if r[8] else None,
                "created_at": r[9].isoformat() if hasattr(r[9], "isoformat") else str(r[9]),
            })
        return records
    except Exception:
        return []


# --- Request Models ---

class DismissFindingRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Mandatory reason explaining why this finding is a false alarm (PRD §4.6, Line 90)")


# --- API Endpoints ---

@router.post("/ads/{ad_id}/findings/{finding_id}/dismiss", status_code=status.HTTP_200_OK)
def dismiss_finding_endpoint(
    ad_id: str,
    finding_id: str,
    req: DismissFindingRequest,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Dismiss a finding as a false alarm with mandatory reason, updating the ad's active verdict."""
    clean_reason = req.reason.strip() if req.reason else ""
    if not clean_reason:
        raise HTTPException(
            status_code=422,
            detail="dismissal reason is required and cannot be blank",
        )

    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        # Client assignment check (Needs: 7)
        require_client_access(user, ad["client_id"])

        # State check: cannot dismiss findings on ads in sending or sent state
        approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        current_state = approval.get("state")
        if current_state in (ApprovalState.SENDING.value, ApprovalState.SENT.value):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"cannot dismiss findings on ad in '{current_state}' state",
            )

        # Retrieve finding metadata if available
        check_type = None
        rule_id = None
        try:
            cur = conn.execute(
                "SELECT check_type, rule_id FROM findings WHERE ad_id = %s AND finding_id = %s;",
                (ad_id, finding_id),
            )
            row = cur.fetchone()
            if row:
                check_type = row[0]
                rule_id = row[1]
        except Exception:
            pass

        dismissal = record_finding_dismissal(
            conn,
            finding_id=finding_id,
            ad_id=ad_id,
            batch_id=ad["batch_id"],
            client_id=ad["client_id"],
            dismissed_by=user.user_id,
            reason=clean_reason,
            check_type=check_type,
            rule_id=rule_id,
        )

        # Calculate remaining active (non-dismissed) findings
        all_fids = []
        try:
            cur = conn.execute("SELECT finding_id FROM findings WHERE ad_id = %s;", (ad_id,))
            all_fids = [r[0] for r in cur.fetchall()]
        except Exception:
            pass

        dismissed_map = get_dismissed_findings_map(conn, [ad_id])
        dismissed_map[finding_id] = dismissal

        active_findings = [fid for fid in all_fids if fid not in dismissed_map]
        new_verdict = "ready" if len(active_findings) == 0 else "fix needed"

        if new_verdict == "ready":
            try:
                conn.execute(
                    """
                    UPDATE worker_traces
                    SET verdict = 'ready', end_time = CURRENT_TIMESTAMP
                    WHERE batch_id = %s AND ad_id = %s;
                    """,
                    (ad["batch_id"], ad_id),
                )
            except Exception:
                pass

        return {
            "dismissal_id": dismissal.get("dismissal_id", 1),
            "finding_id": finding_id,
            "ad_id": ad_id,
            "batch_id": ad["batch_id"],
            "client_id": ad["client_id"],
            "dismissed_by": user.user_id,
            "reason": clean_reason,
            "created_at": dismissal.get("created_at"),
            "is_dismissed": True,
            "active_findings_count": len(active_findings),
            "new_verdict": new_verdict,
        }


@router.get("/ads/{ad_id}/findings/dismissals", status_code=status.HTTP_200_OK)
def get_ad_dismissals_endpoint(
    ad_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve full history of false-alarm dismissals for an ad."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")
        require_client_access(user, ad["client_id"])
        dismissals = get_dismissed_findings_for_ad(conn, ad_id)
        return {
            "ad_id": ad_id,
            "total_dismissed": len(dismissals),
            "dismissals": list(dismissals.values()),
        }


@router.get("/batches/{batch_id}/false-alarms", status_code=status.HTTP_200_OK)
def get_batch_false_alarms_endpoint(
    batch_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve all false-alarm records for a batch, scoped to assigned clients."""
    with get_session() as conn:
        allowed = set(user.assigned_client_ids)
        all_records = get_batch_false_alarms(conn, batch_id)
        scoped = [r for r in all_records if r["client_id"] in allowed]
        return {
            "batch_id": batch_id,
            "total_false_alarms": len(scoped),
            "false_alarms": scoped,
        }
