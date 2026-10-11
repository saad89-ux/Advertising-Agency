"""Mechanical flags for platform limits that need human confirmation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from packages.contracts import AdContract, CheckType, FindingContract


_PLATFORM_ALIASES = {
    "facebook": {"facebook", "fb", "meta", "facebook ads"},
    "instagram": {"instagram", "ig", "insta", "instagram ads"},
    "tiktok": {"tiktok", "tik tok", "tt"},
    "google": {"google", "google ads", "adwords"},
    "linkedin": {"linkedin", "li"},
}
_CHECKED_FIELDS = ("headline_max", "text_max", "ratios")


def _canonical_platform(value: str) -> str | None:
    normalized = value.strip().casefold()
    return next(
        (platform for platform, aliases in _PLATFORM_ALIASES.items() if normalized in aliases),
        None,
    )


def _parse_date(value: object, *, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date string") from exc


def check_stale_platform_limits(
    ad: AdContract,
    platform_limits_config: Mapping[str, Any],
) -> list[FindingContract]:
    """Flag each configured limit for the ad's platform older than its cutoff.

    Pass the full decoded ``data/platform_limits.json`` object. Every stale limit is
    raised for human confirmation, even if the ad currently falls within that limit.
    """
    cutoff = _parse_date(
        platform_limits_config.get("stale_if_last_verified_before"),
        label="stale_if_last_verified_before",
    )
    platforms = platform_limits_config.get("platforms")
    if not isinstance(platforms, Mapping):
        raise ValueError("platform limits config must contain a platforms mapping")

    platform = _canonical_platform(ad.platform)
    if platform is None or platform not in platforms:
        return []
    rules = platforms[platform]
    if not isinstance(rules, Mapping):
        raise ValueError(f"limits for platform {platform!r} must be a mapping")

    findings: list[FindingContract] = []
    for field in _CHECKED_FIELDS:
        entry = rules.get(field)
        if not isinstance(entry, Mapping):
            continue
        verified = _parse_date(
            entry.get("last_verified"),
            label=f"{platform}.{field}.last_verified",
        )
        if verified >= cutoff:
            continue
        findings.append(
            FindingContract(
                finding_id=f"{ad.ad_id}:stale_limit:{field}",
                ad_id=ad.ad_id,
                row_number=ad.row_number,
                check_type=CheckType.MECHANICAL,
                message=(
                    f"The {platform} {field} limit was last verified on {verified.isoformat()}, "
                    f"before the {cutoff.isoformat()} cutoff; a person must confirm it."
                ),
                field=field,
                proposed_fix="Have a person confirm this platform limit before relying on it.",
            )
        )
    return findings
