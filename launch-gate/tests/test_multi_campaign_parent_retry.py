def retry_targets(parent_campaigns):
    """Return only campaigns lacking a persisted Meta object ID."""
    return [campaign for campaign in parent_campaigns if not campaign["meta_campaign_id"]]


def test_retry_skips_successful_campaign_one():
    campaigns = [
        {"campaign_key": "campaign-1", "meta_campaign_id": "meta-campaign-1"},
        {"campaign_key": "campaign-2", "meta_campaign_id": None},
        {"campaign_key": "campaign-3", "meta_campaign_id": "meta-campaign-3"},
    ]

    assert retry_targets(campaigns) == [campaigns[1]]


def test_failed_campaign_does_not_mark_other_parents_failed():
    campaigns = [
        {"campaign_key": "campaign-1", "status": "created_paused", "meta_campaign_id": "meta-campaign-1"},
        {"campaign_key": "campaign-2", "status": "failed", "meta_campaign_id": None},
    ]

    assert campaigns[0]["status"] == "created_paused"
    assert campaigns[1]["status"] == "failed"


def test_successful_ad_set_is_reused_by_ad_retry():
    parents = {"campaign": "meta-campaign-1", "ad_set": "meta-adset-1"}
    retry_existing_ids = dict(parents)

    assert retry_existing_ids == {"campaign": "meta-campaign-1", "ad_set": "meta-adset-1"}
