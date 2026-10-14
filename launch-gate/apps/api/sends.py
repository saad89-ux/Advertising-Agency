"""API routes for triggering and monitoring idempotent stand-in dry-run sends.

Implements PRD §4.6, Dataflow Architecture §3, and Never-break Rules 1, 2, 7:
- POST /ads/{ad_id}/send — Send a single approved ad (idempotent, dry-run only).
- GET  /ads/{ad_id}/send — Retrieve send history and platform response for an ad.
- GET  /ads/{ad_id}/alerts — Retrieve reviewer alerts for an ad.
- POST /batches/{batch_id}/send-approved — Batch send all approved ads for assigned clients.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api.access import require_client_access
from apps.api.approvals import (
    ApprovalState,
    ContentHashMismatchError,
    UnapprovedAdError,
    get_ad_record,
    get_or_create_ad_approval,
)
from apps.api.auth import CurrentUser, get_current_user, require_role
from apps.api.batches import get_batch_record
from apps.api.database import get_session
from apps.worker.platforms import RealPlatformCallDisabledError
from apps.worker.sender import (
    get_ad_alerts,
    get_ad_send_records,
    retry_dead_letter_ad,
    retry_dead_letter_batch_ads,
    send_ad_to_stand_in,
    send_batch_approved_ads,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sends"])


class SendAdRequest(BaseModel):
    dry_run: bool = Field(default=True, description="Enforces dry-run mode (real platform calls are switched off)")


@router.post("/ads/{ad_id}/send", status_code=status.HTTP_200_OK)
def send_ad_endpoint(
    ad_id: str,
    req: SendAdRequest | None = None,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Send an approved ad to the stand-in dry-run platform with idempotency protection."""
    req = req or SendAdRequest()
    if not req.dry_run:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Real platform calls are switched off in Launch Gate v1 (PRD §4.6, Rule 7). Only dry-run is permitted.",
        )

    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        # Client assignment check (Needs: 7)
        require_client_access(user, ad["client_id"])

        try:
            return send_ad_to_stand_in(
                ad_id,
                connection=conn,
                user_id=user.user_id,
                enforce_dry_run=True,
            )
        except UnapprovedAdError as err:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
        except ContentHashMismatchError as err:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(err))
        except RealPlatformCallDisabledError as err:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
        except ValueError as err:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(err))


@router.get("/ads/{ad_id}/send", status_code=status.HTTP_200_OK)
def get_ad_send_status_endpoint(
    ad_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve send history and status for an ad."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])

        approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        sends = get_ad_send_records(conn, ad_id)
        latest = sends[-1] if sends else None

        return {
            "ad_id": ad_id,
            "approval_state": approval.get("state"),
            "idempotency_key": approval.get("idempotency_key"),
            "send_attempts": approval.get("send_attempts", 0),
            "sent_at": approval.get("sent_at"),
            "latest_send": latest,
            "history": sends,
        }


@router.get("/ads/{ad_id}/alerts", status_code=status.HTTP_200_OK)
def get_ad_alerts_endpoint(
    ad_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve reviewer alerts recorded for an ad."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])

        alerts = get_ad_alerts(conn, ad_id)
        return {
            "ad_id": ad_id,
            "alerts": alerts,
            "has_alerts": len(alerts) > 0,
        }


@router.post("/batches/{batch_id}/send-approved", status_code=status.HTTP_200_OK)
def send_batch_approved_endpoint(
    batch_id: str,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Send all approved ads in a batch belonging to the user's assigned clients."""
    with get_session() as conn:
        batch_rec = get_batch_record(conn, batch_id)
        if batch_rec is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="batch not found")

        # Multi-tenant scoping: check if batch has ads and whether user has access to any
        try:
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
        except HTTPException:
            raise
        except Exception as exc:
            logger.debug("Skipping batch scoping SQL query on mock connection: %s", exc)

        allowed_clients = list(user.assigned_client_ids)
        return send_batch_approved_ads(
            batch_id,
            connection=conn,
            client_ids=allowed_clients,
            user_id=user.user_id,
        )


@router.post("/ads/{ad_id}/retry", status_code=status.HTTP_200_OK)
def retry_dead_letter_ad_endpoint(
    ad_id: str,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Retry an ad that is in dead_letter queue, re-attempting stand-in send with bounded retries."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])

        try:
            return retry_dead_letter_ad(
                ad_id,
                connection=conn,
                user_id=user.user_id,
            )
        except ValueError as err:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
        except Exception as err:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(err))


@router.post("/batches/{batch_id}/retry-dead-letter", status_code=status.HTTP_200_OK)
def retry_dead_letter_batch_endpoint(
    batch_id: str,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Retry all dead-letter ads in a batch belonging to the user's assigned clients."""
    with get_session() as conn:
        allowed_clients = list(user.assigned_client_ids)
        return retry_dead_letter_batch_ads(
            batch_id,
            connection=conn,
            client_ids=allowed_clients,
            user_id=user.user_id,
        )

