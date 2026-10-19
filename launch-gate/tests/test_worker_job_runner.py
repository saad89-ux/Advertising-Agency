"""Unit tests for worker job runner using fake database connection and fake Redis."""

from __future__ import annotations

from datetime import date, timedelta
import json
from typing import Any

import pytest

from apps.api.queue import AD_JOB_QUEUE, AdJob
from apps.worker import job_runner, page_text_capture, utm_redirect_check
from apps.worker.file_storage import LocalFileStore
from apps.worker.job_runner import (
    WorkerJob,
    clear_latest_shared_state,
    clear_recorded_failures,
    get_latest_shared_state,
    get_recorded_failures,
    process_job,
    run_once,
)
from apps.worker.page_browser import OpenedPage, PageOpenError
from packages.contracts import CheckType


def stub_browser(monkeypatch, opener):
    monkeypatch.setattr(utm_redirect_check, "open_page_read_only", opener)
    monkeypatch.setattr(page_text_capture, "open_page_read_only", opener)



class FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self._index = 0

    def fetchone(self) -> Any | None:
        if self._index < len(self.rows):
            row = self.rows[self._index]
            self._index += 1
            return row
        return None

    def fetchall(self) -> list[Any]:
        remaining = self.rows[self._index:]
        self._index = len(self.rows)
        return remaining


class FakeConnection:
    """In-memory connection supporting ad queries and finding inserts with conflict handling."""

    def __init__(self, ads: list[dict[str, Any]] | None = None) -> None:
        self.ads = ads or []
        self.findings: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []

    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> FakeCursor:
        self.calls.append((statement, params))
        normalized = " ".join(statement.split())

        # 1. SELECT ads WHERE ad_id = %s AND client_id = %s
        if "FROM ads WHERE ad_id = %s AND client_id = %s" in normalized:
            ad_id, client_id = params[0], params[1]
            matches = [
                (
                    ad["ad_id"],
                    ad["batch_id"],
                    ad["client_id"],
                    ad["row_number"],
                    ad["platform"],
                    ad.get("ad_text"),
                    ad.get("headline"),
                    ad["destination_url"],
                    ad.get("creative_file"),
                    ad.get("row_hash", "h_row"),
                    ad.get("sheet_hash", "h_sheet"),
                )
                for ad in self.ads
                if ad["ad_id"] == ad_id and ad["client_id"] == client_id
            ]
            return FakeCursor(matches)

        # 2. SELECT ads WHERE ad_id = %s
        if "FROM ads WHERE ad_id = %s" in normalized and "SELECT 1" not in normalized and "SELECT client_id" not in normalized:
            ad_id = params[0]
            matches = [
                (
                    ad["ad_id"],
                    ad["batch_id"],
                    ad["client_id"],
                    ad["row_number"],
                    ad["platform"],
                    ad.get("ad_text"),
                    ad.get("headline"),
                    ad["destination_url"],
                    ad.get("creative_file"),
                    ad.get("row_hash", "h_row"),
                    ad.get("sheet_hash", "h_sheet"),
                )
                for ad in self.ads
                if ad["ad_id"] == ad_id
            ]
            return FakeCursor(matches)

        # 3. SELECT client_id FROM ads WHERE ad_id = %s
        if "SELECT client_id FROM ads WHERE ad_id = %s" in normalized:
            ad_id = params[0]
            matches = [(ad["client_id"],) for ad in self.ads if ad["ad_id"] == ad_id]
            return FakeCursor(matches)

        # 4. SELECT 1 FROM ads WHERE ad_id = %s
        if "SELECT 1 FROM ads WHERE ad_id = %s" in normalized:
            ad_id = params[0]
            matches = [(1,) for ad in self.ads if ad["ad_id"] == ad_id]
            return FakeCursor(matches)

        # 5. INSERT INTO findings ... ON CONFLICT (finding_id) DO NOTHING
        if "INSERT INTO findings" in normalized:
            finding_id = params[0]
            if finding_id not in self.findings:
                self.findings[finding_id] = {
                    "finding_id": params[0],
                    "ad_id": params[1],
                    "row_number": params[2],
                    "check_type": params[3],
                    "message": params[4],
                    "field": params[5],
                    "proposed_fix": params[6],
                    "rule_id": params[7],
                    "file_ref": params[8],
                }
            return FakeCursor([])

        return FakeCursor([])


class FakeRedis:
    """In-memory Redis list implementation for AD_JOB_QUEUE."""

    def __init__(self, initial_items: list[str] | None = None) -> None:
        self.items: list[str] = list(initial_items or [])

    def rpush(self, key: str, *values: str) -> int:
        assert key == AD_JOB_QUEUE
        self.items.extend(values)
        return len(self.items)

    def lpop(self, key: str) -> str | None:
        assert key == AD_JOB_QUEUE
        return self.items.pop(0) if self.items else None

    def lpush(self, key: str, *values: str) -> int:
        assert key == AD_JOB_QUEUE
        for val in reversed(values):
            self.items.insert(0, val)
        return len(self.items)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def clean_state():
    clear_recorded_failures()
    clear_latest_shared_state()
    yield
    clear_recorded_failures()
    clear_latest_shared_state()


def sample_manifest() -> dict[str, dict[str, int]]:
    return {
        "square.png": {"width": 1080, "height": 1080},
        "bad_ratio.png": {"width": 1000, "height": 500},
    }


def sample_platform_limits() -> dict[str, Any]:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return {
        "stale_if_last_verified_before": "2024-01-01",
        "platforms": {
            "facebook": {
                "text_max": {"value": 125, "last_verified": tomorrow},
                "headline_max": {"value": 40, "last_verified": tomorrow},
                "ratios": {"value": ["1:1", "4:5"], "last_verified": tomorrow},
            }
        },
    }


# =========================================================================
# W1 Tests: Mechanical checks isolation
# =========================================================================


def test_process_job_one_job_expected_findings():
    """One job with violations produces expected findings with field and proposed fix."""
    bad_ad = {
        "ad_id": "ad-bad-1",
        "batch_id": "batch-1",
        "client_id": "client-1",
        "row_number": 1,
        "platform": "facebook",
        "ad_text": "This ad text is far too long for Facebook limits " * 5,  # > 125 chars
        "headline": "Short headline",
        "destination_url": "https://example.com/landing?utm_medium=paid_social&utm_campaign=brand_jan",  # missing utm_source
        "creative_file": "bad_ratio.png",  # 2:1 ratio, facebook allows 1:1, 4:5
    }
    conn = FakeConnection(ads=[bad_ad])
    job = AdJob(batch_id="batch-1", ad_id="ad-bad-1")

    findings = process_job(
        job,
        connection=conn,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        run_page_checks=False,
    )

    assert findings is not None
    assert len(findings) >= 3

    finding_fields = {f.field for f in findings}
    assert "utm_source" in finding_fields
    assert "ad_text" in finding_fields
    assert "creative_file" in finding_fields

    # All findings must specify row_number, field, proposed_fix
    for f in findings:
        assert f.row_number == 1
        assert f.field is not None
        assert f.proposed_fix is not None

    # Persisted in connection
    assert len(conn.findings) == len(findings)
    assert "ad-bad-1:utm_presence:utm_source" in conn.findings
    assert "ad-bad-1:platform_limit:ad_text" in conn.findings
    assert "ad-bad-1:creative:ratio" in conn.findings


def test_process_job_same_job_twice_no_duplicates():
    """Idempotency: running the same job twice does not create duplicate findings."""
    ad = {
        "ad_id": "ad-bad-2",
        "batch_id": "batch-1",
        "client_id": "client-1",
        "row_number": 2,
        "platform": "facebook",
        "ad_text": "Normal text",
        "headline": "A headline that is excessively long and will breach limits " * 2,
        "destination_url": "https://example.com/landing",  # missing all utms
        "creative_file": "square.png",
    }
    conn = FakeConnection(ads=[ad])
    job = AdJob(batch_id="batch-1", ad_id="ad-bad-2")
    manifest = sample_manifest()
    limits = sample_platform_limits()

    # First run
    findings_1 = process_job(job, connection=conn, manifest=manifest, platform_limits=limits, run_page_checks=False)
    count_1 = len(conn.findings)
    assert count_1 > 0

    # Second run
    findings_2 = process_job(job, connection=conn, manifest=manifest, platform_limits=limits, run_page_checks=False)
    count_2 = len(conn.findings)

    assert len(findings_1) == len(findings_2)
    assert count_1 == count_2, "Findings table must not contain duplicate findings"


def test_run_once_crash_mid_job_leaves_job_retryable(monkeypatch):
    """Crash mid-job leaves the job retryable in Redis and records a failure."""
    ad_id = "ad-crash-1"
    batch_id = "batch-1"
    raw_job = json.dumps({"batch_id": batch_id, "ad_id": ad_id})
    redis_client = FakeRedis([raw_job])

    def crashing_process_job(*args, **kwargs):
        raise RuntimeError("Database connection severed mid-job")

    monkeypatch.setattr(job_runner, "process_job", crashing_process_job)

    result = run_once(redis_client=redis_client)
    assert result is None

    # Job was put back into Redis via lpush and remains retryable
    assert len(redis_client.items) == 1
    assert redis_client.items[0] == raw_job

    # Failure was recorded
    failures = get_recorded_failures()
    assert len(failures) == 1
    assert "Database connection severed mid-job" in (failures[0].error or "")


def test_process_job_client_id_mismatch_skipped():
    """Client ID mismatch skips processing, saves zero findings, and records failure."""
    ad = {
        "ad_id": "ad-client-1",
        "batch_id": "batch-1",
        "client_id": "client-alpha",
        "row_number": 1,
        "platform": "facebook",
        "ad_text": "Short ad",
        "headline": "Promo",
        "destination_url": "https://example.com/p",
        "creative_file": "square.png",
    }
    conn = FakeConnection(ads=[ad])
    # Job expects client-beta, but ad belongs to client-alpha
    job = WorkerJob(batch_id="batch-1", ad_id="ad-client-1", client_id="client-beta")

    findings = process_job(
        job,
        connection=conn,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        run_page_checks=False,
    )

    assert findings is None
    assert len(conn.findings) == 0, "No findings should be saved on client mismatch"

    failures = get_recorded_failures()
    assert len(failures) == 1
    assert "client_id mismatch" in failures[0].reason
    assert "client-beta" in failures[0].reason


def test_process_job_clean_ad_zero_findings():
    """Clean ad generates zero findings."""
    clean_ad = {
        "ad_id": "ad-clean-1",
        "batch_id": "batch-1",
        "client_id": "client-1",
        "row_number": 5,
        "platform": "facebook",
        "ad_text": "Valid Facebook ad text.",
        "headline": "Valid Headline",
        "destination_url": "https://example.com/path?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan",
        "creative_file": "square.png",  # 1080x1080 -> 1:1 ratio matches facebook allowed
    }
    conn = FakeConnection(ads=[clean_ad])
    job = AdJob(batch_id="batch-1", ad_id="ad-clean-1")

    findings = process_job(
        job,
        connection=conn,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        run_page_checks=False,
    )

    assert findings == []
    assert len(conn.findings) == 0


# =========================================================================
# W2 Tests: Page checks (redirects, offer comparison, storage, timeout)
# =========================================================================


def test_page_loads_and_offer_matches_no_finding(monkeypatch, tmp_path):
    """Page loads and offer matches -> no page findings produced."""
    ad = {
        "ad_id": "ad-page-clean",
        "batch_id": "batch-1",
        "client_id": "brightleaf",
        "row_number": 1,
        "platform": "facebook",
        "ad_text": "Enjoy 20% off all shoes today.",
        "headline": "Promo",
        "destination_url": "https://brightleaf.example/promo?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan",
        "creative_file": "square.png",
    }
    conn = FakeConnection(ads=[ad])
    job = AdJob(batch_id="batch-1", ad_id="ad-page-clean")

    async def fake_open(url, **kwargs):
        return OpenedPage(
            final_url="https://brightleaf.example/promo?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan",
            status=200,
            title="Brightleaf Promo",
            screenshot_bytes=b"fake-screenshot-bytes",
            visible_text="Enjoy 20% off all shoes today. Explore our latest arrivals.",
            hidden_text="",
        )

    stub_browser(monkeypatch, fake_open)
    store = LocalFileStore(tmp_path / "storage")

    findings = process_job(
        job,
        connection=conn,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        file_store=store,
        allowed_origins={"https://brightleaf.example"},
    )

    assert findings == []
    assert len(conn.findings) == 0

    state = get_latest_shared_state("ad-page-clean")
    assert state is not None
    assert state.status in ("page_check_completed", "ready")
    assert len(state.file_refs) == 2  # screenshot_ref and text_ref
    for ref in state.file_refs:
        assert store.get(ref) is not None


def test_expired_offer_produces_page_finding(monkeypatch, tmp_path):
    """Expired offer on the page produces a page finding."""
    ad = {
        "ad_id": "ad-page-expired",
        "batch_id": "batch-1",
        "client_id": "brightleaf",
        "row_number": 2,
        "platform": "facebook",
        "ad_text": "Great deals this season.",
        "headline": "Sale",
        "destination_url": "https://brightleaf.example/sale?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan",
        "creative_file": "square.png",
    }
    conn = FakeConnection(ads=[ad])
    job = AdJob(batch_id="batch-1", ad_id="ad-page-expired")

    async def fake_open(url, **kwargs):
        return OpenedPage(
            final_url="https://brightleaf.example/sale?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan",
            status=200,
            title="Sale",
            screenshot_bytes=b"fake-screenshot",
            visible_text="Sorry, the sale has ended. All offers are expired.",
            hidden_text="",
        )

    stub_browser(monkeypatch, fake_open)
    store = LocalFileStore(tmp_path / "storage")

    findings = process_job(
        job,
        connection=conn,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        file_store=store,
        allowed_origins={"https://brightleaf.example"},
        as_of=date(2026, 10, 11),
    )

    assert findings is not None
    page_findings = [f for f in findings if f.check_type == CheckType.PAGE]
    assert len(page_findings) >= 1
    assert any(":expired:offer" in f.finding_id for f in page_findings)


def test_dropped_utm_produces_page_finding(monkeypatch, tmp_path):
    """Dropped UTM parameter during redirect produces a page finding."""
    ad = {
        "ad_id": "ad-page-utm",
        "batch_id": "batch-1",
        "client_id": "brightleaf",
        "row_number": 3,
        "platform": "facebook",
        "ad_text": "Shop our catalog.",
        "headline": "Catalog",
        "destination_url": "https://brightleaf.example/redirect?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan",
        "creative_file": "square.png",
    }
    conn = FakeConnection(ads=[ad])
    job = AdJob(batch_id="batch-1", ad_id="ad-page-utm")

    async def fake_open(url, **kwargs):
        # Redirect dropped utm_campaign
        return OpenedPage(
            final_url="https://brightleaf.example/landing?utm_source=facebook&utm_medium=paid_social",
            status=200,
            title="Landing",
            screenshot_bytes=b"fake-screenshot",
            visible_text="Shop our catalog with great prices and quality.",
            hidden_text="",
        )

    stub_browser(monkeypatch, fake_open)
    store = LocalFileStore(tmp_path / "storage")

    findings = process_job(
        job,
        connection=conn,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        file_store=store,
        allowed_origins={"https://brightleaf.example"},
    )

    assert findings is not None
    page_findings = [f for f in findings if f.check_type == CheckType.PAGE]
    assert len(page_findings) >= 1
    assert any(f.field == "utm_campaign" and ":dropped:utm_campaign" in f.finding_id for f in page_findings)


def test_page_timeout_produces_finding_and_job_finishes(monkeypatch, tmp_path):
    """Page timeout produces a finding and the job still completes successfully."""
    ad = {
        "ad_id": "ad-page-timeout",
        "batch_id": "batch-1",
        "client_id": "brightleaf",
        "row_number": 4,
        "platform": "facebook",
        "ad_text": "Fast shipping guaranteed.",
        "headline": "Fast",
        "destination_url": "https://brightleaf.example/slow?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan",
        "creative_file": "square.png",
    }
    conn = FakeConnection(ads=[ad])
    job = AdJob(batch_id="batch-1", ad_id="ad-page-timeout")

    async def fake_open_timeout(url, **kwargs):
        raise PageOpenError("page_timeout", "Page did not load within 5000 ms")

    stub_browser(monkeypatch, fake_open_timeout)
    store = LocalFileStore(tmp_path / "storage")

    raw_job = json.dumps({"batch_id": "batch-1", "ad_id": "ad-page-timeout"})
    redis_client = FakeRedis([raw_job])

    # run_once should process the job without raising an unhandled crash
    processed = run_once(
        redis_client=redis_client,
        connection=conn,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        file_store=store,
        allowed_origins={"https://brightleaf.example"},
    )

    assert processed is not None
    assert processed.ad_id == "ad-page-timeout"
    assert len(redis_client.items) == 0  # acknowledged and not requeued

    # Finding was saved
    assert len(conn.findings) == 1
    finding_id = next(iter(conn.findings))
    assert "page_load:page_timeout" in finding_id
    assert conn.findings[finding_id]["check_type"] == "page"


@pytest.mark.parametrize("blocked_host", ["127.0.0.1", "169.254.169.254"])
def test_redirect_to_non_allowlisted_host_blocked(blocked_host, tmp_path):
    """Redirect to non-allowlisted host (including raw IPs) is blocked and produces finding."""
    ad = {
        "ad_id": f"ad-blocked-{blocked_host}",
        "batch_id": "batch-1",
        "client_id": "brightleaf",
        "row_number": 5,
        "platform": "facebook",
        "ad_text": "Security test.",
        "headline": "Secure",
        "destination_url": f"http://{blocked_host}/latest/meta-data",
        "creative_file": "square.png",
    }
    conn = FakeConnection(ads=[ad])
    job = AdJob(batch_id="batch-1", ad_id=f"ad-blocked-{blocked_host}")
    store = LocalFileStore(tmp_path / "storage")

    # Real open_page_read_only input validation immediately blocks raw IPs
    findings = process_job(
        job,
        connection=conn,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        file_store=store,
        allowed_origins={"https://brightleaf.example"},
    )

    assert findings is not None
    page_findings = [f for f in findings if f.check_type == CheckType.PAGE]
    assert len(page_findings) == 1
    assert ":page_load:host_not_allowed" in page_findings[0].finding_id


def test_stored_files_confined_to_storage(monkeypatch, tmp_path):
    """Stored screenshot and text files are strictly confined to the storage root."""
    storage_root = tmp_path / "custom_storage"
    store = LocalFileStore(storage_root)

    ad = {
        "ad_id": "ad-storage-confinement",
        "batch_id": "batch-1",
        "client_id": "brightleaf",
        "row_number": 6,
        "platform": "facebook",
        "ad_text": "Confinement test copy.",
        "headline": "Heading",
        "destination_url": "https://brightleaf.example/store?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan",
        "creative_file": "square.png",
    }
    conn = FakeConnection(ads=[ad])
    job = AdJob(batch_id="batch-1", ad_id="ad-storage-confinement")

    async def fake_open(url, **kwargs):
        return OpenedPage(
            final_url="https://brightleaf.example/store?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_jan",
            status=200,
            title="Store",
            screenshot_bytes=b"sample-screenshot",
            visible_text="Confinement test copy. Valid content on page.",
            hidden_text="",
        )

    stub_browser(monkeypatch, fake_open)

    process_job(
        job,
        connection=conn,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        file_store=store,
        allowed_origins={"https://brightleaf.example"},
    )

    state = get_latest_shared_state("ad-storage-confinement")
    assert state is not None
    assert len(state.file_refs) >= 2

    # Verify every file is stored within storage_root
    for ref in state.file_refs:
        path = store.path_for(ref)
        assert path.is_file()
        assert path.resolve().is_relative_to(storage_root.resolve())

    # Path traversal attack must be rejected
    with pytest.raises(ValueError):
        store.get("../evil.txt")
