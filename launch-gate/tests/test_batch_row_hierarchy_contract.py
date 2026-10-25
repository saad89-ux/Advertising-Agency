import pytest

from packages.contracts import BatchRowContract, validate_batch_row_consistency


def row(*, campaign_key="campaign-a", ad_set_key="adset-a", ad_key="ad-a", budget=10000, targeting=None):
    return {
        "campaign_key": campaign_key,
        "ad_set_key": ad_set_key,
        "ad_key": ad_key,
        "campaign_name": campaign_key.title(),
        "campaign_objective": "OUTCOME_SALES",
        "campaign_budget_amount": budget,
        "campaign_special_category": "NONE",
        "campaign_budget_strategy": "DAILY",
        "ad_set_name": ad_set_key.title(),
        "targeting_spec": targeting or {"geo_locations": {"countries": ["US"]}},
        "optimization_goal": "LINK_CLICKS",
        "billing_event": "IMPRESSIONS",
        "ad_set_budget_amount": None,
        "ad_name": ad_key,
        "creative_url": "https://assets.example.test/creative.png",
        "body_copy": "Review the collection.",
        "headline": "A useful headline",
        "page_id": "page-123",
        "platform": "facebook",
        "destination_url": "https://shop.example.test/?utm_source=facebook&utm_medium=paid_social&utm_campaign=summer",
        "instagram_actor_id": None,
        "placements": ["facebook"],
        "schedule": {},
    }


def test_contract_accepts_one_campaign_with_many_ads():
    rows = validate_batch_row_consistency([
        row(ad_key="ad-1"),
        row(ad_key="ad-2"),
        row(ad_key="ad-3", ad_set_key="adset-b"),
    ])

    assert len(rows) == 3
    assert {item.campaign_key for item in rows} == {"campaign-a"}
    assert len({item.ad_key for item in rows}) == 3


def test_contract_accepts_many_campaigns():
    rows = validate_batch_row_consistency([
        row(campaign_key="campaign-a", ad_set_key="adset-a", ad_key="ad-a"),
        row(campaign_key="campaign-b", ad_set_key="adset-b", ad_key="ad-b"),
        row(campaign_key="campaign-c", ad_set_key="adset-c", ad_key="ad-c"),
    ])

    assert {item.campaign_key for item in rows} == {"campaign-a", "campaign-b", "campaign-c"}


def test_conflicting_campaign_settings_are_rejected():
    with pytest.raises(ValueError, match="inconsistent campaign settings"):
        validate_batch_row_consistency([row(ad_key="ad-1"), row(ad_key="ad-2", budget=20000)])


def test_ad_set_cannot_belong_to_two_campaigns():
    with pytest.raises(ValueError, match="cannot belong to two campaigns"):
        validate_batch_row_consistency([
            row(campaign_key="campaign-a", ad_set_key="shared-set", ad_key="ad-1"),
            row(campaign_key="campaign-b", ad_set_key="shared-set", ad_key="ad-2"),
        ])


def test_conflicting_targeting_for_same_ad_set_is_rejected():
    with pytest.raises(ValueError, match="inconsistent ad-set settings"):
        validate_batch_row_consistency([
            row(ad_key="ad-1"),
            row(ad_key="ad-2", targeting={"geo_locations": {"countries": ["CA"]}}),
        ])


def test_duplicate_ad_keys_are_rejected():
    with pytest.raises(ValueError, match="Duplicate ad_key"):
        validate_batch_row_consistency([row(ad_key="duplicate"), row(ad_key="duplicate")])


def test_contract_parses_json_targeting_from_csv_string():
    parsed = BatchRowContract.model_validate(row(targeting='{"geo_locations":{"countries":["US"]}}'))

    assert parsed.targeting_spec["geo_locations"]["countries"] == ["US"]


def test_contract_parses_placements_json_and_optional_budget_blanks():
    payload = row()
    payload["placements"] = '["facebook","instagram"]'
    payload["campaign_budget_amount"] = None
    payload["campaign_budget_strategy"] = "AD_SET_DAILY"
    payload["ad_set_budget_amount"] = 5000
    parsed = BatchRowContract.model_validate(payload)

    assert parsed.placements == ["facebook", "instagram"]
    assert parsed.campaign_budget_amount is None


def test_contract_rejects_campaign_budget_on_ad_set_budget_strategy():
    payload = row()
    payload["campaign_budget_strategy"] = "AD_SET_DAILY"
    payload["ad_set_budget_amount"] = 5000

    with pytest.raises(ValueError, match="forbids campaign_budget_amount"):
        BatchRowContract.model_validate(payload)


def test_contract_rejects_unsupported_objective():
    payload = row()
    payload["campaign_objective"] = "LINK_CLICKS"

    with pytest.raises(ValueError, match="supported Meta ODAX objective"):
        BatchRowContract.model_validate(payload)


def test_upload_validator_attaches_hierarchy_snapshot():
    from apps.api.uploads import validate_hierarchy_rows

    rows = [(1, row(ad_key="ad-1")), (2, row(ad_key="ad-2"))]

    assert validate_hierarchy_rows("client-a", rows) is True
    assert all(row_data["_hierarchy_data"]["campaign_key"] == "campaign-a" for _, row_data in rows)
    assert rows[0][1]["ad_text"] == rows[0][1]["body_copy"]
    assert rows[0][1]["creative_file"] == rows[0][1]["creative_url"]


def test_upload_validator_accepts_multiple_campaign_groups():
    from apps.api.uploads import validate_hierarchy_rows

    rows = [
        (1, row(campaign_key="campaign-a", ad_set_key="set-a", ad_key="ad-1")),
        (2, row(campaign_key="campaign-b", ad_set_key="set-b", ad_key="ad-2")),
    ]

    assert validate_hierarchy_rows("client-a", rows) is True
    assert [row_data["_hierarchy_data"]["campaign_key"] for _, row_data in rows] == ["campaign-a", "campaign-b"]


def test_upload_validator_rejects_partial_hierarchy_keys():
    from fastapi import HTTPException
    from apps.api.uploads import validate_hierarchy_rows

    incomplete = row(ad_key="ad-2")
    incomplete["campaign_key"] = ""
    with pytest.raises(HTTPException, match="Invalid campaign hierarchy at source row 2"):
        validate_hierarchy_rows("client-a", [(1, row(ad_key="ad-1")), (2, incomplete)])
