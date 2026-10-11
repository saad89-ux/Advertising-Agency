"""Capture a compact page snapshot and keep large page artifacts in local storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from apps.worker.file_storage import LocalFileStore
from apps.worker.page_browser import PageOpenError, open_page_read_only
from packages.contracts import VerdictStatus
from packages.guards.input_guard import GuardSignal, guard_page_text


class PageSnapshot(TypedDict):
    loaded: bool
    http_status: int | None
    final_url: str
    visible_text: str
    screenshot_ref: str | None
    text_ref: str | None


@dataclass(frozen=True)
class PageCaptureResult:
    page_snapshot: PageSnapshot
    guard_signals: list[GuardSignal]
    verdict_floor: VerdictStatus | None
    page_error: PageOpenError | None = None


async def capture_page_snapshot(
    url: str,
    *,
    timeout_ms: int,
    file_store: LocalFileStore | None = None,
    browser_channel: str | None = None,
) -> PageCaptureResult:
    """Read visible body text after network idle, guard hidden text, and store artifacts."""
    store = file_store or LocalFileStore()
    try:
        opened = await open_page_read_only(
            url,
            timeout_ms=timeout_ms,
            capture_screenshot=True,
            capture_visible_text=True,
            wait_until="networkidle",
            browser_channel=browser_channel,
        )
    except PageOpenError as error:
        return PageCaptureResult(
            page_snapshot={
                "loaded": False,
                "http_status": None,
                "final_url": url,
                "visible_text": "",
                "screenshot_ref": None,
                "text_ref": None,
            },
            guard_signals=[],
            verdict_floor=None,
            page_error=error,
        )

    visible_raw = opened.visible_text or ""
    hidden_raw = opened.hidden_text or ""
    visible_guard = guard_page_text(visible_raw)
    hidden_guard = guard_page_text(hidden_raw)
    signals = visible_guard.signals + hidden_guard.signals
    verdict_floor = (
        VerdictStatus.NEEDS_PERSON
        if visible_guard.verdict_floor is VerdictStatus.NEEDS_PERSON
        or hidden_guard.verdict_floor is VerdictStatus.NEEDS_PERSON
        else None
    )

    screenshot_ref = (
        store.put(opened.screenshot_bytes)
        if opened.screenshot_bytes is not None
        else None
    )
    text_ref = store.put(visible_raw.encode("utf-8"))
    return PageCaptureResult(
        page_snapshot={
            "loaded": opened.status is not None,
            "http_status": opened.status,
            "final_url": opened.final_url,
            "visible_text": visible_guard.sanitized_text.text,
            "screenshot_ref": screenshot_ref,
            "text_ref": text_ref,
        },
        guard_signals=signals,
        verdict_floor=verdict_floor,
    )
