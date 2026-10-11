"""SYNTHETIC platform limit fixtures loaded from the read-only mock data pack."""

import json
from pathlib import Path

from packages.contracts import AdContract, CheckType
from packages.rules.platform_text_limits import check_platform_text_limits


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_LIMITS = json.loads(
    (REPO_ROOT / "data" / "platform_limits.json").read_text(encoding="utf-8")
)["platforms"]


def make_ad(*, platform="facebook", ad_text="Copy", headline="Headline"):
    return AdContract(
        ad_id="synthetic-ad-16",
        batch_id="synthetic-batch-16",
        client_id="synthetic-client",
        row_number=16,
        platform=platform,
        ad_text=ad_text,
        headline=headline,
        destination_url="https://shop.example/item",
        row_hash="synthetic-row-hash",
        sheet_hash="synthetic-sheet-hash",
    )


def test_values_at_limits_are_allowed():
    limits = SYNTHETIC_LIMITS["facebook"]
    ad = make_ad(
        ad_text="x" * limits["text_max"]["value"],
        headline="x" * limits["headline_max"]["value"],
    )

    assert check_platform_text_limits(ad, SYNTHETIC_LIMITS) == []


def test_over_limit_text_and_headline_create_mechanical_findings():
    ad = make_ad(ad_text="x" * 126, headline="y" * 41)

    findings = check_platform_text_limits(ad, SYNTHETIC_LIMITS)

    assert [finding.field for finding in findings] == ["ad_text", "headline"]
    assert all(finding.check_type is CheckType.MECHANICAL for finding in findings)
    assert findings[0].proposed_fix == "Shorten ad_text to at most 125 characters."
    assert findings[1].proposed_fix == "Shorten headline to at most 40 characters."


def test_platform_alias_uses_canonical_limits():
    ad = make_ad(platform="Meta", ad_text="x" * 126)

    findings = check_platform_text_limits(ad, SYNTHETIC_LIMITS)

    assert len(findings) == 1
    assert findings[0].message.endswith("the facebook limit is 125.")


def test_missing_optional_copy_field_is_skipped():
    ad = make_ad(ad_text=None, headline="x" * 41)

    findings = check_platform_text_limits(ad, SYNTHETIC_LIMITS)

    assert [finding.field for finding in findings] == ["headline"]


def test_unknown_platform_is_not_guessed():
    ad = make_ad(platform="unlisted network", ad_text="x" * 1000, headline="y" * 1000)

    assert check_platform_text_limits(ad, SYNTHETIC_LIMITS) == []


def test_malformed_limit_configuration_fails_clearly():
    ad = make_ad(ad_text="x" * 126)
    limits = {"facebook": {"text_max": {"value": True}, "headline_max": {"value": 40}}}

    try:
        check_platform_text_limits(ad, limits)
    except ValueError as error:
        assert "positive integer" in str(error)
    else:
        raise AssertionError("invalid platform limit should fail")
