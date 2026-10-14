"""Shared platform alias normalization and recognition finding."""

from __future__ import annotations

from dataclasses import dataclass

from packages.contracts import AdContract, CheckType, FindingContract


_PLATFORM_ALIASES = {
    "facebook": {"facebook", "fb", "meta", "facebook ads"},
    "instagram": {"instagram", "ig", "insta", "instagram ads"},
    "tiktok": {"tiktok", "tik tok", "tt"},
    "google": {"google", "google ads", "adwords"},
    "linkedin": {"linkedin", "li"},
}


@dataclass(frozen=True)
class PlatformNormalization:
    raw_value: str
    canonical_value: str | None
    flagged: bool


def normalize_platform(value: str) -> PlatformNormalization:
    """Resolve known aliases while preserving the original value verbatim."""
    normalized = value.strip().casefold()
    canonical = next(
        (
            platform
            for platform, aliases in _PLATFORM_ALIASES.items()
            if normalized in aliases
        ),
        None,
    )
    return PlatformNormalization(
        raw_value=value,
        canonical_value=canonical,
        flagged=canonical is None,
    )


def check_platform_recognized(ad: AdContract) -> list[FindingContract]:
    """Ask a person to confirm an unrecognized platform without rewriting it."""
    result = normalize_platform(ad.platform)
    if not result.flagged:
        return []
    return [
        FindingContract(
            finding_id=f"{ad.ad_id}:platform:unrecognized",
            ad_id=ad.ad_id,
            row_number=ad.row_number,
            check_type=CheckType.MECHANICAL,
            message=f"Platform '{result.raw_value}' is not recognized; a person must confirm it.",
            field="platform",
            proposed_fix="Confirm the platform or enter a supported platform name.",
        )
    ]
