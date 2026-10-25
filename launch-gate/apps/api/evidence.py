"""Client approval evidence persistence and validation.

PRD §4.6, D3, Line 90 & Dataflow Architecture:
- Client approval given by email, text, or call is recorded by the reviewer attaching a
  note or screenshot linked to the ad's content hash [D3].
- Reviewers (buyers or account managers, e.g. Tobias) attach evidence (note, screenshot image, PDF).
- File storage uses LocalFileStore (Needs: 21) with opaque, root-confined references.
- Evidence is validated: non-empty note or valid file, supported image/PDF type, max 10MB.
- Linked to content hash (Needs: 40): verified against ad's canonical content hash (409 on mismatch).
- Scoping enforced (Needs: 7): reviewer must be assigned to client (403).
- Terminal state protection: cannot attach evidence to ads in 'sending' or 'sent' state (409).
"""

from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from apps.api.access import require_client_access
from apps.api.approvals import (
    ApprovalState,
    get_ad_record,
    get_or_create_ad_approval,
)
from apps.api.auth import CurrentUser, get_current_user, require_role
from apps.api.database import get_session
from apps.worker.file_storage import LocalFileStore, get_file_store, is_valid_file_ref

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ads", tags=["evidence"])

CREATE_APPROVAL_EVIDENCE_TABLE = """
CREATE TABLE IF NOT EXISTS approval_evidence (
    evidence_id BIGSERIAL PRIMARY KEY,
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id),
    content_hash TEXT NOT NULL,
    evidence_note TEXT,
    evidence_file TEXT,
    file_name TEXT,
    file_size INTEGER,
    mime_type TEXT,
    created_by TEXT NOT NULL REFERENCES users (user_id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_approval_evidence_ad ON approval_evidence (ad_id);
"""

ALLOWED_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".pdf",
}

ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "application/pdf",
}

MAX_EVIDENCE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


def create_approval_evidence_table(connection: Any = None) -> None:
    """Create the approval_evidence table."""
    from apps.api.ads import create_ads_table

    create_ads_table()
    if connection is not None:
        connection.execute(CREATE_APPROVAL_EVIDENCE_TABLE)
    else:
        with get_session() as conn:
            conn.execute(CREATE_APPROVAL_EVIDENCE_TABLE)


def save_evidence_file(file_bytes: bytes, file_name: str | None = None) -> str:
    """Persist evidence bytes via UnifiedFileStore (Cloudinary if configured, otherwise LocalFileStore)."""
    store = get_file_store()
    return store.put(file_bytes, filename=file_name)


def record_approval_evidence(
    connection: Any,
    *,
    ad_id: str,
    client_id: str,
    content_hash: str,
    created_by: str,
    evidence_note: str | None = None,
    evidence_file: str | None = None,
    file_name: str | None = None,
    file_size: int | None = None,
    mime_type: str | None = None,
    sync_approvals_table: bool = True,
) -> dict[str, Any]:
    """Persist evidence linked to content hash and update ad_approvals record."""
    clean_note = evidence_note.strip() if evidence_note else None
    clean_file = evidence_file.strip() if evidence_file else None

    if not clean_note and not clean_file:
        raise HTTPException(
            status_code=422,
            detail="at least a note or screenshot file must be provided as approval evidence (PRD D3)",
        )

    now = datetime.now(timezone.utc)
    ev_id = 1
    created_at_val = now

    try:
        cur = connection.execute(
            """
            INSERT INTO approval_evidence (
                ad_id, client_id, content_hash, evidence_note, evidence_file,
                file_name, file_size, mime_type, created_by, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING evidence_id, created_at;
            """,
            (
                ad_id,
                client_id,
                content_hash,
                clean_note,
                clean_file,
                file_name,
                file_size,
                mime_type,
                created_by,
                now,
            ),
        )
        row = cur.fetchone()
        if row:
            ev_id = int(row[0])
            created_at_val = row[1]
    except Exception as exc:
        logger.debug("Skipping RETURNING in record_approval_evidence: %s", exc)

    # Sync ad_approvals if requested (when not already synced by transition_ad_state)
    if sync_approvals_table:
        try:
            connection.execute(
                """
                UPDATE ad_approvals
                SET evidence_note = COALESCE(%s, evidence_note),
                    evidence_file = COALESCE(%s, evidence_file),
                    updated_at = %s
                WHERE ad_id = %s;
                """,
                (clean_note, clean_file, now, ad_id),
            )
        except Exception as exc:
            logger.debug("Failed updating ad_approvals with evidence: %s", exc)

    return {
        "evidence_id": ev_id,
        "ad_id": ad_id,
        "client_id": client_id,
        "content_hash": content_hash,
        "evidence_note": clean_note,
        "evidence_file": clean_file,
        "file_name": file_name,
        "file_size": file_size,
        "mime_type": mime_type,
        "created_by": created_by,
        "created_at": (
            created_at_val.isoformat()
            if hasattr(created_at_val, "isoformat")
            else str(created_at_val)
        ),
    }


def get_ad_evidence_history(connection: Any, ad_id: str) -> list[dict[str, Any]]:
    """Retrieve all evidence entries for an ad."""
    try:
        cur = connection.execute(
            """
            SELECT evidence_id, ad_id, client_id, content_hash, evidence_note,
                   evidence_file, file_name, file_size, mime_type, created_by, created_at
            FROM approval_evidence
            WHERE ad_id = %s
            ORDER BY created_at DESC, evidence_id DESC;
            """,
            (ad_id,),
        )
        records = []
        for r in cur.fetchall():
            records.append({
                "evidence_id": int(r[0]),
                "ad_id": str(r[1]),
                "client_id": str(r[2]),
                "content_hash": str(r[3]),
                "evidence_note": r[4],
                "evidence_file": r[5],
                "file_name": r[6],
                "file_size": r[7],
                "mime_type": r[8],
                "created_by": str(r[9]),
                "created_at": r[10].isoformat() if hasattr(r[10], "isoformat") else str(r[10]),
            })
        return records
    except Exception:
        return []


# --- Request Models ---

class AttachEvidenceRequest(BaseModel):
    note: str | None = Field(default=None, description="Reviewer note of client approval (PRD D3)")
    evidence_note: str | None = Field(default=None, description="Alias for note")
    file_ref: str | None = Field(default=None, description="Opaque 32-hex file storage reference (Needs: 21)")
    evidence_file: str | None = Field(default=None, description="Alias for file_ref")
    file_name: str | None = Field(default=None, description="Original filename of evidence file")
    file_size: int | None = Field(default=None, description="File size in bytes")
    mime_type: str | None = Field(default=None, description="MIME type of evidence file")
    content_hash: str | None = Field(default=None, description="Expected content hash to link evidence to (Needs: 40)")


# --- API Endpoints ---

@router.post("/{ad_id}/evidence", status_code=status.HTTP_200_OK)
def attach_evidence_endpoint(
    ad_id: str,
    req: AttachEvidenceRequest,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Attach client approval note or screenshot file reference, validated and linked to content hash."""
    clean_note = (req.note or req.evidence_note or "").strip() or None
    clean_file = (req.file_ref or req.evidence_file or "").strip() or None

    if clean_file and not is_valid_file_ref(clean_file):
        raise HTTPException(status_code=422, detail="invalid evidence file reference")

    if not clean_note and not clean_file:
        raise HTTPException(
            status_code=422,
            detail="at least a note or screenshot file must be provided as approval evidence (PRD D3)",
        )

    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        # Client assignment check (Needs: 7)
        require_client_access(user, ad["client_id"])

        # Terminal state check
        approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        current_state = approval.get("state")
        if current_state in (ApprovalState.SENDING.value, ApprovalState.SENT.value):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"cannot attach evidence to ad in '{current_state}' state",
            )

        # Content hash linkage (Needs: 40)
        expected_hash = ad["row_hash"]
        if req.content_hash and req.content_hash != expected_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"content hash mismatch: expected '{expected_hash}', received '{req.content_hash}'",
            )
        target_hash = expected_hash

        rec = record_approval_evidence(
            conn,
            ad_id=ad_id,
            client_id=ad["client_id"],
            content_hash=target_hash,
            created_by=user.user_id,
            evidence_note=clean_note,
            evidence_file=clean_file,
            file_name=req.file_name,
            file_size=req.file_size,
            mime_type=req.mime_type,
        )

        return {
            **rec,
            "content_hash_matches": True,
            "ad_state": current_state,
        }


@router.post("/{ad_id}/evidence/upload", status_code=status.HTTP_200_OK)
async def upload_evidence_endpoint(
    ad_id: str,
    file: UploadFile | None = File(default=None),
    note: str | None = Form(default=None),
    evidence_note: str | None = Form(default=None),
    content_hash: str | None = Form(default=None),
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Upload a screenshot image or PDF document and attach it as client approval evidence."""
    clean_note = (note or evidence_note or "").strip() or None
    if not clean_note and (file is None or not file.filename):
        raise HTTPException(
            status_code=422,
            detail="at least a note or screenshot file must be provided as approval evidence (PRD D3)",
        )

    file_ref = None
    file_name = None
    file_size = None
    mime_type = None
    try:
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
                    detail=f"cannot attach evidence to ad in '{current_state}' state",
                )

            expected_hash = ad["row_hash"]
            if content_hash and content_hash != expected_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"content hash mismatch: expected '{expected_hash}', received '{content_hash}'",
                )

            if file is not None and file.filename:
                file_name = file.filename
                ext = Path(file_name).suffix.lower()
                if ext not in ALLOWED_EXTENSIONS:
                    raise HTTPException(
                        status_code=422,
                        detail=f"unsupported file extension '{ext}'; only images (PNG, JPEG, WEBP) and PDF files are allowed",
                    )

                data = await file.read(MAX_EVIDENCE_SIZE_BYTES + 1)
                await file.close()
                if len(data) == 0:
                    raise HTTPException(status_code=422, detail="uploaded evidence file is empty")
                if len(data) > MAX_EVIDENCE_SIZE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"evidence file exceeds maximum size of {MAX_EVIDENCE_SIZE_BYTES // (1024 * 1024)} MB",
                    )

                file_size = len(data)
                mime_type = file.content_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
                file_ref = save_evidence_file(data, file_name)

            rec = record_approval_evidence(
                conn,
                ad_id=ad_id,
                client_id=ad["client_id"],
                content_hash=expected_hash,
                created_by=user.user_id,
                evidence_note=clean_note,
                evidence_file=file_ref,
                file_name=file_name,
                file_size=file_size,
                mime_type=mime_type,
            )
    except HTTPException:
        if file_ref:
            try:
                get_file_store().delete(file_ref)
            except Exception:
                logger.exception("Failed cleaning up evidence file after request rejection")
        raise
    except Exception as exc:
        if file_ref:
            try:
                get_file_store().delete(file_ref)
            except Exception:
                logger.exception("Failed cleaning up evidence file after persistence failure")
        logger.error("Failed storing approval evidence: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed storing approval evidence",
        ) from exc

    return {
        **rec,
        "content_hash_matches": True,
        "ad_state": current_state,
    }


@router.get("/{ad_id}/evidence", status_code=status.HTTP_200_OK)
def get_ad_evidence_endpoint(
    ad_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieve full client approval evidence history for an ad."""
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])

        history = get_ad_evidence_history(conn, ad_id)
        approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])

        current_hash = ad["row_hash"]
        annotated_history = []
        for h in history:
            annotated_history.append({
                **h,
                "content_hash_matches": (h["content_hash"] == current_hash),
            })

        latest = annotated_history[0] if annotated_history else None
        if not latest and (approval.get("evidence_note") or approval.get("evidence_file")):
            latest = {
                "evidence_id": 0,
                "ad_id": ad_id,
                "client_id": ad["client_id"],
                "content_hash": approval.get("content_hash") or current_hash,
                "content_hash_matches": (approval.get("content_hash") == current_hash),
                "evidence_note": approval.get("evidence_note"),
                "evidence_file": approval.get("evidence_file"),
                "file_name": None,
                "file_size": None,
                "mime_type": None,
                "created_by": approval.get("approved_by") or "unknown",
                "created_at": approval.get("updated_at"),
            }

        return {
            "ad_id": ad_id,
            "client_id": ad["client_id"],
            "current_content_hash": current_hash,
            "has_evidence": bool(latest),
            "latest": latest,
            "history": annotated_history,
        }


@router.get("/{ad_id}/evidence/file", status_code=status.HTTP_200_OK)
def get_ad_evidence_file_endpoint(
    ad_id: str,
    file_ref: str | None = None,
    redirect: bool = False,
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    """Download or view stored evidence file, protected by client scoping.
    
    Handles both external HTTPS URLs (Cloudinary CDN, via streaming or redirect)
    and local relative paths safely.
    """
    with get_session() as conn:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ad not found")

        require_client_access(user, ad["client_id"])

        approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        attached_ref = approval.get("evidence_file")
        target_ref = file_ref or attached_ref

        if not target_ref:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no evidence file attached")
        if not is_valid_file_ref(target_ref):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid evidence file reference")

        evidence_row = conn.execute(
            "SELECT mime_type FROM approval_evidence WHERE ad_id = %s AND evidence_file = %s LIMIT 1;",
            (ad_id, target_ref),
        ).fetchone()
        if target_ref != attached_ref and evidence_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="evidence file not found for this ad")
        mime_type = evidence_row[0] if evidence_row else None

        # If caller requested redirect for external HTTPS URLs
        if target_ref.startswith("https://") and redirect:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=target_ref, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

        try:
            store = get_file_store()
            file_bytes = store.get(target_ref)
        except Exception as exc:
            logger.debug("Failed retrieving evidence file %s: %s", target_ref, exc)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="evidence file not found in storage")

        media_type = mime_type or mimetypes.guess_type(target_ref)[0] or "application/octet-stream"

        return Response(content=file_bytes, media_type=media_type)
