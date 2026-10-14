"""Unit and integration tests for Batch read API (client-scoped).

Covers:
- Authentication & Authorization:
  - 401 Unauthorized for requests without valid bearer tokens.
  - 404 Not Found for non-existent batches.
  - 403 Forbidden when user is not assigned to any client present in the batch.
- Client Scoping & Isolation:
  - Buyer assigned to client A sees only client A's batches in GET /batches.
  - On a mixed-client batch, buyer assigned to client A sees ONLY client A ads and findings.
  - Buyer assigned to client B sees ONLY client B ads and findings on the same batch.
  - Batch summary counts (total_ads, verdict_counts, client_ids) strictly reflect visible ads.
- Findings and Verdict Integration:
  - Each ad presents its associated findings from the findings table.
  - Verdict is resolved from worker_traces or dynamically via aggregate_ad_verdict.
  - Rejected rows from ingest are included in the batch detail response.
- Real PostgreSQL integration test when RUN_INTEGRATION=1.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from typing import Any

from fastapi.testclient import TestClient
import psycopg
import pytest

from apps.api import auth, batches, main
from apps.api.auth import CurrentUser, create_access_token
from apps.api.main import app
from packages.contracts import CheckType, FindingContract, VerdictStatus


# --- Mock Database for Unit Tests ---

class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class FakeBatchDatabase:
    """In-memory database storing simulated batches, ads, findings, rejected rows, and traces."""

    def __init__(self) -> None:
        # batch_id -> dict
        self.batches: dict[str, dict[str, Any]] = {}
        # ad_id -> dict
        self.ads: dict[str, dict[str, Any]] = {}
        # finding_id -> dict
        self.findings: dict[str, dict[str, Any]] = {}
        # (batch_id, data_row_index) -> dict
        self.rejected_rows: dict[tuple[str, int], dict[str, Any]] = {}
        # (batch_id, ad_id) -> dict
        self.traces: dict[tuple[str, str], dict[str, Any]] = {}

    @contextmanager
    def session(self) -> Any:
        db = self

        class Connection:
            def execute(self, statement: str, params: Any = None) -> FakeResult:
                params_tuple = tuple(params) if params else ()
                s = " ".join(statement.strip().split())

                # 1. Query batch record
                if "FROM batches WHERE batch_id = %s" in s:
                    bid = params_tuple[0]
                    b = db.batches.get(bid)
                    if b:
                        return FakeResult([(b["batch_id"], b["uploaded_by"], b["created_at"], b["filename"], b["sheet_hash"])])
                    return FakeResult([])

                # 2. Count total ads in batch
                if "SELECT COUNT(*) FROM ads WHERE batch_id = %s" in s and "AND client_id" not in s:
                    bid = params_tuple[0]
                    cnt = sum(1 for a in db.ads.values() if a["batch_id"] == bid)
                    return FakeResult([(cnt,)])

                # 3. Query ads in batch scoped to allowed clients
                if "FROM ads WHERE batch_id = %s AND client_id = ANY(%s)" in s and "COUNT" not in s:
                    bid, clients = params_tuple
                    allowed = set(clients)
                    matching = [a for a in db.ads.values() if a["batch_id"] == bid and a["client_id"] in allowed]
                    matching.sort(key=lambda a: a["row_number"])
                    return FakeResult([
                        (
                            a["ad_id"], a["batch_id"], a["client_id"], a["row_number"],
                            a["platform"], a.get("platform_raw"), a.get("ad_text"),
                            a.get("headline"), a["destination_url"], a.get("creative_file"),
                            a["row_hash"], a["sheet_hash"]
                        )
                        for a in matching
                    ])

                # 4. Query findings for ad IDs
                if "FROM findings WHERE ad_id = ANY(%s)" in s:
                    ad_ids = set(params_tuple[0])
                    matching_f = [f for f in db.findings.values() if f["ad_id"] in ad_ids]
                    matching_f.sort(key=lambda f: (f["row_number"], f["finding_id"]))
                    return FakeResult([
                        (
                            f["finding_id"], f["ad_id"], f["row_number"], f["check_type"],
                            f["message"], f.get("field"), f.get("proposed_fix"),
                            f.get("rule_id"), f.get("file_ref")
                        )
                        for f in matching_f
                    ])

                # 5. Query worker_traces for verdicts
                if "FROM worker_traces WHERE batch_id = %s AND ad_id = ANY(%s)" in s:
                    bid, ad_ids = params_tuple
                    ad_set = set(ad_ids)
                    matching_t = [
                        (aid, tr["verdict"])
                        for (b, aid), tr in db.traces.items()
                        if b == bid and aid in ad_set
                    ]
                    return FakeResult(matching_t)

                # 6. Query batch rejected rows
                if "FROM batch_rejected_rows WHERE batch_id = %s" in s:
                    bid = params_tuple[0]
                    matching_r = [r for (b, _), r in db.rejected_rows.items() if b == bid]
                    matching_r.sort(key=lambda r: r["data_row_index"])
                    return FakeResult([
                        (r["data_row_index"], r["row_number"], r["reason_code"], r["reason"], r["raw_cells"])
                        for r in matching_r
                    ])

                # 7. List user batches (with EXISTS / uploaded_by)
                if "FROM batches b" in s:
                    if "EXISTS" in s:
                        client_ids, uid = params_tuple
                        allowed = set(client_ids)
                        b_rows = []
                        for b in db.batches.values():
                            has_client = any(
                                a["client_id"] in allowed
                                for a in db.ads.values()
                                if a["batch_id"] == b["batch_id"]
                            )
                            if has_client or b["uploaded_by"] == uid:
                                b_rows.append(b)
                        b_rows.sort(key=lambda b: b["created_at"], reverse=True)
                        return FakeResult([
                            (b["batch_id"], b["filename"], b["uploaded_by"], b["created_at"], b["sheet_hash"])
                            for b in b_rows
                        ])
                    else:
                        uid = params_tuple[0]
                        b_rows = [b for b in db.batches.values() if b["uploaded_by"] == uid]
                        b_rows.sort(key=lambda b: b["created_at"], reverse=True)
                        return FakeResult([
                            (b["batch_id"], b["filename"], b["uploaded_by"], b["created_at"], b["sheet_hash"])
                            for b in b_rows
                        ])

                # 8. Count visible ads and aggregate client IDs for list_user_batches
                if "SELECT COUNT(*), COALESCE(array_agg(DISTINCT client_id)" in s:
                    bid, client_ids = params_tuple
                    allowed = set(client_ids)
                    matching_ads = [a for a in db.ads.values() if a["batch_id"] == bid and a["client_id"] in allowed]
                    c_ids = list({a["client_id"] for a in matching_ads})
                    return FakeResult([(len(matching_ads), c_ids)])

                return FakeResult([])

        yield Connection()


# --- Fixtures ---

@pytest.fixture(autouse=True)
def configured_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")


def make_auth_header(user_id: str, role: str = "buyer", client_ids: list[str] = None) -> dict[str, str]:
    token = create_access_token(user_id, role)
    return {"Authorization": f"Bearer {token}"}


# --- Unit Tests ---

def test_batches_endpoint_requires_authentication() -> None:
    with TestClient(app) as client:
        resp_list = client.get("/batches")
        assert resp_list.status_code == 401

        resp_get = client.get("/batches/some-batch-id")
        assert resp_get.status_code == 401


def test_get_batch_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeBatchDatabase()
    monkeypatch.setattr(batches, "get_session", db.session)
    monkeypatch.setattr(
        auth.users,
        "get_user_by_id",
        lambda uid: {"user_id": uid, "email": f"{uid}@example.test", "display_name": uid, "role": "buyer", "is_active": True},
    )
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda uid: ["nordic_rest"])

    headers = make_auth_header("buyer-1", "buyer")
    with TestClient(app) as client:
        resp = client.get("/batches/nonexistent-batch-id", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "batch not found"


def test_get_batch_forbidden_when_user_not_assigned_to_any_client_in_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeBatchDatabase()
    # Batch 1 contains only Lumina Skin ads
    db.batches["batch-1"] = {
        "batch_id": "batch-1",
        "uploaded_by": "other-buyer",
        "created_at": "2026-10-14T10:00:00Z",
        "filename": "lumina_ads.csv",
        "sheet_hash": "hash-lumina",
    }
    db.ads["ad-lumina-1"] = {
        "ad_id": "ad-lumina-1",
        "batch_id": "batch-1",
        "client_id": "lumina_skin",
        "row_number": 1,
        "platform": "Meta",
        "platform_raw": "Facebook",
        "ad_text": "Serum for radiant skin",
        "headline": "Glow Today",
        "destination_url": "https://luminaskin.com",
        "creative_file": "serum.png",
        "row_hash": "rh-1",
        "sheet_hash": "hash-lumina",
    }

    monkeypatch.setattr(batches, "get_session", db.session)
    # User is only assigned to Nordic Rest
    monkeypatch.setattr(
        auth.users,
        "get_user_by_id",
        lambda uid: {"user_id": uid, "email": "buyer@example.test", "display_name": "Buyer", "role": "buyer", "is_active": True},
    )
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda uid: ["nordic_rest"])

    headers = make_auth_header("buyer-nordic", "buyer")
    with TestClient(app) as client:
        resp = client.get("/batches/batch-1", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "forbidden"


def test_mixed_client_batch_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a batch contains ads for multiple clients, each buyer sees ONLY their assigned clients' ads."""
    db = FakeBatchDatabase()
    batch_id = "batch-mixed-01"
    db.batches[batch_id] = {
        "batch_id": batch_id,
        "uploaded_by": "uploader-1",
        "created_at": "2026-10-14T12:00:00Z",
        "filename": "mixed_launch.xlsx",
        "sheet_hash": "hash-mixed",
    }

    # 2 ads for nordic_rest
    db.ads["ad-nordic-1"] = {
        "ad_id": "ad-nordic-1",
        "batch_id": batch_id,
        "client_id": "nordic_rest",
        "row_number": 1,
        "platform": "Meta",
        "ad_text": "Cooling mattress",
        "headline": "Sleep Well",
        "destination_url": "https://nordicrest.com",
        "creative_file": "c1.png",
        "row_hash": "rh-n1",
        "sheet_hash": "hash-mixed",
    }
    db.ads["ad-nordic-2"] = {
        "ad_id": "ad-nordic-2",
        "batch_id": batch_id,
        "client_id": "nordic_rest",
        "row_number": 2,
        "platform": "Meta",
        "ad_text": "Organic pillows",
        "headline": "Rest Easy",
        "destination_url": "https://nordicrest.com",
        "creative_file": "c2.png",
        "row_hash": "rh-n2",
        "sheet_hash": "hash-mixed",
    }

    # 1 ad for lumina_skin
    db.ads["ad-lumina-1"] = {
        "ad_id": "ad-lumina-1",
        "batch_id": batch_id,
        "client_id": "lumina_skin",
        "row_number": 3,
        "platform": "Google",
        "ad_text": "Hydrating face cream",
        "headline": "Skin Care",
        "destination_url": "https://luminaskin.com",
        "creative_file": "c3.png",
        "row_hash": "rh-l1",
        "sheet_hash": "hash-mixed",
    }

    # Finding on nordic-2
    db.findings["f-nordic-2"] = {
        "finding_id": "f-nordic-2",
        "ad_id": "ad-nordic-2",
        "row_number": 2,
        "check_type": "mechanical",
        "message": "UTM parameter missing",
        "field": "destination_url",
        "proposed_fix": "Add utm_source",
        "rule_id": "utm_presence",
    }

    # Finding on lumina-1
    db.findings["f-lumina-1"] = {
        "finding_id": "f-lumina-1",
        "ad_id": "ad-lumina-1",
        "row_number": 3,
        "check_type": "mechanical",
        "message": "Headline exceeds limit",
        "field": "headline",
        "proposed_fix": "Shorten headline",
        "rule_id": "text_limits",
    }

    # Trace for nordic-1: ready
    db.traces[(batch_id, "ad-nordic-1")] = {"verdict": "ready", "status": "completed"}

    monkeypatch.setattr(batches, "get_session", db.session)

    # 1. Buyer 1 assigned ONLY to nordic_rest
    monkeypatch.setattr(
        auth.users,
        "get_user_by_id",
        lambda uid: {"user_id": uid, "email": "nordic@test.com", "display_name": "Nordic Buyer", "role": "buyer", "is_active": True},
    )
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda uid: ["nordic_rest"])

    headers_nordic = make_auth_header("buyer-nordic", "buyer")
    with TestClient(app) as client:
        resp_nordic = client.get(f"/batches/{batch_id}", headers=headers_nordic)

    assert resp_nordic.status_code == 200
    data_nordic = resp_nordic.json()
    assert data_nordic["batch_id"] == batch_id
    assert data_nordic["summary"]["total_ads"] == 2
    assert data_nordic["summary"]["client_ids"] == ["nordic_rest"]
    assert len(data_nordic["ads"]) == 2
    assert all(a["client_id"] == "nordic_rest" for a in data_nordic["ads"])
    # Nordic ad 1 has trace verdict 'ready'
    assert data_nordic["ads"][0]["verdict"] == "ready"
    assert len(data_nordic["ads"][0]["findings"]) == 0
    # Nordic ad 2 has mechanical finding -> fix needed
    assert data_nordic["ads"][1]["verdict"] == "fix needed"
    assert len(data_nordic["ads"][1]["findings"]) == 1
    assert data_nordic["ads"][1]["findings"][0]["finding_id"] == "f-nordic-2"

    # 2. Buyer 2 assigned ONLY to lumina_skin
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda uid: ["lumina_skin"])
    headers_lumina = make_auth_header("buyer-lumina", "buyer")
    with TestClient(app) as client:
        resp_lumina = client.get(f"/batches/{batch_id}", headers=headers_lumina)

    assert resp_lumina.status_code == 200
    data_lumina = resp_lumina.json()
    assert data_lumina["summary"]["total_ads"] == 1
    assert data_lumina["summary"]["client_ids"] == ["lumina_skin"]
    assert len(data_lumina["ads"]) == 1
    assert data_lumina["ads"][0]["client_id"] == "lumina_skin"
    assert data_lumina["ads"][0]["verdict"] == "fix needed"
    assert len(data_lumina["ads"][0]["findings"]) == 1
    assert data_lumina["ads"][0]["findings"][0]["finding_id"] == "f-lumina-1"


def test_list_batches_client_scoping(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeBatchDatabase()
    # Batch 1: Nordic Rest only
    db.batches["b1"] = {
        "batch_id": "b1",
        "uploaded_by": "u1",
        "created_at": "2026-10-14T10:00:00Z",
        "filename": "nordic.csv",
        "sheet_hash": "sh1",
    }
    db.ads["a1"] = {"ad_id": "a1", "batch_id": "b1", "client_id": "nordic_rest", "row_number": 1, "platform": "Meta", "destination_url": "https://n.com", "row_hash": "r1", "sheet_hash": "sh1"}

    # Batch 2: Lumina Skin only
    db.batches["b2"] = {
        "batch_id": "b2",
        "uploaded_by": "u2",
        "created_at": "2026-10-14T11:00:00Z",
        "filename": "lumina.csv",
        "sheet_hash": "sh2",
    }
    db.ads["a2"] = {"ad_id": "a2", "batch_id": "b2", "client_id": "lumina_skin", "row_number": 1, "platform": "Meta", "destination_url": "https://l.com", "row_hash": "r2", "sheet_hash": "sh2"}

    monkeypatch.setattr(batches, "get_session", db.session)
    monkeypatch.setattr(
        auth.users,
        "get_user_by_id",
        lambda uid: {"user_id": uid, "email": "test@test.com", "display_name": "User", "role": "buyer", "is_active": True},
    )

    # Buyer assigned to nordic_rest only
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda uid: ["nordic_rest"])
    headers = make_auth_header("buyer-nordic", "buyer")

    with TestClient(app) as client:
        resp = client.get("/batches", headers=headers)

    assert resp.status_code == 200
    batch_list = resp.json()
    assert len(batch_list) == 1
    assert batch_list[0]["batch_id"] == "b1"
    assert batch_list[0]["client_ids"] == ["nordic_rest"]
    assert batch_list[0]["total_ads"] == 1


def test_get_batch_includes_rejected_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeBatchDatabase()
    batch_id = "batch-with-rejected"
    db.batches[batch_id] = {
        "batch_id": batch_id,
        "uploaded_by": "buyer-1",
        "created_at": "2026-10-14T10:00:00Z",
        "filename": "upload.csv",
        "sheet_hash": "sh-rej",
    }
    db.ads["ad-1"] = {
        "ad_id": "ad-1",
        "batch_id": batch_id,
        "client_id": "nordic_rest",
        "row_number": 1,
        "platform": "Meta",
        "destination_url": "https://nordicrest.com",
        "row_hash": "rh-1",
        "sheet_hash": "sh-rej",
    }
    db.rejected_rows[(batch_id, 2)] = {
        "batch_id": batch_id,
        "data_row_index": 2,
        "row_number": 2,
        "reason_code": "malformed_row",
        "reason": "Missing destination URL",
        "raw_cells": ["Google", "Headline", "", ""],
    }

    monkeypatch.setattr(batches, "get_session", db.session)
    monkeypatch.setattr(
        auth.users,
        "get_user_by_id",
        lambda uid: {"user_id": uid, "email": "b1@test.com", "display_name": "B1", "role": "buyer", "is_active": True},
    )
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda uid: ["nordic_rest"])

    headers = make_auth_header("buyer-1", "buyer")
    with TestClient(app) as client:
        resp = client.get(f"/batches/{batch_id}", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["rejected_count"] == 1
    assert len(data["rejected_rows"]) == 1
    assert data["rejected_rows"][0]["reason_code"] == "malformed_row"


# --- Real PostgreSQL Integration Test (Opt-in) ---

@pytest.mark.integration
def test_batches_read_api_real_db(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION is not 1; skipping real PostgreSQL batch API integration test")

    test_db_url = os.environ.get("TEST_DATABASE_URL", "")
    if not test_db_url:
        pytest.fail("TEST_DATABASE_URL must be configured")

    with psycopg.connect(test_db_url, autocommit=True) as conn:
        # Create schema
        batches.create_batches_table()
        from apps.api.ads import create_ads_table
        from apps.api.findings import create_findings_table
        from apps.api.users import create_users_table, create_user, assign_user_to_client
        create_users_table()
        create_ads_table()
        create_findings_table()

        # Seed client, user, batch, ad, finding
        conn.execute("INSERT INTO clients (client_id, name) VALUES ('nordic_rest', 'Nordic Rest') ON CONFLICT DO NOTHING;")
        create_user(user_id="test_buyer_int", email="buyer_int@example.com", display_name="Int Buyer", password_hash="dummy", role="buyer")
        assign_user_to_client("test_buyer_int", "nordic_rest")

        bid = "int-batch-001"
        batches.create_batch_on_connection(conn, batch_id=bid, uploaded_by="test_buyer_int", filename="int_test.csv", sheet_hash="hash-int-001")
        aid = "int-ad-001"
        conn.execute(
            """INSERT INTO ads (ad_id, batch_id, client_id, row_number, platform, destination_url, row_hash, sheet_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING;""",
            (aid, bid, "nordic_rest", 1, "Meta", "https://nordicrest.com", "rh", "hash-int-001")
        )
        conn.execute(
            """INSERT INTO findings (finding_id, ad_id, row_number, check_type, message, field)
               VALUES ('int-f-001', %s, 1, 'mechanical', 'Missing UTM parameter', 'destination_url') ON CONFLICT DO NOTHING;""",
            (aid,)
        )

        headers = make_auth_header("test_buyer_int", "buyer")
        with TestClient(app) as client:
            resp = client.get(f"/batches/{bid}", headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["batch_id"] == bid
        assert body["summary"]["total_ads"] == 1
        assert body["ads"][0]["ad_id"] == aid
        assert body["ads"][0]["findings"][0]["finding_id"] == "int-f-001"
