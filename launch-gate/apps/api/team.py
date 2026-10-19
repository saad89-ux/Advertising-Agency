"""Team & Employee Account Management endpoints for Account Managers."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api.auth import hash_password, require_role
from apps.api.clients import list_clients
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
