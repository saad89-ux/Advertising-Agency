"""Regression tests for tenant isolation and empty-batch security (LG-001).

Proves:
1. A client can access only authorized data.
2. An empty batch returns an empty result, not another client's records.
3. A nonexistent batch does not expose data (returns 404).
4. An unauthorized cross-tenant request is rejected (returns 403).
5. Existing valid same-tenant behavior remains intact.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from apps.api import auth, batches, main
from apps.api.auth import CurrentUser, create_access_token
from apps.api.batches import get_batch_ads, get_batch_detail
from apps.api.main import app


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeTenantIsolationDB:
    """Mock database with distinct clients and batches to test tenant isolation."""

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.batches = {
            "batch-brightleaf-valid": {
                "batch_id": "batch-brightleaf-valid",
                "uploaded_by": "buyer-brightleaf",
                "created_at": now,
                "filename": "brightleaf_ads.csv",
                "sheet_hash": "hash-bl-valid",
            },
            "batch-oakandhome-valid": {
                "batch_id": "batch-oakandhome-valid",
                "uploaded_by": "buyer-oakandhome",
                "created_at": now,
                "filename": "oakandhome_ads.csv",
                "sheet_hash": "hash-oh-valid",
            },
            "batch-empty-brightleaf": {
                "batch_id": "batch-empty-brightleaf",
                "uploaded_by": "buyer-brightleaf",
                "created_at": now,
                "filename": "empty_bl.csv",
                "sheet_hash": "hash-empty-bl",
            },
        }

        self.ads = {
            "ad-bl-1": {
                "ad_id": "ad-bl-1",
                "batch_id": "batch-brightleaf-valid",
                "client_id": "brightleaf",
                "row_number": 1,
                "platform": "facebook",
                "platform_raw": "Facebook",
                "ad_text": "Organic Green Tea for calm focus.",
                "headline": "Pure Botanicals",
                "destination_url": "https://brightleaf.example/tea",
                "creative_file": "tea.png",
                "row_hash": "rh-bl-1",
                "sheet_hash": "hash-bl-valid",
            },
            "ad-oh-1": {
                "ad_id": "ad-oh-1",
                "batch_id": "batch-oakandhome-valid",
                "client_id": "oakandhome",
                "row_number": 1,
                "platform": "instagram",
                "platform_raw": "Instagram",
                "ad_text": "Handcrafted Oak dining tables.",
                "headline": "Solid Oak Design",
                "destination_url": "https://oakandhome.example/tables",
                "creative_file": "table.png",
                "row_hash": "rh-oh-1",
                "sheet_hash": "hash-oh-valid",
            },
        }

    @contextmanager
    def session(self) -> Any:
        db = self

        class Connection:
            def execute(self, statement: str, params: Any = None) -> FakeCursor:
                params_tuple = tuple(params) if params else ()
                s = " ".join(statement.strip().split())

                # 1. Query batch record
                if "FROM batches WHERE batch_id = %s" in s:
                    bid = params_tuple[0]
                    b = db.batches.get(bid)
                    if b:
                        return FakeCursor([(b["batch_id"], b["uploaded_by"], b["created_at"], b["filename"], b["sheet_hash"])])
                    return FakeCursor([])

                # 2. Count total ads in batch
                if "SELECT COUNT(*) FROM ads WHERE batch_id = %s" in s and "client_id" not in s:
                    bid = params_tuple[0]
                    count = sum(1 for a in db.ads.values() if a["batch_id"] == bid)
                    return FakeCursor([(count,)])

                # 3. Query ads in batch scoped strictly to allowed clients
                if "FROM ads WHERE batch_id = %s AND client_id = ANY(%s)" in s:
                    bid, clients = params_tuple
                    allowed = set(clients)
                    matching = [
                        a for a in db.ads.values()
                        if a["batch_id"] == bid and a["client_id"] in allowed
                    ]
                    matching.sort(key=lambda a: a["row_number"])
                    return FakeCursor([
                        (
                            a["ad_id"], a["batch_id"], a["client_id"], a["row_number"],
                            a["platform"], a.get("platform_raw"), a.get("ad_text"),
                            a.get("headline"), a["destination_url"], a.get("creative_file"),
                            a["row_hash"], a["sheet_hash"],
                        )
                        for a in matching
                    ])

                # 4. Insecure fallback query: SELECT ... FROM ads WHERE client_id = %s
                if "FROM ads WHERE client_id = %s" in s:
                    bid, cid = params_tuple[0], params_tuple[1]
                    matching = [a for a in db.ads.values() if a["client_id"] == cid]
                    matching.sort(key=lambda a: a["row_number"])
                    return FakeCursor([
                        (
                            a["ad_id"], bid, a["client_id"], a["row_number"],
                            a["platform"], a.get("platform_raw"), a.get("ad_text"),
                            a.get("headline"), a["destination_url"], a.get("creative_file"),
                            a["row_hash"], a["sheet_hash"],
                        )
                        for a in matching
                    ])

                # 5. Findings
                if "FROM findings WHERE ad_id = ANY(%s)" in s:
                    return FakeCursor([])

                # 6. Worker traces
                if "FROM worker_traces" in s:
                    return FakeCursor([])

                # 7. Approvals
                if "FROM ad_approvals" in s:
                    return FakeCursor([])

                # 8. Tracking / dispatches
                if "FROM ad_performance_tracking" in s or "FROM ad_live_dispatches" in s:
                    return FakeCursor([])

                return FakeCursor([])

        yield Connection()


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")


def make_user(uid: str, role: str, client_ids: list[str]) -> CurrentUser:
    return CurrentUser(
        user_id=uid,
        email=f"{uid}@example.com",
        display_name=uid,
        role=role,
        assigned_client_ids=tuple(client_ids),
    )


# ---------------------------------------------------------------------------
# Test 1: A client can access only authorized data
# ---------------------------------------------------------------------------
def test_client_accesses_only_authorized_data():
    db = FakeTenantIsolationDB()
    with db.session() as conn:
        # Brightleaf buyer querying brightleaf batch receives brightleaf ads
        bl_ads = get_batch_ads(conn, "batch-brightleaf-valid", ["brightleaf"])
        assert len(bl_ads) == 1
        assert bl_ads[0]["client_id"] == "brightleaf"
        assert bl_ads[0]["ad_id"] == "ad-bl-1"

        # Oak & Home buyer querying oakandhome batch receives oakandhome ads
        oh_ads = get_batch_ads(conn, "batch-oakandhome-valid", ["oakandhome"])
        assert len(oh_ads) == 1
        assert oh_ads[0]["client_id"] == "oakandhome"
        assert oh_ads[0]["ad_id"] == "ad-oh-1"


# ---------------------------------------------------------------------------
# Test 2: An empty batch returns an empty result, NOT another client's records
# ---------------------------------------------------------------------------
def test_empty_batch_returns_empty_list_never_leaks_other_client_records():
    db = FakeTenantIsolationDB()
    with db.session() as conn:
        # Querying an empty batch for brightleaf MUST return an empty list
        # It must NEVER fall back to returning oakandhome ads!
        ads = get_batch_ads(conn, "batch-empty-brightleaf", ["brightleaf"])
        assert ads == [], f"Expected empty list for empty batch, but got: {ads}"


# ---------------------------------------------------------------------------
# Test 3: A nonexistent batch does not expose data (returns 404)
# ---------------------------------------------------------------------------
def test_nonexistent_batch_raises_404(monkeypatch: pytest.MonkeyPatch):
    db = FakeTenantIsolationDB()
    monkeypatch.setattr(batches, "get_session", db.session)
    user = make_user("buyer-brightleaf", "buyer", ["brightleaf"])

    with pytest.raises(HTTPException) as exc_info:
        get_batch_detail("nonexistent-batch-id-999", user)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "batch not found"


# ---------------------------------------------------------------------------
# Test 4: An unauthorized cross-tenant request is rejected (returns 403)
# ---------------------------------------------------------------------------
def test_unauthorized_cross_tenant_request_rejected_403(monkeypatch: pytest.MonkeyPatch):
    db = FakeTenantIsolationDB()
    monkeypatch.setattr(batches, "get_session", db.session)

    # Brightleaf buyer attempting to inspect Oak & Home's batch
    bl_user = make_user("buyer-brightleaf", "buyer", ["brightleaf"])

    with pytest.raises(HTTPException) as exc_info:
        get_batch_detail("batch-oakandhome-valid", bl_user)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "forbidden"


# ---------------------------------------------------------------------------
# Test 5: Existing valid same-tenant behavior remains intact
# ---------------------------------------------------------------------------
def test_valid_same_tenant_batch_detail_retrieval(monkeypatch: pytest.MonkeyPatch):
    db = FakeTenantIsolationDB()
    monkeypatch.setattr(batches, "get_session", db.session)

    bl_user = make_user("buyer-brightleaf", "buyer", ["brightleaf"])
    detail = get_batch_detail("batch-brightleaf-valid", bl_user)

    assert detail["batch_id"] == "batch-brightleaf-valid"
    assert len(detail["ads"]) == 1
    assert detail["ads"][0]["client_id"] == "brightleaf"
    assert detail["ads"][0]["ad_id"] == "ad-bl-1"
    assert detail["summary"]["total_ads"] == 1
    assert "oakandhome" not in detail["summary"]["client_ids"]
