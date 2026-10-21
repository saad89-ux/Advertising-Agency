"""Correction-brief acceptance checks against a fresh, isolated Postgres schema."""
import uuid
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from fastapi.testclient import TestClient

from apps.api.auth import CurrentUser, get_current_user
from apps.api.database import get_database_url, get_session
from apps.api.main import app


@pytest.fixture(scope="module")
def clean_database():
    original = get_database_url()
    schema = "brief_test_" + uuid.uuid4().hex
    try:
        admin = psycopg.connect(original, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("Postgres is unavailable")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", make_conninfo(original, options=f"-csearch_path={schema}"))
    for key in ("META_MCP_ENABLE_LIVE", "META_MCP_ALLOW_WRITES"):
        patch.setenv(key, "false")
    for key in ("SMTP_HOST", "MODEL_API_KEY", "GEMINI_API_KEY"):
        patch.setenv(key, "")
    patch.setenv("JWT_SECRET", "test-only-secret-long-enough-for-hmac-acceptance")
    from apps.api.schema import init_schema
    from apps.api.campaign_flow import init_flow_tables
    # Acceptance schemas must never leak temporary ad IDs into the user's real Redis queue.
    patch.setattr("apps.api.uploads._dispatch_pending_jobs", lambda: None)
    try:
        init_schema()
        init_flow_tables()
        with get_session() as conn:
            conn.execute("INSERT INTO clients VALUES ('real-client', 'Real Client')")
            conn.execute("INSERT INTO users (user_id,email,display_name,password_hash,role) VALUES ('brief-user','reviewer@example.com','Reviewer','unused','buyer')")
            conn.execute("INSERT INTO user_client_assignments VALUES ('brief-user','real-client')")
        yield
    finally:
        patch.undo()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture
def client(clean_database):
    user = CurrentUser(user_id="brief-user", email="reviewer@example.com", display_name="Reviewer", role="buyer", assigned_client_ids=("real-client",))
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app, raise_server_exceptions=False, client=("198.51.100.1", 1234)) as session:
        yield session
    app.dependency_overrides.clear()


def payload(**overrides):
    return {"client_id": "real-client", "employee_email": "alerts@example.com", "employee_phone": "+15551234567",
            "target_platforms": ["facebook", "instagram"], "headline": "Our latest offer", "ad_text": "Explore our collection.",
            "destination_url": "https://example.com/?utm_source=facebook&utm_medium=paid_social&utm_campaign=collection_oct", **overrides}


def test_prompt_launch_and_repeat_history(client):
    response = client.post("/campaign-flow/launch-batch", json=payload(), headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "blocked"
    assert len(data["results"]) == 2
    assert data["notifications"][0]["email_status"] == "unconfigured_credentials"
    assert "sms_dispatch" not in data["notification_contact"]
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    batch = data["batch_id"]
    for _ in range(2):
        again = client.post("/campaign-flow/launch-batch", json=payload(batch_id=batch))
        assert again.status_code == 201, again.text
    history = client.get("/campaign-flow/chat-history").json()
    assert next(row for row in history if row["batch_id"] == batch)["ad_count"] == 1
    with get_session() as conn:
        row = conn.execute("SELECT a.row_hash,a.sheet_hash,p.content_hash,p.approved_by FROM ads a JOIN ad_approvals p USING (ad_id) WHERE a.batch_id=%s", (batch,)).fetchone()
        assert row[0] and row[1] and row[0] == row[2] and row[3] == "brief-user"
        assert conn.execute("SELECT COUNT(*) FROM campaign_launch_contacts WHERE batch_id=%s", (batch,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM send_alerts WHERE batch_id=%s", (batch,)).fetchone()[0] > 0
    assert client.get(f"/batches/{batch}").status_code == 200


def test_missing_url_cannot_borrow_other_batch(client):
    response = client.post("/campaign-flow/launch-batch", json=payload(destination_url=None), headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    assert "access-control-allow-origin" in response.headers


def test_email_only_launch_resolves_account_client_and_stores_contact(client):
    campaign = payload(client_id="")
    campaign.pop("employee_phone")
    resolved = client.post("/campaign-flow/resolve-client", json={"input_text": campaign["ad_text"]})
    assert resolved.status_code == 200 and resolved.json()["client_id"] == "real-client"
    response = client.post("/campaign-flow/launch-batch", json=campaign)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["status"] == "blocked"
    assert result["notification_contact"]["employee_email"] == "alerts@example.com"
    assert result["notification_contact"]["employee_phone"] == ""
    with get_session() as conn:
        contact = conn.execute("SELECT employee_email,employee_phone FROM campaign_launch_contacts WHERE batch_id=%s", (result["batch_id"],)).fetchone()
        assert contact == ("alerts@example.com", "")


def test_real_jwt_authentication_email_only_pipeline(client):
    from apps.api.auth import create_access_token
    app.dependency_overrides.clear()
    token = create_access_token("brief-user", "buyer")
    headers = {"Authorization": f"Bearer {token}", "Origin": "http://localhost:3000"}
    campaign = payload(client_id="")
    campaign.pop("employee_phone")
    # Exercise real JWT decoding and database client assignments, without an auth override.
    resolved = client.post("/campaign-flow/resolve-client", json={"input_text": campaign["ad_text"]}, headers=headers)
    assert resolved.status_code == 200 and resolved.json()["client_id"] == "real-client"
    review = client.post("/campaign-flow/preflight-review", json={"ad_overrides": {**campaign, "target_platforms": ["facebook"]}}, headers=headers)
    assert review.status_code == 200 and review.json()["status"] == "ready_for_review", review.text
    launched = client.post("/campaign-flow/launch-batch", json=campaign, headers=headers)
    assert launched.status_code == 201 and launched.json()["status"] == "blocked", launched.text
    batch_id = launched.json()["batch_id"]
    detail = client.get(f"/batches/{batch_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["summary"]["notification_contact"]["employee_email"] == campaign["employee_email"]
    assert client.get("/campaign-flow/chat-history", headers=headers).status_code == 200
    assert client.get("/alerts", headers=headers).status_code == 200


def test_account_manager_real_jwt_sheet_to_launch_pipeline(client, monkeypatch, tmp_path):
    from apps.api.auth import create_access_token
    from apps.worker.file_storage import LocalFileStore
    app.dependency_overrides.clear()
    with get_session() as conn:
        conn.execute("INSERT INTO users (user_id,email,display_name,password_hash,role) VALUES ('brief-manager','manager@example.com','Manager','unused','account_manager')")
        conn.execute("INSERT INTO user_client_assignments VALUES ('brief-manager','real-client')")
    headers = {"Authorization": f"Bearer {create_access_token('brief-manager', 'account_manager')}"}
    monkeypatch.setattr("apps.api.uploads.get_file_store", lambda: LocalFileStore(tmp_path))
    monkeypatch.setattr("apps.api.uploads.load_domain_map", lambda: {"real-client.example": "real-client", "other.example": "unassigned"})
    csv = b"Row,Platform,Headline,Ad text,URL\n1,facebook,Our offer,Explore our collection,https://real-client.example/?utm_source=facebook&utm_medium=paid_social&utm_campaign=collection_oct\n"
    response = client.post("/uploads", files={"file": ("manager-campaign.csv", csv, "text/csv")}, headers=headers)
    assert response.status_code == 201 and response.json()["accepted_count"] == 1, response.text
    batch_id = response.json()["batch_id"]
    resolved = client.post("/campaign-flow/resolve-client", json={"batch_id": batch_id}, headers=headers)
    assert resolved.status_code == 200 and resolved.json()["client_id"] == "real-client", resolved.text
    review = client.post("/campaign-flow/preflight-review", json={"batch_id": batch_id}, headers=headers)
    assert review.status_code == 200 and review.json()["status"] == "ready_for_review", review.text
    launch = client.post("/campaign-flow/launch-batch", json={"batch_id": batch_id, "employee_email": "alerts@example.com", "target_platforms": ["facebook"]}, headers=headers)
    assert launch.status_code == 201 and launch.json()["status"] == "blocked", launch.text
    assert client.get(f"/batches/{batch_id}", headers=headers).status_code == 200
    # Upload access is still scoped to the account manager's assigned clients.
    denied = client.post("/uploads", files={"file": ("unassigned.csv", csv.replace(b'real-client.example', b'other.example'), "text/csv")}, headers=headers)
    assert denied.status_code == 403, denied.text


def test_client_resolution_does_not_guess_between_assigned_clients(client):
    user = CurrentUser(user_id="brief-user", email="reviewer@example.com", display_name="Reviewer", role="buyer", assigned_client_ids=("real-client", "other-client"))
    app.dependency_overrides[get_current_user] = lambda: user
    response = client.post("/campaign-flow/resolve-client", json={"input_text": "A campaign with no brand identified"})
    assert response.status_code == 422
    response = client.post("/campaign-flow/resolve-client", json={"input_text": "Real Client autumn campaign"})
    assert response.status_code == 200 and response.json()["client_id"] == "real-client"


def test_empty_upload_not_in_history(client):
    before = client.get("/campaign-flow/chat-history").json()
    response = client.post("/uploads", files={"file": ("rejected.csv", b"Row,Platform,Headline,Ad text,URL\n1,facebook,Offer,Copy,https://unassigned.invalid\n", "text/csv")})
    assert response.status_code in (201, 422), response.text
    if response.status_code == 201:
        assert response.json()["accepted_count"] == 0
    assert client.get("/campaign-flow/chat-history").json() == before


def test_endpoint_sweep(client):
    paths = ["/campaign-flow/clients", "/campaign-flow/chat-history", "/campaign-flow/all-campaigns", "/campaign-flow/revenue-analytics",
             "/campaign-flow/platform-performance", "/campaign-flow/violations", "/campaign-flow/mcp-traces", "/campaign-flow/launcher-contact",
             "/batches", "/alerts", "/dead-letter", "/attribution/summary", "/attribution/ads", "/attribution/live-events", "/platforms/accounts", "/metrics", "/diagnostics/ai-tracing"]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
    assert client.post("/mock-data/seed").status_code == 404


def test_global_exception_cors_and_history_ip(client, monkeypatch):
    import apps.api.campaign_flow as flow
    with monkeypatch.context() as patch:
        patch.setattr(flow, "init_flow_tables", lambda: (_ for _ in ()).throw(RuntimeError("forced error")))
        response = client.post("/campaign-flow/launch-batch", json=payload(), headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 500
        assert isinstance(response.json()["detail"], str)
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        response = client.get("/campaign-flow/chat-history", headers={"Origin": "https://untrusted.invalid"})
        assert "access-control-allow-origin" not in response.headers
    # Untrusted callers cannot impersonate the recorded uploader through forwarded headers.
    monkeypatch.setenv("TRUSTED_PROXIES", "")
    with TestClient(app, client=("198.51.100.2", 1234)) as other:
        assert other.get("/campaign-flow/chat-history", headers={"X-Forwarded-For": "198.51.100.1"}).json() == []


def test_client_ip_trusted_and_mapped(monkeypatch):
    from apps.api.client_ip import get_client_ip
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"x-forwarded-for": "::ffff:198.51.100.4"})
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1")
    assert get_client_ip(request) == "198.51.100.4"
    monkeypatch.setenv("TRUSTED_PROXIES", "")
    assert get_client_ip(request) == "127.0.0.1"


def test_global_handler_returns_json_and_allows_only_configured_origin():
    from fastapi import FastAPI
    from apps.api.main import unhandled_exception
    probe = FastAPI()
    probe.add_exception_handler(Exception, unhandled_exception)
    @probe.get("/explode")
    def explode():
        raise RuntimeError("Forced unhandled error")
    with TestClient(probe, raise_server_exceptions=False) as session:
        response = session.get("/explode", headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 500 and isinstance(response.json()["detail"], str)
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        response = session.get("/explode", headers={"Origin": "https://untrusted.invalid"})
        assert "access-control-allow-origin" not in response.headers


def test_real_preflight_reports_missing_and_failed(client):
    response = client.post("/campaign-flow/preflight-review", json={"client_id": "real-client", "input_text": "Offer"})
    assert response.json()["status"] == "missing_requirements"
    response = client.post("/campaign-flow/preflight-review", json={"client_id": "real-client", "ad_overrides": payload(destination_url="https://example.com")})
    data = response.json()
    assert data["status"] == "checks_failed"
    assert any(c["name"] == "utm_presence" and c["status"] == "fail" for c in data["checks"])
    assert any(c["name"] == "wording" and c["status"] == "not_checked" for c in data["checks"])


@pytest.mark.parametrize("outcomes,expected", [((True, True), "launched"), ((True, False), "partial"), ((False, False), "failed")])
def test_launch_status_matches_connector_results(client, monkeypatch, outcomes, expected):
    from apps.worker.mcp_client import MCPPlatformDispatcher
    def call(self, platform, tool_name, arguments):
        if tool_name.endswith("track_performance"):
            return SimpleNamespace(success=False, result={}, error="not available", rpc_id="test")
        success = outcomes[0 if platform == "facebook" else 1]
        assert arguments["destination_url"].startswith("https://example.com/")
        assert arguments["ad_text"] == "Explore our collection."
        with get_session() as conn:
            approval = conn.execute("SELECT approved_by,content_hash FROM ad_approvals WHERE ad_id=%s", (arguments["ad_id"],)).fetchone()
            assert approval[0] == "brief-user" and approval[1]
        return SimpleNamespace(success=success, result={"platform_ad_id": "receipt-" + platform} if success else {}, error=None if success else "Platform rejected the submission", rpc_id="test")
    monkeypatch.setattr(MCPPlatformDispatcher, "call_tool", call)
    response = client.post("/campaign-flow/launch-batch", json=payload())
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == expected
    assert [result["status"] == "sent" for result in data["results"]] == list(outcomes)
    batch_id = data["batch_id"]
    if expected == "launched":
        # Recorded successful submissions are reused, so authorizing twice never sends twice.
        def unexpected(*args, **kwargs):
            raise AssertionError("Duplicate authorization called the platform")
        monkeypatch.setattr(MCPPlatformDispatcher, "call_tool", unexpected)
        repeated = client.post("/campaign-flow/launch-batch", json=payload(batch_id=batch_id))
        assert repeated.status_code == 201 and repeated.json()["status"] == "launched", repeated.text
    detail = client.get(f"/batches/{batch_id}").json()
    assert detail["summary"]["total_spend_usd"] is None


def test_partially_reported_metrics_keep_missing_values_null(client):
    batch = client.post("/campaign-flow/launch-batch", json=payload()).json()["batch_id"]
    with get_session() as conn:
        ad_id = conn.execute("SELECT ad_id FROM ads WHERE batch_id=%s", (batch,)).fetchone()[0]
        conn.execute("INSERT INTO ad_performance_tracking (ad_id,batch_id,client_id,platform,handle,impressions,clicks) VALUES (%s,%s,'real-client','facebook','recorded-handle',100,5)", (ad_id,batch))
    response = client.get("/campaign-flow/platform-performance")
    assert response.status_code == 200, response.text
    summary = response.json()["summary"]
    assert summary["total_sales_usd"] is None and summary["total_spend_usd"] is None
    assert summary["total_impressions"] == 100
    detail = client.get(f"/batches/{batch}").json()
    assert detail["summary"]["total_spend_usd"] is None
    assert detail["ads"][0]["spend_usd"] is None
    assert detail["ads"][0]["impressions"] == 100


def test_real_redis_worker_persists_findings_for_prompt_ad(client, monkeypatch):
    from apps.api import queue
    from apps.worker import job_runner
    redis_client = queue._redis_client()
    try:
        redis_client.ping()
    except Exception:
        redis_client.close()
        pytest.skip("Redis is unavailable")
    queue_name = "brief_test_jobs:" + uuid.uuid4().hex
    monkeypatch.setattr(queue, "AD_JOB_QUEUE", queue_name)
    monkeypatch.setattr(job_runner, "AD_JOB_QUEUE", queue_name)
    try:
        campaign = payload(client_id="")
        campaign.pop("employee_phone")
        response = client.post("/campaign-flow/launch-batch", json=campaign)
        assert response.status_code == 201, response.text
        batch_id = response.json()["batch_id"]
        with get_session() as conn:
            ad_id = conn.execute("SELECT ad_id FROM ads WHERE batch_id=%s", (batch_id,)).fetchone()[0]
            assert queue.enqueue_ad_jobs([queue.AdJob(batch_id=batch_id, ad_id=ad_id)], redis_client=redis_client) == 1
            processed = job_runner.run_once(redis_client=redis_client, connection=conn,
                        run_page_checks=False, run_wording_checks=False, manifest={}, reraise=True)
            assert processed is not None and processed.ad_id == ad_id
            assert conn.execute("SELECT COUNT(*) FROM findings WHERE ad_id=%s", (ad_id,)).fetchone()[0] > 0
        assert redis_client.llen(queue_name) == 0
    finally:
        redis_client.delete(queue_name)
        redis_client.close()
