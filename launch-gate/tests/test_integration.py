"""Real-service integration tests against PostgreSQL and Redis.

Opt-in only: runs only when RUN_INTEGRATION=1.
Requires TEST_DATABASE_URL and TEST_REDIS_URL pointing to isolated test instances.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from urllib.parse import urlsplit

import psycopg
import pytest
from redis import Redis
from starlette.testclient import TestClient

from apps.api import auth, database, outbox, queue, schema, uploads, users
from apps.api.auth import hash_password
from apps.api.main import app


pytestmark = [pytest.mark.integration]


def verify_test_service_isolation(
    *,
    dev_db_url: str,
    test_db_url: str,
    dev_redis_url: str,
    test_redis_url: str,
) -> None:
    """Validate that test targets are strictly isolated from development targets."""
    if not test_db_url or not test_db_url.strip():
        pytest.fail("Refusing to run: TEST_DATABASE_URL must be set in the environment or .env file")
    if not test_redis_url or not test_redis_url.strip():
        pytest.fail("Refusing to run: TEST_REDIS_URL must be set in the environment or .env file")

    test_db_name = urlsplit(test_db_url).path.lstrip("/").split("?")[0]
    dev_db_name = urlsplit(dev_db_url).path.lstrip("/").split("?")[0] if dev_db_url else ""

    if not test_db_name.endswith("_test"):
        pytest.fail(
            f"Refusing to run: TEST_DATABASE_URL database name '{test_db_name}' must end with '_test'"
        )
    if dev_db_name and test_db_name == dev_db_name:
        pytest.fail(
            f"Refusing to run: TEST_DATABASE_URL database name '{test_db_name}' equals the dev database name"
        )

    def _redis_index(url: str) -> int:
        if not url:
            return 0
        path = urlsplit(url).path.lstrip("/").split("?")[0]
        return int(path) if path else 0

    test_redis_index = _redis_index(test_redis_url)
    dev_redis_index = _redis_index(dev_redis_url) if dev_redis_url else 0

    if dev_redis_url and test_redis_index == dev_redis_index:
        pytest.fail(
            f"Refusing to run: TEST_REDIS_URL index {test_redis_index} equals the dev Redis index {dev_redis_index}"
        )


def _clean_test_db(test_db_url: str) -> None:
    try:
        with psycopg.connect(test_db_url, autocommit=True) as conn:
            conn.execute("""
                DROP TABLE IF EXISTS findings CASCADE;
                DROP TABLE IF EXISTS batch_rejected_rows CASCADE;
                DROP TABLE IF EXISTS ad_job_outbox CASCADE;
                DROP TABLE IF EXISTS ads CASCADE;
                DROP TABLE IF EXISTS sheet_claims CASCADE;
                DROP TABLE IF EXISTS batches CASCADE;
                DROP TABLE IF EXISTS user_client_assignments CASCADE;
                DROP TABLE IF EXISTS users CASCADE;
                DROP TABLE IF EXISTS clients CASCADE;
                DROP TABLE IF EXISTS client_rules CASCADE;
                DROP TABLE IF EXISTS cost_ledger CASCADE;
            """)
    except Exception as exc:
        pytest.fail(f"PostgreSQL service at TEST_DATABASE_URL is unreachable or error: {exc}")


def _clean_test_redis(test_redis_url: str) -> None:
    try:
        r = Redis.from_url(test_redis_url, decode_responses=True)
        r.flushdb()
        r.close()
    except Exception as exc:
        pytest.fail(f"Redis service at TEST_REDIS_URL is unreachable or error: {exc}")


@pytest.fixture
def real_services(monkeypatch):
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION is not set to 1; integration tests are skipped")

    auth.load_dotenv()
    dev_db_url = os.environ.get("DATABASE_URL", "")
    test_db_url = os.environ.get("TEST_DATABASE_URL", "")
    dev_redis_url = os.environ.get("REDIS_URL", "")
    test_redis_url = os.environ.get("TEST_REDIS_URL", "")

    verify_test_service_isolation(
        dev_db_url=dev_db_url,
        test_db_url=test_db_url,
        dev_redis_url=dev_redis_url,
        test_redis_url=test_redis_url,
    )

    # Clean test targets before test
    _clean_test_db(test_db_url)
    _clean_test_redis(test_redis_url)

    # Route all database and queue operations to TEST instances
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production-launch-gate-key")
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("REDIS_URL", test_redis_url)
    monkeypatch.setattr(database, "get_database_url", lambda: test_db_url)
    monkeypatch.setattr(
        queue, "_redis_client", lambda: Redis.from_url(test_redis_url, decode_responses=True)
    )

    yield {
        "test_db_url": test_db_url,
        "test_redis_url": test_redis_url,
    }

    # Clean test targets after test
    _clean_test_db(test_db_url)
    _clean_test_redis(test_redis_url)


def test_refusal_logic_unit_checks():
    """Verify that isolation verification refuses to run with invalid configurations."""
    # 1. Missing test DB url
    with pytest.raises(pytest.fail.Exception, match="TEST_DATABASE_URL must be set"):
        verify_test_service_isolation(
            dev_db_url="postgresql://localhost/launch_gate",
            test_db_url="",
            dev_redis_url="redis://localhost:6379/0",
            test_redis_url="redis://localhost:6379/1",
        )

    # 2. Test DB name does not end with _test
    with pytest.raises(pytest.fail.Exception, match="must end with '_test'"):
        verify_test_service_isolation(
            dev_db_url="postgresql://localhost/launch_gate",
            test_db_url="postgresql://localhost/launch_gate_staging",
            dev_redis_url="redis://localhost:6379/0",
            test_redis_url="redis://localhost:6379/1",
        )

    # 3. Test DB name equals dev DB name
    with pytest.raises(pytest.fail.Exception, match="equals the dev database name"):
        verify_test_service_isolation(
            dev_db_url="postgresql://localhost/launch_gate_test",
            test_db_url="postgresql://localhost/launch_gate_test",
            dev_redis_url="redis://localhost:6379/0",
            test_redis_url="redis://localhost:6379/1",
        )

    # 4. Test Redis index equals dev Redis index
    with pytest.raises(pytest.fail.Exception, match="equals the dev Redis index"):
        verify_test_service_isolation(
            dev_db_url="postgresql://localhost/launch_gate",
            test_db_url="postgresql://localhost/launch_gate_test",
            dev_redis_url="redis://localhost:6379/0",
            test_redis_url="redis://localhost:6379/0",
        )


def _seed_test_users_and_clients(test_db_url: str) -> None:
    """Initialize schema and seed standard pilot clients and demo users."""
    schema.init_schema()

    with psycopg.connect(test_db_url) as conn:
        for client_id in ("brightleaf", "calmleaf", "oakandhome"):
            conn.execute(
                """INSERT INTO clients (client_id, name) VALUES (%s, %s)
                   ON CONFLICT (client_id) DO UPDATE SET name = EXCLUDED.name""",
                (client_id, client_id.title()),
            )

    # Seed buyer (assigned to brightleaf only)
    users.create_user(
        user_id="integration-buyer-1",
        email="buyer@test.test",
        display_name="Integration Buyer",
        password_hash=hash_password("ValidPassword123!"),
        role="buyer",
    )
    users.assign_user_to_client("integration-buyer-1", "brightleaf")

    # Seed account manager
    users.create_user(
        user_id="integration-am-1",
        email="am@test.test",
        display_name="Integration AM",
        password_hash=hash_password("ValidPassword123!"),
        role="account_manager",
    )


def test_init_schema_and_seeding(real_services):
    """Cover init_schema and seeding users in real PostgreSQL."""
    test_db_url = real_services["test_db_url"]
    _seed_test_users_and_clients(test_db_url)

    with psycopg.connect(test_db_url) as conn:
        users_count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        clients_count = conn.execute("SELECT count(*) FROM clients").fetchone()[0]
        assignments_count = conn.execute("SELECT count(*) FROM user_client_assignments").fetchone()[0]

    assert users_count == 2
    assert clients_count == 3
    assert assignments_count == 1


def test_login_and_upload_lifecycle(real_services):
    """Cover login 401/200, upload 201, repeat 200, canonical platform, sheet_06 header, queue len."""
    test_db_url = real_services["test_db_url"]
    test_redis_url = real_services["test_redis_url"]
    _seed_test_users_and_clients(test_db_url)

    client = TestClient(app)

    # 1. Login 401
    bad_login = client.post(
        "/auth/login",
        json={"email": "buyer@test.test", "password": "WrongPassword"},
    )
    assert bad_login.status_code == 401
    assert bad_login.json()["detail"] == "invalid credentials"

    # 2. Login 200
    good_login = client.post(
        "/auth/login",
        json={"email": "buyer@test.test", "password": "ValidPassword123!"},
    )
    assert good_login.status_code == 200
    token = good_login.json()["access_token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    # 3. Upload 201 with Meta platform (F2) and 'creative file name' header (F1)
    csv_content = (
        "Row,Platform,Headline,Destination URL,creative file name\n"
        "1,Meta,Summer Sale,https://brightleaf.example/promo,summer_banner.png\n"
    ).encode("utf-8")

    upload_res = client.post(
        "/uploads",
        files={"file": ("sheet_06_style.csv", csv_content, "text/csv")},
        headers=auth_header,
    )
    assert upload_res.status_code == 201
    res_data = upload_res.json()
    batch_id = res_data["batch_id"]
    assert res_data["accepted_count"] == 1
    assert res_data["rejected_count"] == 0
    assert res_data["duplicate_sheet"] is False

    # 4. Verify canonical platform stored in ads.platform and raw in ads.platform_raw (F2)
    #    and creative file kept from 'creative file name' header (F1)
    with psycopg.connect(test_db_url) as conn:
        ad_row = conn.execute(
            """SELECT platform, platform_raw, creative_file, destination_url
               FROM ads WHERE batch_id = %s""",
            (batch_id,),
        ).fetchone()

    assert ad_row is not None
    assert ad_row[0] == "facebook"  # Canonical normalized platform
    assert ad_row[1] == "Meta"      # platform_raw preserved
    assert ad_row[2] == "summer_banner.png"  # creative_file kept

    # 5. Verify queue length equals accepted count
    redis_client = Redis.from_url(test_redis_url, decode_responses=True)
    queue_len = redis_client.llen(queue.AD_JOB_QUEUE)
    assert queue_len == 1

    queued_item = json.loads(redis_client.lpop(queue.AD_JOB_QUEUE))
    assert queued_item["batch_id"] == batch_id

    # 6. Repeated upload 200 with original batch_id
    repeat_res = client.post(
        "/uploads",
        files={"file": ("sheet_06_style.csv", csv_content, "text/csv")},
        headers=auth_header,
    )
    assert repeat_res.status_code == 200
    repeat_data = repeat_res.json()
    assert repeat_data["batch_id"] == batch_id
    assert repeat_data["duplicate_sheet"] is True
    assert repeat_data["accepted_count"] == 0


def test_multi_client_sheet_and_rejected_rows_saved(real_services):
    """Cover multi-client sheet with per-row client_not_assigned and rejected rows saved."""
    test_db_url = real_services["test_db_url"]
    _seed_test_users_and_clients(test_db_url)

    client = TestClient(app)
    login_res = client.post(
        "/auth/login",
        json={"email": "buyer@test.test", "password": "ValidPassword123!"},
    )
    token = login_res.json()["access_token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    # Row 1 is brightleaf (assigned), Row 2 is calmleaf (unassigned to buyer)
    csv_content = (
        "Row,Platform,Headline,Destination URL,Creative file\n"
        "1,Facebook,Bright Promo,https://brightleaf.example/promo,b.png\n"
        "2,Facebook,Calm Promo,https://calmleaf.example/promo,c.png\n"
    ).encode("utf-8")

    upload_res = client.post(
        "/uploads",
        files={"file": ("multi_client.csv", csv_content, "text/csv")},
        headers=auth_header,
    )
    assert upload_res.status_code == 201
    res_data = upload_res.json()
    batch_id = res_data["batch_id"]
    assert res_data["accepted_count"] == 1
    assert res_data["rejected_count"] == 1
    assert res_data["rejected_rows"][0]["reason_code"] == "client_not_assigned"
    assert res_data["rejected_rows"][0]["data_row_index"] == 2

    # Verify rejected row persisted in real PostgreSQL batch_rejected_rows
    with psycopg.connect(test_db_url) as conn:
        rejected_in_db = conn.execute(
            """SELECT data_row_index, reason_code, reason
               FROM batch_rejected_rows WHERE batch_id = %s""",
            (batch_id,),
        ).fetchall()
        ads_in_db = conn.execute(
            "SELECT client_id FROM ads WHERE batch_id = %s",
            (batch_id,),
        ).fetchall()

    assert len(rejected_in_db) == 1
    assert rejected_in_db[0][0] == 2
    assert rejected_in_db[0][1] == "client_not_assigned"
    assert len(ads_in_db) == 1
    assert ads_in_db[0][0] == "brightleaf"


def test_failure_after_claim_leaves_nothing_saved(real_services, monkeypatch):
    """Cover failure after claim leaves nothing saved (atomic rollback)."""
    test_db_url = real_services["test_db_url"]
    _seed_test_users_and_clients(test_db_url)

    client = TestClient(app)
    login_res = client.post(
        "/auth/login",
        json={"email": "buyer@test.test", "password": "ValidPassword123!"},
    )
    token = login_res.json()["access_token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    csv_content = (
        "Row,Platform,Headline,Destination URL,Creative file\n"
        "1,Facebook,Promo,https://brightleaf.example/promo,b.png\n"
    ).encode("utf-8")

    # Inject failure after sheet_claims insertion but before commit
    orig_persist_ads = uploads.ads.persist_ads_on_connection

    def fail_persist_ads(conn, ads):
        raise RuntimeError("Simulated failure after claim")

    monkeypatch.setattr(uploads.ads, "persist_ads_on_connection", fail_persist_ads)

    with pytest.raises(RuntimeError, match="Simulated failure after claim"):
        client.post(
            "/uploads",
            files={"file": ("rollback_test.csv", csv_content, "text/csv")},
            headers=auth_header,
        )

    # Verify nothing was saved in batches, sheet_claims, ads, or rejected_rows
    with psycopg.connect(test_db_url) as conn:
        batches_count = conn.execute("SELECT count(*) FROM batches").fetchone()[0]
        claims_count = conn.execute("SELECT count(*) FROM sheet_claims").fetchone()[0]
        ads_count = conn.execute("SELECT count(*) FROM ads").fetchone()[0]

    assert batches_count == 0
    assert claims_count == 0
    assert ads_count == 0

    # Retrying the upload without failure succeeds
    monkeypatch.setattr(uploads.ads, "persist_ads_on_connection", orig_persist_ads)
    retry_res = client.post(
        "/uploads",
        files={"file": ("rollback_test.csv", csv_content, "text/csv")},
        headers=auth_header,
    )
    assert retry_res.status_code == 201


def test_redis_down_leaves_jobs_pending_in_outbox(real_services, monkeypatch):
    """Cover Redis down leaves jobs pending in ad_job_outbox and dispatch_pending_jobs sends them later."""
    test_db_url = real_services["test_db_url"]
    test_redis_url = real_services["test_redis_url"]
    _seed_test_users_and_clients(test_db_url)

    client = TestClient(app)
    login_res = client.post(
        "/auth/login",
        json={"email": "buyer@test.test", "password": "ValidPassword123!"},
    )
    token = login_res.json()["access_token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    csv_content = (
        "Row,Platform,Headline,Destination URL,Creative file\n"
        "1,Google,Promo 1,https://brightleaf.example/p1,b1.png\n"
        "2,Google,Promo 2,https://brightleaf.example/p2,b2.png\n"
    ).encode("utf-8")

    # Simulate Redis being completely unreachable during upload dispatch
    def fake_enqueue_failing(jobs, redis_client=None):
        raise ConnectionError("Redis is down")

    monkeypatch.setattr(uploads, "enqueue_ad_jobs", fake_enqueue_failing)

    upload_res = client.post(
        "/uploads",
        files={"file": ("outbox_test.csv", csv_content, "text/csv")},
        headers=auth_header,
    )
    assert upload_res.status_code == 201
    batch_id = upload_res.json()["batch_id"]

    # Verify ads were saved in DB and jobs remain in outbox with queued_at IS NULL
    with psycopg.connect(test_db_url) as conn:
        outbox_rows = conn.execute(
            """SELECT batch_id, ad_id, queued_at
               FROM ad_job_outbox WHERE batch_id = %s""",
            (batch_id,),
        ).fetchall()

    assert len(outbox_rows) == 2
    assert all(row[2] is None for row in outbox_rows)

    # Redis queue is empty
    redis_client = Redis.from_url(test_redis_url, decode_responses=True)
    assert redis_client.llen(queue.AD_JOB_QUEUE) == 0

    # Redis comes back up: dispatch_pending_jobs sends them
    monkeypatch.setattr(uploads, "enqueue_ad_jobs", queue.enqueue_ad_jobs)
    dispatched_count = outbox.dispatch_pending_jobs(enqueue_fn=queue.enqueue_ad_jobs)
    assert dispatched_count == 2

    # In PostgreSQL, queued_at is now stamped
    with psycopg.connect(test_db_url) as conn:
        updated_outbox = conn.execute(
            """SELECT queued_at FROM ad_job_outbox WHERE batch_id = %s""",
            (batch_id,),
        ).fetchall()
    assert all(row[0] is not None for row in updated_outbox)

    # In Redis, queue now has the 2 jobs
    assert redis_client.llen(queue.AD_JOB_QUEUE) == 2


def test_worker_job_runner_integration(real_services, monkeypatch):
    """Cover worker runner against real PostgreSQL and Redis (Task W1).

    Verifies:
    1. one job -> expected findings in PostgreSQL findings table
    2. same job twice -> no duplicates (idempotent)
    3. clean ad -> zero findings
    4. crash mid-job -> job still retryable in Redis
    """
    from apps.worker import job_runner
    from apps.worker.job_runner import run_once

    test_db_url = real_services["test_db_url"]
    test_redis_url = real_services["test_redis_url"]
    _seed_test_users_and_clients(test_db_url)

    client = TestClient(app)
    login_res = client.post(
        "/auth/login",
        json={"email": "buyer@test.test", "password": "ValidPassword123!"},
    )
    token = login_res.json()["access_token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    # Upload two ads: row 1 has missing utm_source, row 2 is clean
    csv_content = (
        "Row,Platform,Headline,Ad Text,Destination URL,Creative file\n"
        "1,Facebook,Promo 1,Text 1,https://brightleaf.example/promo?utm_medium=paid_social&utm_campaign=brand_jan,brightleaf_300x250.png\n"
        "2,Facebook,Promo 2,Text 2,https://brightleaf.example/landing?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan,brightleaf_300x250.png\n"
    ).encode("utf-8")

    upload_res = client.post(
        "/uploads",
        files={"file": ("worker_test.csv", csv_content, "text/csv")},
        headers=auth_header,
    )
    assert upload_res.status_code == 201
    batch_id = upload_res.json()["batch_id"]

    redis_client = Redis.from_url(test_redis_url, decode_responses=True)
    assert redis_client.llen(queue.AD_JOB_QUEUE) == 2

    # Get ad IDs from test DB
    with psycopg.connect(test_db_url) as conn:
        ad_rows = conn.execute(
            """SELECT ad_id, row_number FROM ads WHERE batch_id = %s ORDER BY row_number""",
            (batch_id,),
        ).fetchall()
    assert len(ad_rows) == 2
    ad_id_1, ad_id_2 = ad_rows[0][0], ad_rows[1][0]

    # 1. Process job 1: one job -> expected findings
    processed_job_1 = run_once(redis_client=redis_client, run_page_checks=False)
    assert processed_job_1 is not None
    assert processed_job_1.ad_id == ad_id_1

    with psycopg.connect(test_db_url) as conn:
        findings_1 = conn.execute(
            """SELECT finding_id, field, proposed_fix, check_type
               FROM findings WHERE ad_id = %s""",
            (ad_id_1,),
        ).fetchall()

    assert len(findings_1) > 0
    fields_1 = {row[1] for row in findings_1}
    assert "utm_source" in fields_1
    assert all(row[3] == "mechanical" for row in findings_1)

    # 2. Same job twice -> no duplicates (idempotency)
    # Re-push job 1 to Redis
    queue.enqueue_ad_jobs([processed_job_1], redis_client=redis_client)
    repeat_job_1 = run_once(redis_client=redis_client, run_page_checks=False)
    assert repeat_job_1 == processed_job_1

    with psycopg.connect(test_db_url) as conn:
        findings_1_repeat = conn.execute(
            """SELECT finding_id FROM findings WHERE ad_id = %s""",
            (ad_id_1,),
        ).fetchall()
    assert len(findings_1_repeat) == len(findings_1)

    # 3. Clean ad -> zero findings
    processed_job_2 = run_once(redis_client=redis_client, run_page_checks=False)
    assert processed_job_2 is not None
    assert processed_job_2.ad_id == ad_id_2

    with psycopg.connect(test_db_url) as conn:
        findings_2 = conn.execute(
            """SELECT finding_id FROM findings WHERE ad_id = %s""",
            (ad_id_2,),
        ).fetchall()
    assert len(findings_2) == 0

    # 4. Crash mid-job -> job still retryable
    # Enqueue a test job
    crash_job = queue.AdJob(batch_id=batch_id, ad_id=ad_id_1)
    queue.enqueue_ad_jobs([crash_job], redis_client=redis_client)
    assert redis_client.llen(queue.AD_JOB_QUEUE) == 1

    def crashing_process(job, **kwargs):
        raise RuntimeError("Injected worker crash")

    monkeypatch.setattr(job_runner, "process_job", crashing_process)
    crash_res = run_once(redis_client=redis_client)
    assert crash_res is None
    # Job was preserved in Redis queue for retry
    assert redis_client.llen(queue.AD_JOB_QUEUE) == 1
    retry_popped = queue.dequeue_ad_job(redis_client=redis_client)
    assert retry_popped == crash_job


def test_retrieval_client_isolation_integration(real_services, monkeypatch):
    """Real PostgreSQL + pgvector test: verify client-filtered retrieval and cross-client isolation."""
    from apps.worker.client_rule_loader import load_client_rules
    from apps.worker.retrieval import (
        list_client_rules,
        retrieve_client_rules,
        retrieve_rule_by_entry,
    )

    test_db_url = real_services["test_db_url"]
    _seed_test_users_and_clients(test_db_url)

    # Simple deterministic 4-dim embedding
    def embed_func(text: str) -> list[float]:
        v = [0.0, 0.0, 0.0, 0.0]
        for idx, ch in enumerate(text):
            v[idx % 4] += ord(ch)
        norm = (sum(x * x for x in v) ** 0.5) or 1.0
        return [x / norm for x in v]

    # Load rules for brightleaf and oakandhome
    bl_count = load_client_rules(embed_func, client_id="brightleaf")
    oh_count = load_client_rules(embed_func, client_id="oakandhome")
    assert bl_count > 0
    assert oh_count > 0

    # Retrieve an oakandhome rule directly to get its exact vector
    oh_rules = list_client_rules("oakandhome")
    assert len(oh_rules) > 0
    target_oh_rule = oh_rules[0]
    oh_vector = embed_func(target_oh_rule.content)

    # Query for brightleaf using oakandhome's exact embedding vector
    results = retrieve_client_rules("brightleaf", oh_vector, top_k=5)

    # Multi-tenant isolation: every single returned rule MUST belong to brightleaf
    assert len(results) > 0
    assert all(r.client_id == "brightleaf" for r in results)
    assert not any(r.client_id == "oakandhome" for r in results)
    assert not any(r.entry_number.startswith("OH-") for r in results)

    # Exact rule lookup cross-client isolation
    assert retrieve_rule_by_entry("brightleaf", target_oh_rule.entry_number) is None
    assert retrieve_rule_by_entry("oakandhome", target_oh_rule.entry_number) is not None


