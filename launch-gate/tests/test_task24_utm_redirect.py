import asyncio
from functools import wraps

import pytest

from apps.worker import utm_redirect_check
from apps.worker.file_storage import LocalFileStore
from apps.worker.page_browser import OpenedPage, PageOpenError
from apps.worker import page_browser
from packages.contracts import AdContract, CheckType


def run_async(test_function):
    @wraps(test_function)
    def wrapper(*args, **kwargs):
        return asyncio.run(test_function(*args, **kwargs))

    return wrapper


def make_ad(url: str) -> AdContract:
    return AdContract(
        ad_id="ad-24",
        batch_id="batch-24",
        client_id="client-24",
        row_number=24,
        platform="Meta",
        headline="Example headline",
        destination_url=url,
        row_hash="row-hash",
        sheet_hash="sheet-hash",
    )


def stub_opener(monkeypatch, final_url, screenshot_bytes=b"synthetic screenshot"):
    calls = []

    async def open_page(url, **kwargs):
        calls.append((url, kwargs))
        return OpenedPage(final_url=final_url, status=200, title="Page", screenshot_bytes=screenshot_bytes)

    monkeypatch.setattr(utm_redirect_check, "open_page_read_only", open_page)
    return calls


@run_async
@pytest.mark.parametrize(
    "original,final",
    [
        (
            "https://example.test/?utm_source=a&utm_medium=b&utm_campaign=c"
            "&utm_content=d&utm_term=e&utm_sorce=typo",
            "https://landing.test/?utm_source=a&utm_medium=b&utm_campaign=c"
            "&utm_content=d&utm_term=e&utm_sorce=typo&gclid=new&utm_new=extra#fragment",
        ),
        (
            "example.test/?utm_source=a&utm_source=b&utm_content=",
            "https://landing.test/?utm_content=&utm_source=b&utm_source=a",
        ),
    ],
)
async def test_passes_when_all_original_utm_values_survive_unordered(original, final, monkeypatch):
    calls = stub_opener(monkeypatch, final)

    findings = await utm_redirect_check.check_utm_survives_redirect(
        make_ad(original), timeout_ms=1200
    )

    assert findings == []
    assert calls[0][0] == original
    assert calls[0][1]["timeout_ms"] == 1200
    assert calls[0][1]["capture_screenshot"] is True
    assert calls[0][1]["allowed_origins"] == {"http://localhost:8099"}


@run_async
@pytest.mark.parametrize(
    "original,final,issue,key",
    [
        ("https://a.test/?utm_source=x", "https://b.test/", "dropped", "utm_source"),
        (
            "https://a.test/?utm_source=x",
            "https://b.test/?utm_source=y",
            "value_changed",
            "utm_source",
        ),
        (
            "https://a.test/?utm_Source=x",
            "https://b.test/?utm_source=x",
            "dropped",
            "utm_Source",
        ),
        (
            "https://a.test/?utm_source=a&utm_source=a",
            "https://b.test/?utm_source=a",
            "duplicate_mismatch",
            "utm_source",
        ),
        (
            "https://a.test/?utm_source=a",
            "https://b.test/?utm_source=a&utm_source=b",
            "duplicate_mismatch",
            "utm_source",
        ),
        (
            "https://a.test/?utm_source=a&utm_source=b",
            "https://b.test/?utm_source=a&utm_source=c",
            "duplicate_mismatch",
            "utm_source",
        ),
        (
            "https://a.test/?utm_term=",
            "https://b.test/",
            "dropped",
            "utm_term",
        ),
    ],
)
async def test_reports_each_changed_key(original, final, issue, key, monkeypatch, tmp_path):
    stub_opener(monkeypatch, final)
    store = LocalFileStore(tmp_path / "storage")

    findings = await utm_redirect_check.check_utm_survives_redirect(
        make_ad(original), timeout_ms=900, file_store=store
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_id == f"ad-24:redirect_utm:{issue}:{key}"
    assert finding.field == key
    assert finding.check_type is CheckType.PAGE
    assert "Final URL:" in finding.message and final in finding.message
    assert finding.proposed_fix == "Check the redirect keeps the original utm_* parameters"
    assert finding.file_ref is not None
    assert store.get(finding.file_ref) == b"synthetic screenshot"


@run_async
async def test_no_original_utm_returns_no_finding_after_page_load(monkeypatch):
    calls = stub_opener(monkeypatch, "https://example.test/final")

    assert await utm_redirect_check.check_utm_survives_redirect(
        make_ad("https://example.test/?gclid=abc#utm_source=fragment"), timeout_ms=500
    ) == []
    assert calls[0][1]["timeout_ms"] == 500
    assert calls[0][1]["capture_screenshot"] is False
    assert calls[0][1]["allowed_origins"] == {"http://localhost:8099"}


@run_async
async def test_changed_values_are_inert_and_truncated(monkeypatch):
    original_value = "x" * 250
    final = "https://landing.test/?utm_campaign=changed"
    stub_opener(monkeypatch, final)

    findings = await utm_redirect_check.check_utm_survives_redirect(
        make_ad(f"https://example.test/?utm_campaign={original_value}"), timeout_ms=500
    )

    assert "x" * 200 in findings[0].message
    assert "x" * 201 not in findings[0].message


@run_async
async def test_load_error_is_propagated_without_utm_loss_finding(monkeypatch):
    async def fail_to_open(*args, **kwargs):
        raise PageOpenError("page_timeout", "Page did not load")

    monkeypatch.setattr(utm_redirect_check, "open_page_read_only", fail_to_open)

    with pytest.raises(PageOpenError) as error:
        await utm_redirect_check.check_utm_survives_redirect(
            make_ad("https://example.test/?utm_source=x"), timeout_ms=500
        )
    assert error.value.reason_code == "page_timeout"


@run_async
async def test_non_http_redirect_is_invalid_link_not_utm_loss(monkeypatch):
    stub_opener(monkeypatch, "file:///unexpected")

    with pytest.raises(PageOpenError) as error:
        await utm_redirect_check.check_utm_survives_redirect(
            make_ad("https://example.test/?utm_source=x"), timeout_ms=500
        )
    assert error.value.reason_code == "unsupported_url_scheme"


@run_async
async def test_original_and_final_fragments_do_not_change_query_comparison(monkeypatch):
    stub_opener(
        monkeypatch,
        "https://landing.test/?utm_source=Case%20Sensitive#different-fragment",
    )

    findings = await utm_redirect_check.check_utm_survives_redirect(
        make_ad("https://example.test/?utm_source=Case+Sensitive#utm_term=not-query"),
        timeout_ms=700,
    )

    assert findings == []


@run_async
async def test_emits_one_finding_for_each_changed_key(monkeypatch):
    stub_opener(monkeypatch, "https://landing.test/?utm_source=wrong")

    findings = await utm_redirect_check.check_utm_survives_redirect(
        make_ad("https://example.test/?utm_source=right&utm_medium=paid_social"),
        timeout_ms=700,
    )

    assert [finding.field for finding in findings] == ["utm_medium", "utm_source"]


@run_async
async def test_captures_screenshot_bytes_when_requested(monkeypatch):
    class Page:
        url = "https://example.test/final"

        async def add_init_script(self, script):
            pass

        async def goto(self, url, **kwargs):
            return None

        async def screenshot(self, **kwargs):
            assert kwargs == {"timeout": 600}
            return b"png-bytes"

        async def title(self):
            return "Page"

    class Context:
        async def route(self, pattern, handler):
            pass

        async def new_page(self):
            return Page()

    class Browser:
        async def new_context(self, **kwargs):
            return Context()

        async def close(self):
            pass

    class Chromium:
        async def launch(self, **kwargs):
            return Browser()

    class PlaywrightContext:
        async def __aenter__(self):
            from types import SimpleNamespace

            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(page_browser, "async_playwright", lambda: PlaywrightContext())

    opened = await page_browser.open_page_read_only(
        "https://example.test",
        allowed_origins={"https://example.test"},
        timeout_ms=600,
        capture_screenshot=True,
    )

    assert opened.screenshot_bytes == b"png-bytes"
