"""Opaque, revocable guest sessions for anonymous campaign review."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from apps.api import auth as auth_module
from apps.api.auth import CurrentUser, bearer_scheme, unauthorized
from apps.api.database import get_session
from fastapi.security import HTTPAuthorizationCredentials
from apps.api.client_ip import get_client_ip

router = APIRouter(tags=["guest sessions"])
COOKIE_NAME = "launch_gate_guest"


def guest_ttl_days() -> int:
    raw = os.getenv("GUEST_SESSION_TTL_DAYS", "30")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("GUEST_SESSION_TTL_DAYS must be a positive integer") from exc
    if not 1 <= value <= 365:
        raise RuntimeError("GUEST_SESSION_TTL_DAYS must be between 1 and 365")
    return value


def cookie_secure() -> bool:
    return os.getenv("APP_ENV", "development").casefold() in {"production", "prod"}


def cookie_samesite() -> str:
    # Secure is mandatory for None; use it for separated production frontend/API origins.
    return "none" if cookie_secure() else "lax"


def init_guest_tables() -> None:
    with get_session() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS guest_sessions (
            session_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            client_id TEXT NOT NULL UNIQUE REFERENCES clients(client_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            created_ip TEXT
        );""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_guest_sessions_expiry ON guest_sessions(expires_at);")
        conn.execute("""CREATE TABLE IF NOT EXISTS guest_session_creation_limits (
            ip_hash TEXT NOT NULL,
            window_hour TIMESTAMPTZ NOT NULL,
            attempts INTEGER NOT NULL,
            PRIMARY KEY (ip_hash, window_hour)
        );""")


def init_batch_guest_ownership() -> None:
    with get_session() as conn:
        conn.execute("ALTER TABLE batches ADD COLUMN IF NOT EXISTS guest_session_id TEXT;")
        conn.execute("ALTER TABLE batches ALTER COLUMN uploaded_by DROP NOT NULL;")
        conn.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='batches_guest_session_id_fkey' AND conrelid='batches'::regclass) THEN
                ALTER TABLE batches ADD CONSTRAINT batches_guest_session_id_fkey
                    FOREIGN KEY (guest_session_id) REFERENCES guest_sessions(session_id);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='batches_owner_xor_check' AND conrelid='batches'::regclass) THEN
                ALTER TABLE batches ADD CONSTRAINT batches_owner_xor_check
                    CHECK ((uploaded_by IS NULL) <> (guest_session_id IS NULL));
            END IF;
        END $$;""")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", errors="strict")).hexdigest()


def _valid_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    allowed = {x.strip().rstrip("/") for x in os.getenv("FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if x.strip()}
    return origin is not None and origin.rstrip("/") in allowed


def require_guest_csrf(request: Request) -> None:
    if not _valid_origin(request):
        raise HTTPException(status_code=403, detail="request origin is not allowed")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"", "application/json", "multipart/form-data"}:
            raise HTTPException(status_code=403, detail="request content type is not allowed")


def get_guest_user(request: Request) -> CurrentUser:
    token = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
        raise unauthorized()
    try:
        token_hash = _token_hash(token)
    except (UnicodeError, ValueError):
        raise unauthorized() from None
    with get_session() as conn:
        row = conn.execute("""SELECT session_id, client_id FROM guest_sessions
            WHERE token_hash=%s AND revoked_at IS NULL AND expires_at > NOW()""", (token_hash,)).fetchone()
    if row is None:
        raise unauthorized()
    return CurrentUser(user_id=f"guest:{row[0]}", email="", display_name="Guest", role="guest",
                       assigned_client_ids=(str(row[1]),), guest_session_id=str(row[0]))


def get_request_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> CurrentUser:
    # An explicit Authorization header always takes precedence; invalid JWTs cannot fall back to cookies.
    if request.headers.get("authorization"):
        if credentials is None or credentials.scheme.casefold() != "bearer" or not credentials.credentials:
            raise unauthorized()
        return auth_module.authenticate_token(credentials.credentials)
    if request.cookies.get(COOKIE_NAME):
        return get_guest_user(request)
    raise unauthorized()


@router.post("/auth/guest-session")
def create_guest_session(request: Request, response: Response):
    require_guest_csrf(request)
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            get_guest_user(request)
            return {"status": "active", "guest": True}
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=cookie_secure(), samesite=cookie_samesite())
            response.status_code = 401
            return {"detail": "guest session expired or invalid"}
    token = secrets.token_urlsafe(32)
    sid = str(uuid4())
    client_id = f"guest_{uuid4().hex}"
    expires = datetime.now(timezone.utc) + timedelta(days=guest_ttl_days())
    ip = get_client_ip(request)
    init_guest_tables()
    with get_session() as conn:
        if ip:
            rate_key = hashlib.sha256(ip.encode("utf-8")).hexdigest()
            rate_result = conn.execute("""INSERT INTO guest_session_creation_limits(ip_hash,window_hour,attempts)
                VALUES (%s,date_trunc('hour',NOW()),1)
                ON CONFLICT (ip_hash,window_hour) DO UPDATE SET attempts=guest_session_creation_limits.attempts+1
                RETURNING attempts""", (rate_key,))
            rate_row = rate_result.fetchone() if rate_result is not None else None
            if rate_row is not None and int(rate_row[0]) > 20:
                raise HTTPException(status_code=429, detail="too many guest sessions from this network; try again later")
            conn.execute("DELETE FROM guest_session_creation_limits WHERE window_hour < date_trunc('hour',NOW()) - INTERVAL '24 hours'")
        conn.execute("INSERT INTO clients(client_id,name) VALUES (%s,%s)", (client_id, "Anonymous campaign"))
        conn.execute("""INSERT INTO guest_sessions(session_id,token_hash,client_id,expires_at,created_ip)
            VALUES (%s,%s,%s,%s,%s)""", (sid, _token_hash(token), client_id, expires, ip))
    response.set_cookie(COOKIE_NAME, token, max_age=guest_ttl_days()*86400, expires=expires,
                        httponly=True, secure=cookie_secure(), samesite=cookie_samesite(), path="/")
    return {"status": "created", "guest": True}


@router.delete("/auth/guest-session")
def revoke_guest_session(request: Request, response: Response):
    require_guest_csrf(request)
    token = request.cookies.get(COOKIE_NAME)
    if token and re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
        with get_session() as conn:
            conn.execute("UPDATE guest_sessions SET revoked_at=NOW() WHERE token_hash=%s AND revoked_at IS NULL", (_token_hash(token),))
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=cookie_secure(), samesite=cookie_samesite())
    return {"status": "revoked"}
