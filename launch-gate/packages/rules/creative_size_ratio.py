"""Pure mechanical creative dimension and aspect-ratio checks."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from packages.contracts import AdContract, CheckType, FindingContract
from packages.rules.platform_normalization import normalize_platform


RATIO_TOLERANCE = Decimal("0.02")
def _canonical_platform(value: str) -> str | None:
    return normalize_platform(value).canonical_value


def _finding(
    ad: AdContract,
    *,
    issue: str,
    message: str,
    proposed_fix: str,
    actual_value: str | None = None,
    expected_format: str | None = None,
) -> FindingContract:
    return FindingContract(
        finding_id=f"{ad.ad_id}:creative:{issue}",
        ad_id=ad.ad_id,
        row_number=ad.row_number,
        check_type=CheckType.MECHANICAL,
        message=message,
        field="creative_file",
        proposed_fix=proposed_fix,
        actual_value=actual_value,
        expected_format=expected_format,
    )


def _dimensions(entry: Mapping[str, Any], filename: str) -> tuple[int, int]:
    width = entry.get("width")
    height = entry.get("height")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
    ):
        raise ValueError(f"creative dimensions for {filename!r} must be positive integers")
    return width, height


def _ratio(value: str) -> Decimal:
    try:
        width_text, height_text = value.split(":", 1)
        width = Decimal(width_text)
        height = Decimal(height_text)
        if width <= 0 or height <= 0:
            raise InvalidOperation
        return width / height
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid allowed creative ratio: {value!r}") from exc


def check_creative_size_ratio(
    ad: AdContract,
    creative_manifest: Mapping[str, Mapping[str, Any]],
    platform_limits: Mapping[str, Mapping[str, Any]],
) -> list[FindingContract]:
    """Resolve manifest dimensions and compare their aspect ratio to platform limits.

    ``creative_manifest`` is keyed by filename and contains ``width`` and ``height``.
    ``platform_limits`` is the ``platforms`` mapping from the platform-limits file.
    """
    platform = _canonical_platform(ad.platform)
    if platform is None or platform not in platform_limits:
        return []

    filename = (ad.creative_file or "").strip()
    if not filename or filename.casefold() == "n/a":
        if platform == "google":
            return []
        return [
            _finding(
                ad,
                issue="missing",
                message=f"A creative file is required to check {platform} dimensions and ratio.",
                proposed_fix="Provide a creative file with verifiable dimensions.",
            )
        ]

    manifest_key = next(
        (key for key in creative_manifest if key.strip().casefold() == filename.casefold()),
        None,
    )
    if manifest_key is None:
        return [
            _finding(
                ad,
                issue="unknown",
                message=f"Creative dimensions for '{filename}' are not in the configured manifest.",
                proposed_fix="Provide a creative with dimensions listed in the manifest.",
            )
        ]

    width, height = _dimensions(creative_manifest[manifest_key], filename)
    try:
        allowed_ratios = platform_limits[platform]["ratios"]["value"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"ratio limits for {platform!r} must contain a value list") from exc
    if not isinstance(allowed_ratios, list):
        raise ValueError(f"ratio limits for {platform!r} must contain a value list")
    if not allowed_ratios:
        return []

    actual_ratio = Decimal(width) / Decimal(height)
    allowed = [(name, _ratio(name)) for name in allowed_ratios]
    if any(abs(actual_ratio - target) / target <= RATIO_TOLERANCE for _, target in allowed):
        return []

    actual_text = f"{width}:{height} ({actual_ratio:.3f}:1)"
    allowed_text = "/".join(name for name, _ in allowed)
    expected_text = f"{allowed_text} (2% ratio tolerance)"
    return [
        _finding(
            ad,
            issue="ratio",
            message=(
                f"Creative '{filename}' is {actual_text}; {platform} allows "
                f"{expected_text}."
            ),
            proposed_fix=f"Use a creative with ratio {allowed_text}.",
            actual_value=f"{width}x{height} pixels ({actual_text})",
            expected_format=expected_text,
        )
    ]
