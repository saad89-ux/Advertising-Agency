"""Read-only HTTP(S) page opening for worker page checks."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


_FORM_BLOCKING_SCRIPT = """
(() => {
  const block = (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
  };
  document.addEventListener("submit", block, true);
  HTMLFormElement.prototype.submit = function () {};
  HTMLFormElement.prototype.requestSubmit = function () {};
})();
"""

_VISIBLE_TEXT_SCRIPT = """
() => {
  const body = document.body;
  if (!body) return { visible_text: "", hidden_text: "" };
  const ignored = new Set([
    "FORM", "SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "TITLE", "META", "LINK"
  ]);
  const visible = [];
  const hidden = [];
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    let element = node.parentElement;
    let omit = false;
    let isHidden = false;
    while (element && element !== body) {
      if (ignored.has(element.tagName)) { omit = true; break; }
      const style = getComputedStyle(element);
      if (element.hidden || element.getAttribute("aria-hidden") === "true" ||
          style.display === "none" || style.visibility === "hidden" ||
          style.visibility === "collapse" || style.contentVisibility === "hidden") {
        isHidden = true;
      }
      element = element.parentElement;
    }
    if (omit) continue;
    if (isHidden) {
      hidden.push(node.nodeValue || "");
      continue;
    }
    const range = document.createRange();
    range.selectNodeContents(node);
    if (range.getClientRects().length) visible.push(node.nodeValue || "");
  }
  return { visible_text: visible.join(" "), hidden_text: hidden.join(" ") };
}
"""


@dataclass(frozen=True)
class OpenedPage:
    """Small page metadata result; page content remains outside shared state."""

    final_url: str
    status: int | None
    title: str
    screenshot_bytes: bytes | None = None
    visible_text: str | None = None
    hidden_text: str | None = None


class PageOpenError(RuntimeError):
    """Page opening failed for a stable, caller-readable reason."""

    def __init__(self, reason_code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.http_status = http_status


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").casefold().rstrip(".")
        if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        port = parsed.port
        default_port = 80 if scheme == "http" else 443
        return f"{scheme}://{host}" if port in (None, default_port) else f"{scheme}://{host}:{port}"
    except ValueError:
        return None


def _is_allowlisted_url(url: str, allowed_origins: frozenset[str] | set[str]) -> bool:
    origin = _origin(url)
    if origin is None:
        return False
    normalized_allowed = {_origin(value) for value in allowed_origins}
    return origin in normalized_allowed


def _validate_inputs(
    url: str, timeout_ms: int, allowed_origins: frozenset[str] | set[str]
) -> None:
    if not isinstance(url, str) or not _is_http_url(url):
        raise PageOpenError("unsupported_url_scheme", "URL must be an absolute HTTP or HTTPS URL")
    if not _is_allowlisted_url(url, allowed_origins):
        raise PageOpenError("host_not_allowed", "URL origin is not on the page-check allowlist")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise ValueError("timeout_ms must be a positive integer")


async def _handle_route(route, allowed_origins: frozenset[str] | set[str]) -> None:
    request = route.request
    if (
        not _is_http_url(request.url)
        or not _is_allowlisted_url(request.url, allowed_origins)
        or request.method.upper() not in {"GET", "HEAD"}
    ):
        await route.abort("blockedbyclient")
        return
    await route.continue_()


async def open_page_read_only(
    url: str,
    *,
    allowed_origins: frozenset[str] | set[str],
    timeout_ms: int,
    capture_screenshot: bool = False,
    capture_visible_text: bool = False,
    wait_until: Literal["domcontentloaded", "networkidle"] = "domcontentloaded",
    browser_channel: str | None = None,
) -> OpenedPage:
    """Open an HTTP(S) page with bounded navigation and blocked form writes.

    The timeout is required because the architecture specifies timeouts but does not
    define a project-wide default duration.
    """
    _validate_inputs(url, timeout_ms, allowed_origins)

    try:
        async with async_playwright() as playwright:
            launch_options = {"headless": True, "timeout": timeout_ms}
            if browser_channel is not None:
                launch_options["channel"] = browser_channel
            browser = await playwright.chromium.launch(**launch_options)
            try:
                context = await browser.new_context(accept_downloads=False)
                await context.route(
                    "**/*", lambda route: _handle_route(route, allowed_origins)
                )
                page = await context.new_page()
                await page.add_init_script(_FORM_BLOCKING_SCRIPT)
                response = await page.goto(
                    url,
                    wait_until=wait_until,
                    timeout=timeout_ms,
                )
                if not _is_allowlisted_url(page.url, allowed_origins):
                    raise PageOpenError(
                        "host_not_allowed", "Final page origin is not on the page-check allowlist"
                    )
                page_text = (
                    await page.evaluate(_VISIBLE_TEXT_SCRIPT)
                    if capture_visible_text
                    else None
                )
                screenshot_bytes = None
                if capture_screenshot:
                    try:
                        screenshot_bytes = await page.screenshot(timeout=timeout_ms)
                    except PlaywrightTimeoutError as exc:
                        raise PageOpenError(
                            "screenshot_timeout",
                            f"Page screenshot did not complete within {timeout_ms} ms",
                        ) from exc
                return OpenedPage(
                    final_url=page.url,
                    status=response.status if response is not None else None,
                    title=await page.title(),
                    screenshot_bytes=screenshot_bytes,
                    visible_text=page_text["visible_text"] if page_text is not None else None,
                    hidden_text=page_text["hidden_text"] if page_text is not None else None,
                )
            finally:
                await browser.close()
    except PlaywrightTimeoutError as exc:
        raise PageOpenError("page_timeout", f"Page did not load within {timeout_ms} ms") from exc
    except PageOpenError:
        raise
    except Exception as exc:
        raise PageOpenError("page_open_failed", "Page could not be opened") from exc
