from datetime import datetime, timezone

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api import auth, main, users
from apps.api.access import require_client_access, user_can_access_client
from apps.api.auth import CurrentUser
from apps.api.main import app


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")
    monkeypatch.setenv("AUTH_RATE_LIMIT_PER_HOUR", "20")
    with auth._AUTH_ATTEMPTS_LOCK:
        auth._AUTH_ATTEMPTS.clear()


def test_login_returns_signed_token_but_never_password(monkeypatch, caplog):
    password = "synthetic-test-password"
    record = {
        "user_id": "buyer-1",
        "email": "buyer@example.test",
        "display_name": "Synthetic Buyer",
        "password_hash": auth.hash_password(password),
        "role": "buyer",
        "is_active": True,
    }
    monkeypatch.setattr(auth.users, "get_user_by_email", lambda email: record)

    with TestClient(app) as client:
        response = client.post(
            "/auth/login", json={"email": record["email"], "password": password}
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type"}
    assert body["token_type"] == "bearer"
    claims = jwt.decode(
        body["access_token"],
        "test-secret-only-not-for-production",
        algorithms=["HS256"],
    )
    assert set(claims) == {"sub", "role", "iat", "exp"}
    assert claims["sub"] == "buyer-1"
    assert claims["role"] == "buyer"
    assert password not in response.text
    assert password not in caplog.text
@pytest.mark.parametrize("case", ["wrong_password", "unknown_user", "inactive_user"])
def test_login_errors_are_generic(monkeypatch, case):
    password = "synthetic-test-password"
    record = {
        "user_id": "buyer-1",
        "email": "buyer@example.test",
        "display_name": "Synthetic Buyer",
        "password_hash": auth.hash_password(password),
        "role": "buyer",
        "is_active": case != "inactive_user",
    }
    monkeypatch.setattr(
        auth.users,
        "get_user_by_email",
        lambda email: None if case == "unknown_user" else record,
    )
    submitted_password = "wrong-password" if case == "wrong_password" else password

    with TestClient(app) as client:
        response = client.post(
            "/auth/login",
            json={"email": record["email"], "password": submitted_password},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}
    assert submitted_password not in response.text


def test_register_then_login_uses_same_credentials_and_creates_buyer_workspace(monkeypatch):
    password = "long-test-password-123"
    saved = {}

    def register_buyer(**values):
        saved.update(values)
        return True

    def get_user_by_email(email):
        return {
            "user_id": saved["user_id"],
            "email": email,
            "display_name": saved["display_name"],
            "password_hash": saved["password_hash"],
            "role": "buyer",
            "is_active": True,
        }

    monkeypatch.setattr(auth.users, "register_buyer", register_buyer)
    monkeypatch.setattr(auth.users, "get_user_by_email", get_user_by_email)

    with TestClient(app) as client:
        registration = client.post(
            "/auth/register",
            json={
                "email": "New.User@Example.test",
                "display_name": "New User",
                "password": password,
                "role": "account_manager",
            },
        )
        login = client.post(
            "/auth/login",
            json={"email": "new.user@example.test", "password": password},
        )

    assert registration.status_code == 201
    assert registration.json() == {"email": "new.user@example.test", "display_name": "New User"}
    assert login.status_code == 200
    claims = jwt.decode(
        login.json()["access_token"],
        "test-secret-only-not-for-production",
        algorithms=["HS256"],
    )
    assert claims["sub"] == saved["user_id"]
    assert saved["client_id"] == f"usr_{saved['user_id']}"
    assert saved["password_hash"] != password
    assert "role" not in saved


def test_register_rejects_invalid_email_weak_password_and_duplicate(monkeypatch):
    monkeypatch.setattr(auth.users, "register_buyer", lambda **values: False)
    with TestClient(app) as client:
        invalid_email = client.post(
            "/auth/register",
            json={"email": "invalid", "display_name": "Buyer", "password": "long-password-123"},
        )
        weak_password = client.post(
            "/auth/register",
            json={"email": "buyer@example.test", "display_name": "Buyer", "password": "weak"},
        )
        duplicate = client.post(
            "/auth/register",
            json={"email": "buyer@example.test", "display_name": "Buyer", "password": "long-password-123"},
        )

    assert invalid_email.status_code == 422
    assert weak_password.status_code == 422
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Unable to create an account with these details."}


def test_auth_routes_rate_limit_attempts_by_client_ip(monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_PER_HOUR", "1")
    with TestClient(app) as client:
        first = client.post("/auth/register", json={})
        second = client.post("/auth/login", json={})

    assert first.status_code == 422
    assert second.status_code == 429


@pytest.mark.parametrize("token_kind", ["missing", "expired", "tampered"])
def test_upload_auth_rejects_missing_expired_and_tampered_tokens(monkeypatch, token_kind):
    monkeypatch.setattr(auth.users, "get_user_by_id", lambda user_id: None)
    headers = {}
    if token_kind == "expired":
        now = int(datetime.now(timezone.utc).timestamp())
        token = jwt.encode(
            {"sub": "buyer-1", "role": "buyer", "iat": now - 120, "exp": now - 60},
            "test-secret-only-not-for-production",
            algorithm="HS256",
        )
        headers["Authorization"] = f"Bearer {token}"
    elif token_kind == "tampered":
        token = auth.create_access_token("buyer-1", "buyer") + "x"
        headers["Authorization"] = f"Bearer {token}"

    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files={"file": ("ads.csv", b"Row,Platform,Headline,URL\n1,Meta,Ad,/page", "text/csv")},
            headers=headers,
        )

    assert response.status_code == 401


def test_jwt_secret_empty_or_placeholder_is_rejected(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        auth.get_jwt_secret()
    monkeypatch.setenv("JWT_SECRET", "replace-this-placeholder-before-running")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        auth.get_jwt_secret()


def test_app_startup_refuses_empty_jwt_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        with TestClient(app):
            pass


def test_app_startup_initializes_schema(monkeypatch):
    initialized = []
    monkeypatch.setattr(main, "init_schema", lambda: initialized.append(True))

    with TestClient(app):
        pass

    assert initialized == [True]


def test_client_access_is_limited_to_assigned_clients():
    buyer = CurrentUser("b", "b@example.test", "Buyer", "buyer", ("brightleaf",))
    manager = CurrentUser("m", "m@example.test", "Manager", "account_manager", ("calmleaf",))

    assert user_can_access_client(buyer, "brightleaf")
    assert not user_can_access_client(buyer, "calmleaf")
    assert user_can_access_client(manager, "calmleaf")
    assert not user_can_access_client(manager, "brightleaf")
    with pytest.raises(HTTPException) as exc_info:
        require_client_access(buyer, "calmleaf")
    assert exc_info.value.status_code == 403


def test_only_buyer_and_account_manager_roles_are_valid():
    assert users.VALID_ROLES == {"buyer", "account_manager"}


def test_assignments_are_loaded_again_for_each_authenticated_request(monkeypatch):
    assigned = ["brightleaf"]
    monkeypatch.setattr(
        auth.users,
        "get_user_by_id",
        lambda user_id: {
            "user_id": user_id,
            "email": "buyer@example.test",
            "display_name": "Buyer",
            "role": "buyer",
            "is_active": True,
        },
    )
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda user_id: list(assigned))
    token = auth.create_access_token("buyer-1", "buyer")
    credentials = auth.HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    first = auth.get_current_user(credentials)
    assigned[:] = ["calmleaf"]
    second = auth.get_current_user(credentials)

    assert first.assigned_client_ids == ("brightleaf",)
    assert second.assigned_client_ids == ("calmleaf",)


def test_existing_token_for_inactive_user_is_rejected(monkeypatch):
    monkeypatch.setattr(
        auth.users,
        "get_user_by_id",
        lambda user_id: {
            "user_id": user_id,
            "email": "buyer@example.test",
            "display_name": "Buyer",
            "role": "buyer",
            "is_active": False,
        },
    )
    token = auth.create_access_token("buyer-1", "buyer")
    credentials = auth.HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user(credentials)
    assert exc_info.value.status_code == 401
