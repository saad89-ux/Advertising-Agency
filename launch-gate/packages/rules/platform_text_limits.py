"""Pure mechanical text and headline limit checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.contracts import AdContract, CheckType, FindingContract
from packages.rules.platform_normalization import normalize_platform


def _canonical_platform(value: str) -> str | None:
    return normalize_platform(value).canonical_value


def _limit_value(platform_rules: Mapping[str, Any], key: str) -> int:
    try:
        value = platform_rules[key]["value"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"platform limit {key!r} must contain a value") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"platform limit {key!r} must be a positive integer")
    return value


def check_platform_text_limits(
    ad: AdContract,
    platform_limits: Mapping[str, Mapping[str, Any]],
) -> list[FindingContract]:
    """Report text/headline values longer than limits from the platform-limits file.

    Pass the ``platforms`` mapping from ``data/platform_limits.json``. Staleness is
    intentionally left to the separate stale-limit check (Task 18).
    """
    platform = _canonical_platform(ad.platform)
    if platform is None or platform not in platform_limits:
        return []

    rules = platform_limits[platform]
    checks = (
        ("ad_text", ad.ad_text, "text_max"),
        ("headline", ad.headline, "headline_max"),
    )
    findings: list[FindingContract] = []
    for field, value, limit_key in checks:
        if value is None:
            continue
        limit = _limit_value(rules, limit_key)
        character_count = len(value)
        if character_count <= limit:
            continue
        findings.append(
            FindingContract(
                finding_id=f"{ad.ad_id}:platform_limit:{field}",
                ad_id=ad.ad_id,
                row_number=ad.row_number,
                check_type=CheckType.MECHANICAL,
                message=(
                    f"{field} has {character_count} characters; the {platform} limit is "
                    f"{limit}."
                ),
                field=field,
                proposed_fix=f"Shorten {field} to at most {limit} characters.",
            )
        )
    return findings
