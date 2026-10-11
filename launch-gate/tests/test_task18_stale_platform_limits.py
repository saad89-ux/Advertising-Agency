"""SYNTHETIC stale-limit cases from the read-only mock platform limits."""

import json
from datetime import date
from pathlib import Path

from packages.contracts import AdContract, CheckType
from packages.rules.stale_platform_limits import check_stale_platform_limits


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_LIMITS = json.loads(
    (REPO_ROOT / "data" / "platform_limits.json").read_text(encoding="utf-8")
)
SYNTHETIC_EXPECTED = json.loads(
    (REPO_ROOT / "data" / "labels" / "expected_stale_flags.json").read_text(
        encoding="utf-8"
    )
)["expected_stale"]


def make_ad(platform="instagram"):
    return AdContract(
        ad_id=f"synthetic-ad-{platform}",
        batch_id="synthetic-batch-18",
        client_id="synthetic-client",
        row_number=18,
        platform=platform,
        headline="Synthetic headline",
        destination_url="https://shop.example/item",
        row_hash="synthetic-row-hash",
        sheet_hash="synthetic-sheet-hash",
    )


def test_stale_fields_match_synthetic_expected_list():
    found = set()
    for platform in SYNTHETIC_LIMITS["platforms"]:
        for finding in check_stale_platform_limits(make_ad(platform), SYNTHETIC_LIMITS):
            found.add(f"{platform}.{finding.field}")

    assert found == set(SYNTHETIC_EXPECTED)


def test_cutoff_is_strict_older_than():
    config = {
        "stale_if_last_verified_before": "2025-10-10",
        "platforms": {
            "facebook": {
                "text_max": {"value": 125, "last_verified": "2025-10-10"},
                "headline_max": {"value": 40, "last_verified": "2025-10-09"},
            }
        },
    }

    findings = check_stale_platform_limits(make_ad("facebook"), config)

    assert [finding.field for finding in findings] == ["headline_max"]
    assert findings[0].check_type is CheckType.MECHANICAL
    assert "a person must confirm" in findings[0].message


def test_unknown_platform_is_not_guessed():
    assert check_stale_platform_limits(make_ad("unlisted platform"), SYNTHETIC_LIMITS) == []


def test_invalid_cutoff_fails_clearly():
    config = {"stale_if_last_verified_before": "not-a-date", "platforms": {}}
    try:
        check_stale_platform_limits(make_ad(), config)
    except ValueError as error:
        assert "ISO date string" in str(error)
    else:
        raise AssertionError("invalid cutoff should fail")
