from apps.api.campaign_flow import load_batch_campaigns


class HierarchyRowsConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return self.rows


def hierarchy_row(index: int) -> dict:
    return {
        "campaign_key": "camp_1",
        "ad_set_key": "as_1",
        "ad_key": f"ad_{index}",
        "campaign_name": "Promo",
        "campaign_objective": "OUTCOME_TRAFFIC",
        "campaign_budget_amount": 5000,
        "campaign_budget_strategy": "DAILY",
        "campaign_special_category": "NONE",
        "ad_set_name": "Prospecting",
        "targeting_spec": {"geo_locations": {"countries": ["US"]}},
        "optimization_goal": "LINK_CLICKS",
        "billing_event": "IMPRESSIONS",
        "ad_set_budget_amount": None,
        "ad_name": f"Ad {index}",
        "creative_url": f"https://cdn.example.test/img_{index}.png",
        "creative_mime_type": "image/png",
        "body_copy": f"Copy {index}",
        "headline": f"Headline {index}",
        "page_id": "page-1",
        "platform": "facebook",
        "destination_url": "https://shop.example.test/",
        "instagram_actor_id": None,
        "placements": ["facebook"],
        "schedule": {},
    }


def test_37_rows_group_into_one_campaign_and_keep_creatives_aligned():
    rows = [
        (f"ad-id-{index}", {**hierarchy_row(index), "ad_id": f"ad-id-{index}"})
        for index in range(37)
    ]

    campaigns = load_batch_campaigns(
        HierarchyRowsConnection(rows),
        "batch-local",
        "client-local",
    )

    assert len(campaigns) == 1
    assert campaigns[0].campaign_key == "camp_1"
    assert len(campaigns[0].ad_sets) == 1
    ads = campaigns[0].ad_sets[0].ads
    assert len(ads) == 37
    assert {
        ad.ad_key: ad.creative_file
        for ad in ads
    } == {
        f"ad_{index}": f"https://cdn.example.test/img_{index}.png"
        for index in range(37)
    }
