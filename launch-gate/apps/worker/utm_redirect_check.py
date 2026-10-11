"""Compare original UTM query values with the final URL after page redirects."""

from __future__ import annotations

from collections import Counter
from urllib.parse import parse_qsl, urlsplit

from apps.worker.file_storage import LocalFileStore
from apps.worker.page_browser import PageOpenError, open_page_read_only
from packages.contracts import AdContract, CheckType, FindingContract


def _query_entries(url: str) -> list[tuple[str, str]]:
    candidate = url.strip()
    if "://" not in candidate and not candidate.startswith("//"):
        candidate = "//" + candidate
    try:
        return parse_qsl(urlsplit(candidate).query, keep_blank_values=True)
    except ValueError:
        return []


def _utm_values(url: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in _query_entries(url):
        if key.casefold().startswith("utm_"):
            values.setdefault(key, []).append(value)
    return values


def _http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _bounded_values(values: list[str]) -> str:
    return repr([value[:200] for value in values])


def _issue_for(original: list[str], final: list[str]) -> str | None:
    if Counter(original) == Counter(final):
        return None
    if not final:
        return "dropped"
    if len(original) > 1 or len(final) > 1:
        return "duplicate_mismatch"
    return "value_changed"


async def check_utm_survives_redirect(
    ad: AdContract,
    *,
    timeout_ms: int,
    file_store: LocalFileStore | None = None,
) -> list[FindingContract]:
    """Return page findings for original UTM values that changed or disappeared.

    The URL is only inspected. It is never rewritten, and browser errors from the page
    opener are propagated so callers can report the existing load/invalid-link result.
    """
    original = _utm_values(ad.destination_url)
    opened = await open_page_read_only(
        ad.destination_url,
        timeout_ms=timeout_ms,
        capture_screenshot=bool(original),
    )
    if not _http_url(opened.final_url):
        raise PageOpenError(
            "unsupported_url_scheme",
            "Redirect ended at a URL that is not HTTP or HTTPS",
        )
    if not original:
        return []

    final = _utm_values(opened.final_url)
    changed = [
        (key, issue)
        for key in sorted(original)
        if (issue := _issue_for(original[key], final.get(key, []))) is not None
    ]
    if not changed:
        return []

    if opened.screenshot_bytes is None:
        raise PageOpenError("screenshot_unavailable", "Page screenshot evidence is unavailable")
    file_ref = (file_store or LocalFileStore()).put(opened.screenshot_bytes)

    findings: list[FindingContract] = []
    for key, issue in changed:
        findings.append(
            FindingContract(
                finding_id=f"{ad.ad_id}:redirect_utm:{issue}:{key}",
                ad_id=ad.ad_id,
                row_number=ad.row_number,
                check_type=CheckType.PAGE,
                message=(
                    f"UTM parameter '{key}' changed across the redirect. "
                    f"Original values: {_bounded_values(original[key])}; "
                    f"final values: {_bounded_values(final.get(key, []))}. "
                    f"Final URL: {opened.final_url}"
                ),
                field=key,
                proposed_fix="Check the redirect keeps the original utm_* parameters",
                file_ref=file_ref,
            )
        )
    return findings
