"""Client access policy shared by API endpoints."""

from fastapi import HTTPException

from apps.api.auth import CurrentUser


def user_can_access_client(user: CurrentUser, client_id: str) -> bool:
    normalized_client_id = (client_id or "").strip()
    if not normalized_client_id:
        return False
    if user.role in {"admin", "owner"}:
        return True
    return normalized_client_id in set(user.assigned_client_ids)


def require_client_access(user: CurrentUser, client_id: str) -> None:
    if not user_can_access_client(user, client_id):
        raise HTTPException(status_code=403, detail="forbidden")
