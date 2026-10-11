"""Pure mechanical checks for UTM spelling and naming conventions."""

from __future__ import annotations

from difflib import get_close_matches
import re
from urllib.parse import parse_qsl, urlsplit

from packages.contracts import AdContract, CheckType, FindingContract
from packages.rules.utm_presence import REQUIRED_UTM_PARAMS


_PLATFORM_ALIASES = {
    "facebook": {"facebook", "fb", "meta", "facebook ads"},
    "instagram": {"instagram", "ig", "insta", "instagram ads"},
    "tiktok": {"tiktok", "tik tok", "tt"},
    "google": {"google", "google ads", "adwords"},
    "linkedin": {"linkedin", "li"},
}
_SOURCE_ALIASES = {
    "facebook": {"facebook", "fb", "meta"},
    "instagram": {"instagram", "ig", "insta"},
    "tiktok": {"tiktok", "tt", "tik tok"},
    "google": {"google", "goog", "adwords"},
    "linkedin": {"linkedin", "li"},
}
_MEDIUM_BY_PLATFORM = {
    "facebook": "paid_social",
    "instagram": "paid_social",
    "tiktok": "paid_social",
    "linkedin": "paid_social",
    "google": "cpc",
}
_MEDIUM_ALIASES = {
    "paid_social": {"paid_social", "paid social", "paid-social", "paid", "social"},
    "cpc": {"cpc", "ppc", "paid_search"},
}
_CAMPAIGN_PATTERN = re.compile(
    r"^[a-z0-9]+(_[a-z0-9]+)*_(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)$"
)
_OAK_CAMPAIGN_PATTERN = re.compile(
    r"^[a-z0-9]+(_[a-z0-9]+)*_(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|q[1-4])$"
)


def _query_entries(destination_url: str) -> list[tuple[str, str]]:
    candidate = destination_url.strip()
    if "://" not in candidate and not candidate.startswith("//"):
        candidate = "//" + candidate
    try:
        return parse_qsl(urlsplit(candidate).query, keep_blank_values=True)
    except ValueError:
        return []


def _canonical_platform(value: str) -> str | None:
    normalized = value.strip().casefold()
    return next(
        (platform for platform, aliases in _PLATFORM_ALIASES.items() if normalized in aliases),
        None,
    )


def _finding(
    ad: AdContract,
    *,
    issue: str,
    field: str,
    message: str,
    proposed_fix: str,
) -> FindingContract:
    return FindingContract(
        finding_id=f"{ad.ad_id}:utm_naming:{issue}:{field}",
        ad_id=ad.ad_id,
        row_number=ad.row_number,
        check_type=CheckType.MECHANICAL,
        message=message,
        field=field,
        proposed_fix=proposed_fix,
    )


def check_utm_naming(ad: AdContract) -> list[FindingContract]:
    """Suggest UTM spelling/value/order fixes without changing the ad."""
    entries = _query_entries(ad.destination_url)
    values_by_key: dict[str, list[str]] = {}
    for key, value in entries:
        values_by_key.setdefault(key, []).append(value)

    findings: list[FindingContract] = []

    # Report obvious misspellings of required keys; unknown query keys are left alone.
    for key, _value in entries:
        if key in REQUIRED_UTM_PARAMS or not key.casefold().startswith("utm_"):
            continue
        matches = get_close_matches(key.casefold(), REQUIRED_UTM_PARAMS, n=1, cutoff=0.8)
        if matches:
            expected_key = matches[0]
            findings.append(
                _finding(
                    ad,
                    issue="key",
                    field=expected_key,
                    message=f"Query parameter '{key}' appears misspelled; use '{expected_key}'.",
                    proposed_fix=f"Replace query key '{key}' with '{expected_key}'.",
                )
            )

    canonical_platform = _canonical_platform(ad.platform)
    expected_source = canonical_platform
    expected_medium = _MEDIUM_BY_PLATFORM.get(canonical_platform or "")
    value_rules = (
        ("utm_source", expected_source, _SOURCE_ALIASES),
        ("utm_medium", expected_medium, _MEDIUM_ALIASES),
    )
    for parameter, expected, aliases_by_expected in value_rules:
        if expected is None:
            continue
        nonempty_values = [value.strip() for value in values_by_key.get(parameter, []) if value.strip()]
        if not nonempty_values:
            continue  # Task 14 reports missing or blank required values.
        aliases = aliases_by_expected[expected]
        incorrect_values = [
            value for value in nonempty_values if value.casefold() != expected
        ]
        if incorrect_values:
            observed = ", ".join(repr(value) for value in incorrect_values)
            description = (
                "recognized alias"
                if all(value.casefold() in aliases for value in incorrect_values)
                else "value"
            )
            findings.append(
                _finding(
                    ad,
                    issue="value",
                    field=parameter,
                    message=(
                        f"UTM {description} {observed} does not use the agreed "
                        f"platform naming '{expected}'."
                    ),
                    proposed_fix=f"Use '{expected}' for {parameter} on {canonical_platform} ads.",
                )
            )

    campaign_values = [
        value.strip() for value in values_by_key.get("utm_campaign", []) if value.strip()
    ]
    campaign_pattern = (
        _OAK_CAMPAIGN_PATTERN if ad.client_id == "oakandhome" else _CAMPAIGN_PATTERN
    )
    for campaign in campaign_values:
        if campaign_pattern.fullmatch(campaign):
            continue
        normalized = re.sub(r"[^a-z0-9]+", "_", campaign.casefold()).strip("_")
        suggested = normalized if campaign_pattern.fullmatch(normalized) else None
        if suggested:
            fix = f"Use '{suggested}' for utm_campaign."
        elif ad.client_id == "oakandhome":
            fix = (
                "Use lowercase words joined by underscores and end with a three-letter "
                "month or an allowed quarter suffix (q1–q4)."
            )
        else:
            fix = "Use lowercase words joined by underscores and end with a three-letter month."
        findings.append(
            _finding(
                ad,
                issue="value",
                field="utm_campaign",
                message=f"Campaign value '{campaign}' does not match the naming convention.",
                proposed_fix=fix,
            )
        )

    first_positions: dict[str, int] = {}
    for position, (key, _value) in enumerate(entries):
        if key in REQUIRED_UTM_PARAMS:
            first_positions.setdefault(key, position)
    if all(parameter in first_positions for parameter in REQUIRED_UTM_PARAMS):
        actual_order = sorted(REQUIRED_UTM_PARAMS, key=first_positions.__getitem__)
        if tuple(actual_order) != REQUIRED_UTM_PARAMS:
            order_text = ", ".join(REQUIRED_UTM_PARAMS)
            findings.append(
                _finding(
                    ad,
                    issue="order",
                    field="destination_url",
                    message="Required UTM parameters are not in the agreed order.",
                    proposed_fix=f"Order them as {order_text}.",
                )
            )

    return findings
