import hashlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from apps.api import guest_sessions, main
from apps.api.auth import CurrentUser
from apps.api.batches import get_batch_detail
from apps.api.database import get_database_url, get_session
from apps.api.main import app
from apps.worker.file_storage import LocalFileStore


def _request(method="GET", headers=None, cookies=None):
    scope = {"type": "http", "method": method, "path": "/", "headers": [], "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1), "scheme": "http"}
    for key, value in (headers or {}).items():
        scope["headers"].append((key.lower().encode(), value.encode()))
    if cookies:
        scope["headers"].append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request(scope)


def test_guest_cookie_is_random_opaque_and_server_stores_only_hash(monkeypatch):
    writes = []

    class Connection:
        def execute(self, sql, params=None):
            writes.append((sql, params))

    @contextmanager
    def session():
        yield Connection()

    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setattr(guest_sessions, "init_guest_tables", lambda: None)
    monkeypatch.setattr(guest_sessions, "get_session", session)
    monkeypatch.setenv("FRONTEND_ORIGINS", "http://localhost:3000")
    with TestClient(app) as client:
        response = client.post("/auth/guest-session", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 200
    assert response.json() == {"status": "created", "guest": True}
    cookie = response.cookies[guest_sessions.COOKIE_NAME]
    assert len(cookie) >= 40
    assert cookie not in response.text
    assert "httponly" in response.headers["set-cookie"].lower()
    session_insert = next(item for item in writes if "insert into guest_sessions" in item[0].lower())
    assert any("guest_session_creation_limits" in item[0].lower() for item in writes)
    assert session_insert[1][1] == guest_sessions._token_hash(cookie)
    assert cookie not in session_insert[1]


def test_guest_session_creation_is_rate_limited_before_ownership_rows(monkeypatch):
    statements = []

    class Result:
        def fetchone(self): return (21,)

    class Connection:
        def execute(self, sql, params=None):
            statements.append(sql)
            return Result()

    @contextmanager
    def session():
        yield Connection()

    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setattr(guest_sessions, "init_guest_tables", lambda: None)
    monkeypatch.setattr(guest_sessions, "get_session", session)
    monkeypatch.setenv("FRONTEND_ORIGINS", "http://localhost:3000")
    with TestClient(app) as client:
        response = client.post("/auth/guest-session", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 429
    assert any("guest_session_creation_limits" in statement for statement in statements)
    assert not any("insert into guest_sessions" in statement.lower() for statement in statements)


def test_malformed_guest_cookie_is_rejected_without_fallback(monkeypatch):
    from apps.api.guest_sessions import get_request_user

    request = _request(cookies={guest_sessions.COOKIE_NAME: "guessable"})
    with pytest.raises(HTTPException) as exc:
        get_request_user(request, credentials=None)
    assert exc.value.status_code == 401


def test_valid_cookie_resolves_only_server_recorded_guest_identity(monkeypatch):
    token = "A" * 43

    class Result:
        def fetchone(self): return ("session-123", "guest_client_123")

    class Connection:
        def execute(self, sql, params):
            assert guest_sessions._token_hash(token) == params[0]
            assert "revoked_at IS NULL" in sql
            assert "expires_at > NOW()" in sql
            return Result()

    @contextmanager
    def session():
        yield Connection()

    monkeypatch.setattr(guest_sessions, "get_session", session)
    user = guest_sessions.get_guest_user(_request(cookies={guest_sessions.COOKIE_NAME: token}))
    assert user.guest_session_id == "session-123"
    assert user.assigned_client_ids == ("guest_client_123",)
    assert user.role == "guest"


def test_bad_authorization_cannot_fall_back_to_guest_cookie(monkeypatch):
    from apps.api.guest_sessions import get_request_user

    request = _request(headers={"Authorization": "Bearer invalid"}, cookies={guest_sessions.COOKIE_NAME: "A" * 43})
    with pytest.raises(HTTPException) as exc:
        get_request_user(request, credentials=None)
    assert exc.value.status_code == 401


def test_cookie_mutations_require_allowlisted_origin(monkeypatch):
    monkeypatch.setenv("FRONTEND_ORIGINS", "https://app.example.test")
    guest_sessions.require_guest_csrf(_request("POST", {"Origin": "https://app.example.test", "Content-Type": "application/json"}))
    with pytest.raises(HTTPException) as exc:
        guest_sessions.require_guest_csrf(_request("POST", {"Origin": "https://evil.example.test"}))
    assert exc.value.status_code == 403


def test_guest_cannot_read_batch_owned_by_another_session():
    class Result:
        def __init__(self, row): self.row = row
        def fetchone(self): return self.row
        def fetchall(self): return []

    class Connection:
        def execute(self, sql, params=None):
            if "select batch_id, uploaded_by" in sql.lower():
                return Result(("private-batch", None, datetime.now(timezone.utc), "x.csv", "hash", None, "owner-session"))
            if "count(*) from ads" in sql.lower():
                return Result((0,))
            if "from ads" in sql.lower():
                return Result(None)
            raise AssertionError(sql)

    guest = CurrentUser("guest:other", "", "Guest", "guest", ("guest_client",), guest_session_id="other-session")
    with pytest.raises(HTTPException) as exc:
        get_batch_detail("private-batch", guest, Connection())
    assert exc.value.status_code == 404


def test_same_ip_multiple_guests_isolation_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify that multiple guest sessions sharing the exact same client IP remain strictly isolated."""
    original = get_database_url()
    schema = "guest_iso_" + uuid.uuid4().hex
    try:
        admin = psycopg.connect(original, autocommit=True, connect_timeout=5)
    except Exception:
        pytest.skip("Postgres is unavailable for same-IP guest isolation integration test")

    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    monkeypatch.setenv("DATABASE_URL", make_conninfo(original, options=f"-csearch_path={schema}"))
    monkeypatch.setenv("FRONTEND_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-long-enough-for-hmac-acceptance")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_FILE_STORE_ROOT", str(tmp_path))
    for key in ("META_MCP_ENABLE_LIVE", "META_MCP_ALLOW_WRITES", "SMTP_HOST", "MODEL_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(key, "")

    file_store = LocalFileStore(tmp_path)
    monkeypatch.setattr("apps.api.uploads.get_file_store", lambda: file_store)
    monkeypatch.setattr("apps.api.uploads._dispatch_pending_jobs", lambda: None)

    from apps.api.schema import init_schema
    from apps.api.campaign_flow import init_flow_tables

    try:
        init_schema()
        init_flow_tables()

        shared_ip = "198.51.100.99"
        origin_header = {"Origin": "http://localhost:3000"}

        # Simulate two concurrent visitors (Guest A and Guest B) connecting from the exact same IP
        with TestClient(app, client=(shared_ip, 51001)) as client_a, \
             TestClient(app, client=(shared_ip, 51002)) as client_b:

            # 1. Distinct tokens and distinct server-assigned identities
            resp_a = client_a.post("/auth/guest-session", headers=origin_header)
            assert resp_a.status_code == 200
            assert resp_a.json() == {"status": "created", "guest": True}
            cookie_a = resp_a.cookies[guest_sessions.COOKIE_NAME]

            resp_b = client_b.post("/auth/guest-session", headers=origin_header)
            assert resp_b.status_code == 200
            assert resp_b.json() == {"status": "created", "guest": True}
            cookie_b = resp_b.cookies[guest_sessions.COOKIE_NAME]

            assert cookie_a != cookie_b
            assert len(cookie_a) >= 40 and len(cookie_b) >= 40

            with get_session() as conn:
                row_a = conn.execute(
                    "SELECT session_id, client_id, created_ip FROM guest_sessions WHERE token_hash=%s",
                    (guest_sessions._token_hash(cookie_a),),
                ).fetchone()
                row_b = conn.execute(
                    "SELECT session_id, client_id, created_ip FROM guest_sessions WHERE token_hash=%s",
                    (guest_sessions._token_hash(cookie_b),),
                ).fetchone()
                assert row_a is not None and row_b is not None
                session_id_a, client_id_a, ip_a = row_a
                session_id_b, client_id_b, ip_b = row_b

            assert session_id_a != session_id_b
            assert client_id_a != client_id_b
            assert ip_a == shared_ip and ip_b == shared_ip

            # 2. Associated only with their own server-assigned guest client
            res_resolve_a = client_a.post("/campaign-flow/resolve-client", json={"input_text": ""}, headers=origin_header)
            assert res_resolve_a.status_code == 200
            assert res_resolve_a.json()["client_id"] == client_id_a

            res_resolve_b = client_b.post("/campaign-flow/resolve-client", json={"input_text": ""}, headers=origin_header)
            assert res_resolve_b.status_code == 200
            assert res_resolve_b.json()["client_id"] == client_id_b

            # Attempting to substitute the other guest's client ID returns 404
            res_substitute = client_a.post("/campaign-flow/resolve-client", json={"client_id": client_id_b}, headers=origin_header)
            assert res_substitute.status_code == 404

            # 3. Create independent batches for Guest A and Guest B
            csv_a = b"Row,Platform,Headline,Ad text,URL\n1,facebook,Spring Sale A,Organic tea,https://example.com/tea\n"
            up_a = client_a.post("/uploads", files={"file": ("guest_a.csv", csv_a, "text/csv")}, headers=origin_header)
            assert up_a.status_code == 201
            batch_a_id = up_a.json()["batch_id"]

            csv_b = b"Row,Platform,Headline,Ad text,URL\n1,instagram,Summer Sale B,Handmade chair,https://example.com/chair\n"
            up_b = client_b.post("/uploads", files={"file": ("guest_b.csv", csv_b, "text/csv")}, headers=origin_header)
            assert up_b.status_code == 201
            batch_b_id = up_b.json()["batch_id"]

            assert batch_a_id != batch_b_id

            # 4. Each guest can access their own batch
            get_own_a = client_a.get(f"/batches/{batch_a_id}")
            assert get_own_a.status_code == 200
            assert get_own_a.json()["batch_id"] == batch_a_id

            get_own_b = client_b.get(f"/batches/{batch_b_id}")
            assert get_own_b.status_code == 200
            assert get_own_b.json()["batch_id"] == batch_b_id

            # 5. Cross-batch retrieval by guessing batch ID is rejected (404)
            probe_b = client_a.get(f"/batches/{batch_b_id}")
            assert probe_b.status_code == 404

            probe_a = client_b.get(f"/batches/{batch_a_id}")
            assert probe_a.status_code == 404

            # 6. Listing and chat history do not leak across sessions
            list_a = client_a.get("/batches")
            assert list_a.status_code == 200
            ids_a = [item["batch_id"] for item in list_a.json()]
            assert batch_a_id in ids_a and batch_b_id not in ids_a

            list_b = client_b.get("/batches")
            assert list_b.status_code == 200
            ids_b = [item["batch_id"] for item in list_b.json()]
            assert batch_b_id in ids_b and batch_a_id not in ids_b

            hist_a = client_a.get("/campaign-flow/chat-history")
            assert hist_a.status_code == 200
            hist_ids_a = [item["batch_id"] for item in hist_a.json()]
            assert batch_a_id in hist_ids_a and batch_b_id not in hist_ids_a

            hist_b = client_b.get("/campaign-flow/chat-history")
            assert hist_b.status_code == 200
            hist_ids_b = [item["batch_id"] for item in hist_b.json()]
            assert batch_b_id in hist_ids_b and batch_a_id not in hist_ids_b

            # 7. Metrics and live status cannot be accessed across sessions
            assert client_a.get(f"/batches/{batch_a_id}/metrics").status_code == 200
            assert client_b.get(f"/batches/{batch_b_id}/metrics").status_code == 200
            assert client_a.get(f"/batches/{batch_b_id}/metrics").status_code == 404
            assert client_b.get(f"/batches/{batch_a_id}/metrics").status_code == 404
            assert client_a.get(f"/batches/{batch_b_id}/metrics/summary").status_code == 404
            assert client_b.get(f"/batches/{batch_a_id}/metrics/summary").status_code == 404
            assert client_a.get(f"/batches/{batch_b_id}/metrics/export").status_code == 404
            assert client_b.get(f"/batches/{batch_a_id}/metrics/export").status_code == 404
            assert client_a.get(f"/batches/{batch_b_id}/live-status").status_code == 404
            assert client_b.get(f"/batches/{batch_a_id}/live-status").status_code == 404

            # 8. Triggering rate limiting on shared IP does not merge or alter existing sessions
            with get_session() as conn:
                rate_key = hashlib.sha256(shared_ip.encode("utf-8")).hexdigest()
                conn.execute(
                    """INSERT INTO guest_session_creation_limits(ip_hash, window_hour, attempts)
                       VALUES (%s, date_trunc('hour', NOW()), 25)
                       ON CONFLICT (ip_hash, window_hour) DO UPDATE SET attempts=25""",
                    (rate_key,),
                )

            # Subsequent new guest session creation on that IP is rate-limited (429)
            with TestClient(app, client=(shared_ip, 51003)) as client_c:
                resp_c = client_c.post("/auth/guest-session", headers=origin_header)
                assert resp_c.status_code == 429

            # Existing sessions A and B continue working with zero cross-tenant contamination
            assert client_a.get(f"/batches/{batch_a_id}").status_code == 200
            assert client_b.get(f"/batches/{batch_b_id}").status_code == 200
            assert client_a.get(f"/batches/{batch_b_id}").status_code == 404
            assert client_b.get(f"/batches/{batch_a_id}").status_code == 404
    finally:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
