from packages.contracts import AdContract, CheckType
from packages.rules.utm_presence import check_utm_presence


def make_ad(destination_url: str) -> AdContract:
    return AdContract(
        ad_id="ad-14",
        batch_id="batch-14",
        client_id="client-14",
        row_number=14,
        platform="facebook",
        headline="A synthetic headline",
        destination_url=destination_url,
        row_hash="row-hash",
        sheet_hash="sheet-hash",
    )


def test_all_required_utms_present_with_url_without_scheme():
    findings = check_utm_presence(
        make_ad(
            "shop.example/item?utm_source=facebook&utm_medium=paid_social"
            "&utm_campaign=launch_oct"
        )
    )

    assert findings == []


def test_each_missing_parameter_has_a_mechanical_finding():
    findings = check_utm_presence(
        make_ad("https://shop.example/item?utm_source=facebook&utm_campaign=launch_oct")
    )

    assert len(findings) == 1
    assert findings[0].field == "utm_medium"
    assert findings[0].check_type is CheckType.MECHANICAL
    assert findings[0].row_number == 14
    assert "utm_medium" in findings[0].proposed_fix


def test_blank_required_value_is_missing_but_nonblank_duplicate_counts():
    findings = check_utm_presence(
        make_ad(
            "https://shop.example/item?utm_source=&utm_source=facebook"
            "&utm_medium=paid_social&utm_campaign="
        )
    )

    assert [finding.field for finding in findings] == ["utm_campaign"]


def test_unrelated_query_parameters_do_not_satisfy_utm_presence():
    findings = check_utm_presence(make_ad("https://shop.example/item?source=facebook#utm_medium=x"))

    assert [finding.field for finding in findings] == [
        "utm_source",
        "utm_medium",
        "utm_campaign",
    ]


def test_required_parameters_can_be_supplied_by_caller():
    findings = check_utm_presence(
        make_ad("https://shop.example/item?tracking_id=abc"),
        required_params=("tracking_id",),
    )

    assert findings == []
