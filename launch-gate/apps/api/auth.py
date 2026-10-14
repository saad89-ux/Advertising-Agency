"""Local password login and signed bearer-token authentication."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import os
import re
import time

import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apps.api import users


router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    email: str
    display_name: str
    role: str
    assigned_client_ids: tuple[str, ...]


def get_jwt_secret() -> str:
    load_dotenv()
    secret = os.environ.get("JWT_SECRET", "").strip()
    placeholder = not secret or bool(
        re.search(r"placeholder|replace[-_ ]?me|change[-_ ]?me|your[-_ ]?secret", secret, re.I)
    )
    if placeholder:
        raise RuntimeError("JWT_SECRET must be set to a non-placeholder secret")
    return secret


def get_jwt_expire_minutes() -> int:
    load_dotenv()
    raw = os.environ.get("JWT_EXPIRE_MINUTES", "60").strip()
    try:
        minutes = int(raw)
    except ValueError as exc:
        raise RuntimeError("JWT_EXPIRE_MINUTES must be a positive integer") from exc
    if minutes <= 0:
        raise RuntimeError("JWT_EXPIRE_MINUTES must be a positive integer")
    return minutes


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(derived).decode("ascii"),
        )
    )


def _verify_scrypt_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, expected_text = encoded_hash.split("$")
        if algorithm != "scrypt":
            return False
        if (int(n), int(r), int(p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
        if len(salt) != 16 or len(expected) != _SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError):
        return False


def verify_password(password: str, encoded_hash: str) -> bool:
    if not isinstance(password, str) or not isinstance(encoded_hash, str):
        return False
    return _verify_scrypt_password(password, encoded_hash)


def create_access_token(user_id: str, role: str) -> str:
    if role not in users.VALID_ROLES or not user_id:
        raise ValueError("a valid user_id and role are required")
    now = int(time.time())
    payload = {
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + get_jwt_expire_minutes() * 60,
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm="HS256")


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate_token(token_str: str) -> CurrentUser:
    try:
        payload = jwt.decode(
            token_str,
            get_jwt_secret(),
            algorithms=["HS256"],
            options={"require": ["sub", "role", "iat", "exp"]},
        )
    except (jwt.InvalidTokenError, RuntimeError):
        raise unauthorized() from None
    if set(payload) != {"sub", "role", "iat", "exp"}:
        raise unauthorized()
    if not isinstance(payload["sub"], str) or payload["role"] not in users.VALID_ROLES:
        raise unauthorized()

    record = users.get_user_by_id(payload["sub"])
    if (
        record is None
        or not record["is_active"]
        or record["role"] != payload["role"]
    ):
        raise unauthorized()
    client_ids = users.get_user_client_ids(payload["sub"])
    return CurrentUser(
        user_id=str(record["user_id"]),
        email=str(record["email"]),
        display_name=str(record["display_name"]),
        role=str(record["role"]),
        assigned_client_ids=tuple(client_ids),
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> CurrentUser:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise unauthorized()
    return authenticate_token(credentials.credentials)


def get_current_user_for_sse(
    token: str | None = Query(None),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> CurrentUser:
    if credentials is not None and credentials.scheme.casefold() == "bearer":
        return authenticate_token(credentials.credentials)
    if token:
        return authenticate_token(token)
    raise unauthorized()


def require_role(*roles: str):
    allowed = frozenset(roles)

    def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return dependency


@router.post("/auth/login")
async def login(request: Request) -> dict[str, str]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=401, detail="invalid credentials") from None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("email"), str)
        or not isinstance(payload.get("password"), str)
    ):
        raise HTTPException(status_code=401, detail="invalid credentials")
    email = payload["email"].strip()
    password = payload["password"]
    record = users.get_user_by_email(email)
    valid = (
        record is not None
        and bool(record["is_active"])
        and verify_password(password, str(record["password_hash"]))
    )
    if not valid:
        raise HTTPException(status_code=401, detail="invalid credentials")
    try:
        token = create_access_token(str(record["user_id"]), str(record["role"]))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="authentication is not configured") from exc
    return {"access_token": token, "token_type": "bearer"}
