"""SYNTHETIC creative checks against the read-only mock manifest and limits."""

import csv
import json
from pathlib import Path

from packages.contracts import AdContract, CheckType
from packages.rules.creative_size_ratio import check_creative_size_ratio


REPO_ROOT = Path(__file__).resolve().parents[1]
with (REPO_ROOT / "data" / "creatives_manifest.csv").open(
    newline="", encoding="utf-8"
) as source:
    SYNTHETIC_MANIFEST = {
        row["file"]: {"width": int(row["width"]), "height": int(row["height"])}
        for row in csv.DictReader(source)
    }
SYNTHETIC_LIMITS = json.loads(
    (REPO_ROOT / "data" / "platform_limits.json").read_text(encoding="utf-8")
)["platforms"]


def make_ad(*, platform="facebook", creative_file="bundle_v1_1080x1350.png"):
    return AdContract(
        ad_id="synthetic-ad-17",
        batch_id="synthetic-batch-17",
        client_id="synthetic-client",
        row_number=17,
        platform=platform,
        headline="Synthetic headline",
        creative_file=creative_file,
        destination_url="https://shop.example/item",
        row_hash="synthetic-row-hash",
        sheet_hash="synthetic-sheet-hash",
    )


def test_manifest_dimensions_with_allowed_ratio_pass():
    assert check_creative_size_ratio(
        make_ad(), SYNTHETIC_MANIFEST, SYNTHETIC_LIMITS
    ) == []


def test_disallowed_ratio_returns_mechanical_finding_and_suggestion():
    findings = check_creative_size_ratio(
        make_ad(creative_file="bundle_v9_1920x1080.mp4"),
        SYNTHETIC_MANIFEST,
        SYNTHETIC_LIMITS,
    )

    assert len(findings) == 1
    assert findings[0].check_type is CheckType.MECHANICAL
    assert findings[0].field == "creative_file"
    assert "1920:1080" in findings[0].message
    assert findings[0].proposed_fix == "Use a creative with ratio 1:1/4:5/9:16/1.91:1."


def test_two_percent_ratio_tolerance_is_inclusive():
    manifest = {"edge.png": {"width": 102, "height": 100}}
    assert check_creative_size_ratio(
        make_ad(creative_file="edge.png"),
        manifest,
        {"facebook": {"ratios": {"value": ["1:1"]}}},
    ) == []


def test_ratio_outside_two_percent_is_flagged():
    manifest = {"edge.png": {"width": 103, "height": 100}}
    assert check_creative_size_ratio(
        make_ad(creative_file="edge.png"),
        manifest,
        {"facebook": {"ratios": {"value": ["1:1"]}}},
    )[0].field == "creative_file"


def test_google_blank_or_na_creative_is_allowed():
    for filename in (None, "n/a"):
        assert check_creative_size_ratio(
            make_ad(platform="Google Ads", creative_file=filename),
            SYNTHETIC_MANIFEST,
            SYNTHETIC_LIMITS,
        ) == []


def test_missing_creative_for_other_platform_is_reported():
    finding = check_creative_size_ratio(
        make_ad(creative_file=None), SYNTHETIC_MANIFEST, SYNTHETIC_LIMITS
    )[0]

    assert finding.message.startswith("A creative file is required")


def test_unlisted_creative_is_not_given_guessed_dimensions():
    finding = check_creative_size_ratio(
        make_ad(creative_file="unknown.png"), SYNTHETIC_MANIFEST, SYNTHETIC_LIMITS
    )[0]

    assert "not in the configured manifest" in finding.message


def test_unknown_platform_is_not_guessed():
    assert check_creative_size_ratio(
        make_ad(platform="unknown", creative_file="unknown.png"),
        SYNTHETIC_MANIFEST,
        SYNTHETIC_LIMITS,
    ) == []
