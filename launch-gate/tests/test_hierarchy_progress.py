import pytest

from apps.api import campaign_flow
from apps.worker import progress_emitter
from apps.worker.states import MetaEntityStatus, entity_status_for_run


def test_run_statuses_map_to_stable_entity_states():
    assert entity_status_for_run("CREATED") is MetaEntityStatus.CREATED_PAUSED
    assert entity_status_for_run("RECONCILING") is MetaEntityStatus.NEEDS_RECONCILIATION
    assert entity_status_for_run("ACTIVE") is MetaEntityStatus.ACTIVE

    with pytest.raises(ValueError, match="Unsupported Meta entity run status"):
        entity_status_for_run("UNKNOWN")


def test_hierarchy_progress_nests_ad_sets_and_aggregates_campaign_and_ad_set_states():
    result = campaign_flow.build_hierarchy_progress(
        [
            ("campaign-run-1", "campaign-one", "facebook", "CREATED", "meta-campaign-1", None, "client-a", 1000),
        ],
        [
            ("ad-set-run-1", "campaign-run-1", "campaign-one", "ad-set-one", None, "CREATING", None, "client-a"),
        ],
    )

    assert result["aggregate_completion_percentage"] == 50
    assert result["summary"] == {
        "total_campaign_groups": 1,
        "completed_campaign_groups": 1,
        "total_ad_sets": 1,
        "completed_ad_sets": 0,
    }
    assert result["structure"][0]["status"] == "created_paused"
    assert result["structure"][0]["ad_sets"][0]["status"] == "creating"
    assert result["campaigns"][0]["status"] == "CREATED"
    assert result["ad_sets"][0]["status"] == "CREATING"
    assert result["structure"][0]["ad_sets"][0]["campaign_run_id"] == "campaign-run-1"


def test_progress_event_publishes_normalized_hierarchy_payload(monkeypatch):
    published = {}

    def capture(batch_id, event, payload):
        published.update(batch_id=batch_id, event=event, payload=payload)
        return 1

    monkeypatch.setattr(progress_emitter.broadcaster, "publish_sync", capture)

    progress_emitter.emit_progress_event(
        "batch-1", "CAMPAIGN", "campaign-one", MetaEntityStatus.CREATING
    )

    assert published["batch_id"] == "batch-1"
    assert published["event"] == "hierarchy_progress"
    assert published["payload"]["status"] == "creating"
    assert published["payload"]["grouping_key"] == "campaign-one"
    assert published["payload"]["client_id"] is None
    assert published["payload"]["timestamp"]
