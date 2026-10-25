from fastapi import HTTPException
import pytest
from contextlib import contextmanager

from apps.api import campaign_flow
from apps.api.auth import CurrentUser
from apps.api.campaign_flow import LaunchBatchRequest, build_launch_hierarchy, load_batch_campaigns, order_ads_by_hierarchy


def hierarchy_payload(*, campaign_key="campaign-a", ad_set_key="adset-a"):
    return {
        "client_id": "client-a",
        "employee_email": "buyer@example.com",
        "target_platforms": ["facebook"],
        "campaigns": [{
            "campaign_key": campaign_key,
            "name": "Summer Campaign",
            "daily_budget_minor_units": 150000,
            "ad_sets": [{
                "ad_set_key": ad_set_key,
                "campaign_key": campaign_key,
                "name": "US Prospecting",
                "targeting_country": "US",
                "ads": [{
                    "ad_key": "ad-a",
                    "ad_set_key": ad_set_key,
                    "platform": "facebook",
                    "headline": "Headline",
                    "ad_text": "Copy",
                    "destination_url": "https://example.com/?utm_source=facebook&utm_medium=paid_social&utm_campaign=summer",
                    "creative_file": "https://cdn.example.com/creative.png",
                }],
            }],
        }],
    }


def stored_hierarchy_row(*, campaign_key, ad_set_key, ad_key, ad_id):
    return {
        "campaign_key": campaign_key,
        "ad_set_key": ad_set_key,
        "ad_key": ad_key,
        "campaign_name": campaign_key,
        "campaign_objective": "OUTCOME_TRAFFIC",
        "campaign_budget_amount": 150000,
        "campaign_budget_strategy": "DAILY",
        "campaign_special_category": "NONE",
        "ad_set_name": ad_set_key,
        "targeting_spec": {"geo_locations": {"countries": ["US"]}},
        "optimization_goal": "LINK_CLICKS",
        "billing_event": "IMPRESSIONS",
        "ad_set_budget_amount": None,
        "ad_name": ad_key,
        "creative_url": "https://cdn.example.test/ad.jpg",
        "creative_mime_type": "image/jpeg",
        "body_copy": "Body copy",
        "headline": "Headline",
        "page_id": "page-1",
        "platform": "facebook",
        "destination_url": "https://shop.example.test/",
        "instagram_actor_id": None,
        "placements": ["facebook"],
        "schedule": {},
        "ad_id": ad_id,
    }


class StoredHierarchyConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return self.rows


def test_flat_upload_rows_rebuild_one_campaign_with_37_ads():
    rows = [
        (f"ad-id-{index}", stored_hierarchy_row(
            campaign_key="holiday", ad_set_key="prospecting", ad_key=f"ad-{index}", ad_id=f"ad-id-{index}"
        ))
        for index in range(37)
    ]

    campaigns = load_batch_campaigns(StoredHierarchyConnection(rows), "batch-1", "client-a")

    assert len(campaigns) == 1
    assert len(campaigns[0].ad_sets) == 1
    assert len(campaigns[0].ad_sets[0].ads) == 37


def test_flat_upload_rows_rebuild_37_separate_campaigns():
    rows = [
        (f"ad-id-{index}", stored_hierarchy_row(
            campaign_key=f"campaign-{index}", ad_set_key=f"set-{index}", ad_key=f"ad-{index}", ad_id=f"ad-id-{index}"
        ))
        for index in range(37)
    ]

    campaigns = load_batch_campaigns(StoredHierarchyConnection(rows), "batch-1", "client-a")

    assert len(campaigns) == 37
    assert all(len(campaign.ad_sets) == 1 and len(campaign.ad_sets[0].ads) == 1 for campaign in campaigns)


def test_hierarchy_preview_reports_parent_and_budget_totals():
    result = build_launch_hierarchy(LaunchBatchRequest(**hierarchy_payload()))

    assert result["preview"] == {
        "campaign_count": 1,
        "ad_set_count": 1,
        "creative_count": 1,
        "ad_count": 1,
        "total_budget_minor_units": 150000,
        "target_platforms": ["facebook"],
        "target_summary": [{
            "campaign_key": "campaign-a",
            "name": "Summer Campaign",
            "ad_sets": [{
                "ad_set_key": "adset-a",
                "name": "US Prospecting",
                "countries": ["US"],
                "platforms": ["facebook"],
            }],
        }],
    }
    assert len(result["hierarchy_hash"]) == 64


def test_meta_preflight_preview_includes_resolved_account_and_target_summary(monkeypatch):
    @contextmanager
    def empty_session():
        yield object()

    monkeypatch.setattr(campaign_flow, "init_flow_tables", lambda: None)
    monkeypatch.setattr(campaign_flow, "resolve_campaign_client", lambda *_args: "client-a")
    monkeypatch.setattr(campaign_flow, "get_session", empty_session)
    monkeypatch.setattr(
        "apps.api.clients.resolve_client_meta_account",
        lambda _conn, _client_id: "123456",
    )
    user = CurrentUser("buyer-1", "buyer@example.com", "Buyer", "buyer", ("client-a",))
    request = campaign_flow.PreflightReviewRequest(
        client_id="client-a",
        input_text="Explore our collection.",
        ad_overrides={
            "meta_launch": True,
            "campaign_name": "Spring campaign",
            "target_platforms": ["facebook"],
            "headline": "Our latest offer",
            "ad_text": "Explore our collection.",
            "destination_url": "https://example.com/?utm_source=facebook&utm_medium=paid_social&utm_campaign=spring",
            "creative_file": "https://cdn.example.com/creative.jpg",
            "creative_mime_type": "image/jpeg",
            "meta_page_id": "page-1",
            "targeting_country": "US",
            "daily_budget_minor_units": 10000,
            "special_ad_category": "NONE",
        },
    )

    result = campaign_flow.preflight_review(request, None, user)

    assert result["hierarchy_preview"]["meta_ad_account_id"] == "123456"
    assert result["hierarchy_preview"]["target_platforms"] == ["facebook"]
    assert result["hierarchy_preview"]["target_summary"][0]["ad_sets"][0]["countries"] == ["US"]


def test_meta_preflight_hash_exists_for_batch_without_stored_hierarchy(monkeypatch):
    row = (
        "ad-1",
        "facebook",
        "Our latest offer",
        "Explore our collection.",
        "https://example.com/?utm_source=facebook&utm_medium=paid_social&utm_campaign=spring",
        "https://cdn.example.com/creative.jpg",
        "row-hash",
        "sheet-hash",
        1,
    )

    class PreflightConnection:
        def execute(self, *_args, **_kwargs):
            return self

        def fetchall(self):
            return [row]

    @contextmanager
    def preflight_session():
        yield PreflightConnection()

    monkeypatch.setattr(campaign_flow, "init_flow_tables", lambda: None)
    monkeypatch.setattr(campaign_flow, "resolve_campaign_client", lambda *_args: "client-a")
    monkeypatch.setattr(campaign_flow, "load_batch_campaigns", lambda *_args: None)
    monkeypatch.setattr(campaign_flow, "get_session", preflight_session)
    monkeypatch.setattr(
        "apps.api.clients.resolve_client_meta_account",
        lambda _conn, _client_id: "123456",
    )
    user = CurrentUser("buyer-1", "buyer@example.com", "Buyer", "buyer", ("client-a",))
    overrides = {
        "meta_launch": True,
        "campaign_name": "Spring campaign",
        "target_platforms": ["facebook"],
        "headline": "Our latest offer",
        "ad_text": "Explore our collection.",
        "destination_url": row[4],
        "creative_file": row[5],
        "creative_mime_type": "image/jpeg",
        "meta_page_id": "page-1",
        "targeting_country": "US",
        "daily_budget_minor_units": 10000,
        "special_ad_category": "NONE",
    }
    request = campaign_flow.PreflightReviewRequest(
        client_id="client-a",
        batch_id="batch-1",
        input_text=overrides["ad_text"],
        ad_overrides=overrides,
    )

    result = campaign_flow.preflight_review(request, None, user)
    expected_hash = build_launch_hierarchy(LaunchBatchRequest(
        client_id="client-a",
        batch_id="batch-1",
        employee_email="buyer@example.com",
        campaign_name=overrides["campaign_name"],
        target_platforms=overrides["target_platforms"],
        headline=overrides["headline"],
        ad_text=overrides["ad_text"],
        destination_url=overrides["destination_url"],
        creative_file=overrides["creative_file"],
        creative_mime_type=overrides["creative_mime_type"],
        meta_page_id=overrides["meta_page_id"],
        targeting_country=overrides["targeting_country"],
        daily_budget_minor_units=overrides["daily_budget_minor_units"],
        special_ad_category=overrides["special_ad_category"],
        meta_launch=True,
    ))["hierarchy_hash"]

    assert result["hierarchy_hash"] == expected_hash
    assert result["hierarchy_preview"]["meta_ad_account_id"] == "123456"


def test_meta_launch_requires_write_opt_ins_before_creating_batch(monkeypatch):
    req = LaunchBatchRequest(**{
        **hierarchy_payload(),
        "meta_launch": True,
    })
    req.hierarchy_preview_hash = build_launch_hierarchy(req)["hierarchy_hash"]
    monkeypatch.setattr(campaign_flow, "init_flow_tables", lambda: None)
    monkeypatch.setattr(campaign_flow, "resolve_campaign_client", lambda *_args: "client-a")
    monkeypatch.setenv("META_MCP_ENABLE_LIVE", "true")
    monkeypatch.setenv("META_MCP_ALLOW_WRITES", "false")
    user = CurrentUser("buyer-1", "buyer@example.com", "Buyer", "buyer", ("client-a",))

    with pytest.raises(HTTPException) as exc:
        campaign_flow.launch_batch(req, user=user)

    assert exc.value.status_code == 409
    assert "META_MCP_ALLOW_WRITES=true" in exc.value.detail


def test_hierarchy_rejects_duplicate_campaign_keys():
    payload = hierarchy_payload()
    payload["campaigns"] *= 2

    with pytest.raises(HTTPException) as exc:
        build_launch_hierarchy(LaunchBatchRequest(**payload))
    assert exc.value.status_code == 422
    assert "campaign_key" in exc.value.detail


def test_hierarchy_rejects_ad_set_attached_to_wrong_campaign():
    payload = hierarchy_payload()
    payload["campaigns"][0]["ad_sets"][0]["campaign_key"] = "campaign-other"

    with pytest.raises(HTTPException) as exc:
        build_launch_hierarchy(LaunchBatchRequest(**payload))
    assert exc.value.status_code == 422
    assert "campaign_key" in exc.value.detail


def test_hierarchy_rejects_ad_attached_to_wrong_ad_set():
    payload = hierarchy_payload()
    payload["campaigns"][0]["ad_sets"][0]["ads"][0]["ad_set_key"] = "adset-other"

    with pytest.raises(HTTPException) as exc:
        build_launch_hierarchy(LaunchBatchRequest(**payload))
    assert exc.value.status_code == 422
    assert "ad_set_key" in exc.value.detail


def test_hierarchy_rejects_duplicate_ad_keys():
    payload = hierarchy_payload()
    first = payload["campaigns"][0]["ad_sets"][0]["ads"][0]
    payload["campaigns"][0]["ad_sets"][0]["ads"].append({**first})

    with pytest.raises(HTTPException) as exc:
        build_launch_hierarchy(LaunchBatchRequest(**payload))
    assert exc.value.status_code == 422
    assert "ad_key" in exc.value.detail


def test_one_campaign_with_37_ads_previews_one_shared_parent():
    payload = hierarchy_payload()
    ad_set = payload["campaigns"][0]["ad_sets"][0]
    base_ad = ad_set["ads"][0]
    ad_set["ads"] = [{**base_ad, "ad_key": f"ad-{index}"} for index in range(37)]

    result = build_launch_hierarchy(LaunchBatchRequest(**payload))

    assert result["preview"]["campaign_count"] == 1
    assert result["preview"]["ad_set_count"] == 1
    assert result["preview"]["ad_count"] == 37


def test_37_unique_campaign_keys_preview_37_campaigns():
    template = hierarchy_payload()["campaigns"][0]
    campaigns = []
    for index in range(37):
        campaign_key = f"campaign-{index}"
        ad_set = {**template["ad_sets"][0], "campaign_key": campaign_key, "ad_set_key": f"set-{index}"}
        ad_set["ads"] = [{**ad_set["ads"][0], "ad_set_key": f"set-{index}", "ad_key": f"ad-{index}"}]
        campaigns.append({**template, "campaign_key": campaign_key, "name": campaign_key, "ad_sets": [ad_set]})
    payload = hierarchy_payload()
    payload["campaigns"] = campaigns

    result = build_launch_hierarchy(LaunchBatchRequest(**payload))

    assert result["preview"]["campaign_count"] == 37
    assert result["preview"]["ad_set_count"] == 37
    assert result["preview"]["ad_count"] == 37


def test_tree_ordering_binds_each_row_to_its_own_creative():
    campaign = hierarchy_payload()["campaigns"][0]
    ad = campaign["ad_sets"][0]["ads"][0]
    ad["ad_id"] = "ad-1"
    rows = [("ad-1", ad["headline"], ad["ad_text"], ad["destination_url"], "hash", ad["creative_file"], ad["platform"])]

    assert order_ads_by_hierarchy([campaign], rows) == rows

    ad["creative_file"] = "https://cdn.example.com/other.png"
    with pytest.raises(HTTPException, match="creative does not match uploaded ad"):
        order_ads_by_hierarchy([campaign], rows)
