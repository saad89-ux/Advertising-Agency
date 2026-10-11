import asyncio
from functools import wraps
from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from apps.worker import page_browser


def run_async(test_function):
    @wraps(test_function)
    def wrapper(*args, **kwargs):
        return asyncio.run(test_function(*args, **kwargs))

    return wrapper


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/page",
        "file:///C:/secret.txt",
        "javascript:alert(1)",
        "//example.test/page",
        "example.test/page",
        "http:///missing-host",
        "http://[invalid-ipv6]/",
    ],
)
def test_rejects_non_absolute_http_urls(url):
    with pytest.raises(page_browser.PageOpenError) as error:
        page_browser._validate_inputs(url, 1000)

    assert error.value.reason_code == "unsupported_url_scheme"


@pytest.mark.parametrize("timeout_ms", [0, -1, True, 1.5, "1000"])
def test_requires_positive_integer_timeout(timeout_ms):
    with pytest.raises(ValueError, match="timeout_ms must be a positive integer"):
        page_browser._validate_inputs("https://example.test", timeout_ms)


@run_async
async def test_route_allows_only_http_safe_methods():
    class Route:
        def __init__(self, url, method):
            self.request = SimpleNamespace(url=url, method=method)
            self.action = None

        async def abort(self, reason):
            self.action = ("abort", reason)

        async def continue_(self):
            self.action = ("continue",)

    cases = [
        ("https://example.test/", "GET", ("continue",)),
        ("http://example.test/", "HEAD", ("continue",)),
        ("https://example.test/submit", "POST", ("abort", "blockedbyclient")),
        ("file:///secret", "GET", ("abort", "blockedbyclient")),
    ]
    for url, method, expected in cases:
        route = Route(url, method)
        await page_browser._handle_route(route)
        assert route.action == expected


@run_async
async def test_open_returns_page_metadata_and_closes_browser(monkeypatch):
    calls = []

    class Page:
        url = "https://example.test/final"

        async def add_init_script(self, script):
            calls.append(("script", script))

        async def goto(self, url, **kwargs):
            calls.append(("goto", url, kwargs))
            return SimpleNamespace(status=200)

        async def title(self):
            return "Example"

    class Context:
        async def route(self, pattern, handler):
            calls.append(("route", pattern, handler))

        async def new_page(self):
            return Page()

    class Browser:
        async def new_context(self, **kwargs):
            calls.append(("context", kwargs))
            return Context()

        async def close(self):
            calls.append(("close",))

    class Chromium:
        async def launch(self, **kwargs):
            calls.append(("launch", kwargs))
            return Browser()

    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(page_browser, "async_playwright", lambda: PlaywrightContext())

    result = await page_browser.open_page_read_only("https://example.test", timeout_ms=1500)

    assert result == page_browser.OpenedPage(
        final_url="https://example.test/final",
        status=200,
        title="Example",
    )
    assert ("launch", {"headless": True, "timeout": 1500}) in calls
    assert ("goto", "https://example.test", {
        "wait_until": "domcontentloaded",
        "timeout": 1500,
    }) in calls
    assert ("close",) in calls
    assert any(call[0] == "script" and "preventDefault" in call[1] for call in calls)


@run_async
async def test_navigation_timeout_has_stable_error(monkeypatch):
    class Page:
        async def add_init_script(self, script):
            pass

        async def goto(self, url, **kwargs):
            raise PlaywrightTimeoutError("details hidden from caller")

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
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(page_browser, "async_playwright", lambda: PlaywrightContext())

    with pytest.raises(page_browser.PageOpenError, match="within 800 ms") as error:
        await page_browser.open_page_read_only("https://example.test", timeout_ms=800)
    assert error.value.reason_code == "page_timeout"
