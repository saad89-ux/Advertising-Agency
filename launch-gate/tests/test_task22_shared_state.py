import pytest
from pydantic import ValidationError

from apps.worker.shared_state import SharedState


def test_shared_state_keeps_only_ids_status_summary_and_file_refs():
    state = SharedState(
        ad_id="ad-1",
        client_id="client-1",
        status="checking",
        summary="UTM check completed",
        file_refs=["0123456789abcdef0123456789abcdef"],
    )

    assert state.model_dump() == {
        "ad_id": "ad-1",
        "client_id": "client-1",
        "status": "checking",
        "summary": "UTM check completed",
        "file_refs": ["0123456789abcdef0123456789abcdef"],
    }


def test_shared_state_summary_and_file_refs_default_to_empty():
    state = SharedState(ad_id="ad-1", client_id="client-1", status="queued")

    assert state.summary is None
    assert state.file_refs == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"ad_id": "  "},
        {"client_id": "  "},
        {"status": "  "},
        {"file_refs": [""]},
        {"large_output": "not part of shared state"},
    ],
)
def test_shared_state_rejects_empty_required_values_and_extra_payload(overrides):
    values = {"ad_id": "ad-1", "client_id": "client-1", "status": "queued"}
    values.update(overrides)

    with pytest.raises(ValidationError):
        SharedState(**values)
