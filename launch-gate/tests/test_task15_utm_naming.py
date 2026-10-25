"""SYNTHETIC naming-convention fixtures based on the mock naming config."""

from packages.contracts import AdContract
from packages.rules.utm_naming import check_utm_naming


def make_ad(url: str, *, platform: str = "Meta", client_id: str = "brightleaf") -> AdContract:
    return AdContract(
        ad_id="synthetic-ad-15",
        batch_id="synthetic-batch-15",
        client_id=client_id,
        row_number=15,
        platform=platform,
        headline="Synthetic headline",
        destination_url=url,
        row_hash="synthetic-row-hash",
        sheet_hash="synthetic-sheet-hash",
    )


def test_recognized_source_and_medium_aliases_are_suggested_without_mutation():
    ad = make_ad(
        "shop.example/item?utm_source=FB&utm_medium=Paid%20Social"
        "&utm_campaign=launch_oct"
    )

    findings = check_utm_naming(ad)

    assert {finding.field for finding in findings} == {"utm_source", "utm_medium"}
    assert {finding.proposed_fix for finding in findings} == {
        "Use 'facebook' for utm_source on facebook ads.",
        "Use 'paid_social' for utm_medium on facebook ads.",
    }
    assert {(finding.actual_value, finding.expected_format) for finding in findings} == {
        ("FB", "facebook"),
        ("Paid Social", "paid_social"),
    }
    assert ad.destination_url.endswith("utm_campaign=launch_oct")


def test_wrong_source_medium_and_campaign_get_suggested_values():
    findings = check_utm_naming(
        make_ad(
            "https://shop.example/item?utm_source=facebook&utm_medium=paid_social"
            "&utm_campaign=Summer%20Sale%20Oct",
            platform="Google Ads",
        )
    )

    # Source and medium mismatch the platform; campaign is normalizable to the documented pattern.
    by_field = {finding.field: finding for finding in findings}
    assert by_field["utm_source"].proposed_fix == "Use 'google' for utm_source on google ads."
    assert by_field["utm_medium"].proposed_fix == "Use 'cpc' for utm_medium on google ads."
    assert by_field["utm_campaign"].proposed_fix == "Use 'summer_sale_oct' for utm_campaign."


def test_client_campaign_override_accepts_quarter_suffix_only_for_oakandhome():
    url = "https://shop.example/item?utm_source=facebook&utm_medium=paid_social&utm_campaign=rugs_q4"

    assert not [f for f in check_utm_naming(make_ad(url, client_id="oakandhome")) if f.field == "utm_campaign"]
    assert [f for f in check_utm_naming(make_ad(url, client_id="brightleaf")) if f.field == "utm_campaign"]


def test_campaign_case_and_spacing_suggest_normalized_value():
    findings = check_utm_naming(
        make_ad(
            "https://shop.example/item?utm_source=facebook&utm_medium=paid_social"
            "&utm_campaign=Glow%20Sale%20Oct"
        )
    )

    campaign = next(finding for finding in findings if finding.field == "utm_campaign")
    assert campaign.proposed_fix == "Use 'glow_sale_oct' for utm_campaign."
    assert campaign.actual_value == "Glow Sale Oct"
    assert campaign.expected_format == "^[a-z0-9]+(_[a-z0-9]+)*_(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)$"


def test_misspelled_key_and_wrong_parameter_order_are_reported():
    findings = check_utm_naming(
        make_ad(
            "https://shop.example/item?utm_medium=paid_social&utm_sorce=facebook"
            "&utm_campaign=launch_oct&utm_source=facebook"
        )
    )

    assert any("utm_sorce" in finding.message and finding.field == "utm_source" for finding in findings)
    assert any(finding.field == "destination_url" and "order" in finding.message for finding in findings)


def test_unknown_platform_does_not_guess_source_or_medium():
    findings = check_utm_naming(
        make_ad(
            "https://shop.example/item?utm_source=unknown&utm_medium=unknown"
            "&utm_campaign=launch_oct",
            platform="Unlisted Platform",
        )
    )

    assert findings == []
