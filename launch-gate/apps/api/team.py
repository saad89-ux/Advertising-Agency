"""Team & Employee Account Management endpoints for Account Managers."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api.auth import hash_password, require_role
from apps.api.clients import list_clients, normalize_meta_account_id
from apps.api.database import get_session
from apps.api.auth import CurrentUser
from apps.api.users import (
    assign_user_to_client,
    create_user,
    get_user_by_email,
    get_user_client_ids,
    list_all_users,
)

router = APIRouter(prefix="/team", tags=["team"])


class CreateUserRequest(BaseModel):
    email: str = Field(..., description="Employee's business email address")
    display_name: str = Field(..., description="Full Name or Callout Name")
    password: str = Field(..., min_length=6, description="Password (at least 6 characters)")
    role: Literal["buyer", "account_manager"] = Field(
        default="buyer",
        description="Role: 'buyer' for Media Buyers, 'account_manager' for Account Managers",
    )
    assigned_client_ids: list[str] = Field(
        default_factory=list,
        description="List of client IDs this employee is authorized to manage",
    )


class MetaAccountMappingRequest(BaseModel):
    client_id: str
    meta_ad_account_id: str


class UserResponse(BaseModel):
    user_id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    assigned_client_ids: list[str]


@router.get("/clients", summary="List available clients for assignment")
def get_available_clients(
    current_user: Any = Depends(require_role("account_manager")),
) -> list[dict[str, str]]:
    """Return all system clients available for account assignments."""
    return list_clients()


@router.post("/clients/meta-account", summary="Configure a client's Meta ad account")
def configure_client_meta_account(
    req: MetaAccountMappingRequest,
    current_user: CurrentUser = Depends(require_role("account_manager")),
) -> dict[str, str]:
    """Set the Meta ad account mapped to an assigned internal client workspace."""
    if req.client_id not in set(current_user.assigned_client_ids):
        raise HTTPException(status_code=403, detail="Client is not assigned to this account manager.")
    try:
        normalized_account = normalize_meta_account_id(req.meta_ad_account_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with get_session() as connection:
        updated = connection.execute(
            "UPDATE clients SET meta_ad_account_id=%s WHERE client_id=%s RETURNING client_id",
            (normalized_account, req.client_id),
        ).fetchone()
        if updated:
            connection.execute(
                """INSERT INTO meta_workspace_connections
                   (workspace_id, ad_account_id, connection_type, status, updated_at)
                   VALUES (%s,%s,'AGENCY','active',NOW())
                   ON CONFLICT (workspace_id) DO UPDATE SET
                   ad_account_id=EXCLUDED.ad_account_id,
                   connection_type='AGENCY', status='active', updated_at=NOW()""",
                (req.client_id, normalized_account),
            )
            connection.execute(
                """INSERT INTO workspace_meta_connections
                   (workspace_id, meta_ad_account_id, token_type, connection_health, updated_at)
                   VALUES (%s,%s,'AGENCY_SYSTEM_USER','UNKNOWN',NOW())
                   ON CONFLICT (workspace_id) DO UPDATE SET
                   meta_ad_account_id=EXCLUDED.meta_ad_account_id,
                   token_type='AGENCY_SYSTEM_USER', connection_health='UNKNOWN',
                   verified_scopes=ARRAY[]::TEXT[], last_verified_at=NULL,
                   error_message=NULL, updated_at=NOW()""",
                (req.client_id, normalized_account),
            )
    if not updated:
        raise HTTPException(status_code=404, detail="Client workspace was not found.")
    return {"client_id": req.client_id, "meta_ad_account_id": normalized_account}


@router.get("/users", summary="List all team members and media buyers")
def get_team_users(
    current_user: Any = Depends(require_role("account_manager")),
) -> list[dict[str, object]]:
    """List all registered employee accounts and their assigned clients."""
    return list_all_users()


@router.post("/users", status_code=status.HTTP_201_CREATED, summary="Create new employee account")
def create_team_user(
    req: CreateUserRequest,
    current_user: Any = Depends(require_role("account_manager")),
) -> dict[str, object]:
    """Create a new Media Buyer or Account Manager account."""
    clean_email = req.email.strip().lower()
    if not clean_email or "@" not in clean_email or "." not in clean_email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A valid email address is required.",
        )

    clean_name = req.display_name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Display name cannot be empty.",
        )

    if len(req.password.strip()) < 6:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must be at least 6 characters long.",
        )

    # Check for existing email
    existing = get_user_by_email(clean_email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An account with email '{clean_email}' already exists.",
        )

    # Generate user_id: e.g. "sarah_buyer_a1b2c3"
    prefix = re.sub(r"[^a-zA-Z0-9_]", "_", clean_email.split("@")[0])[:20].lower()
    user_id = f"{prefix}_{uuid.uuid4().hex[:6]}"

    # Hash password securely
    pwd_hash = hash_password(req.password.strip())

    # Persist user
    create_user(
        user_id=user_id,
        email=clean_email,
        display_name=clean_name,
        password_hash=pwd_hash,
        role=req.role,
        is_active=True,
    )

    # Assign clients
    assigned = req.assigned_client_ids or []
    # If no clients specified and role is buyer, default to all registered clients from DB
    if not assigned and req.role == "buyer":
        assigned = [c["client_id"] for c in list_clients()]

    for cid in assigned:
        assign_user_to_client(user_id=user_id, client_id=cid)

    return {
        "user_id": user_id,
        "email": clean_email,
        "display_name": clean_name,
        "role": req.role,
        "is_active": True,
        "assigned_client_ids": assigned,
        "message": f"Successfully created {req.role} account for {clean_name} ({clean_email}). Ready for login.",
    }
