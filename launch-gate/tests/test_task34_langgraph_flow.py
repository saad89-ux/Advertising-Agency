"""Tests for Task 34: Step Cap & Bounded LangGraph Flow Wiring."""

from __future__ import annotations

from datetime import date, timedelta
import json
from unittest.mock import MagicMock
import pytest

from langgraph.graph.state import CompiledStateGraph

from apps.worker.graph import (
    WorkerGraphResult,
    create_ad_worker_graph,
    run_ad_worker_flow,
)
from apps.worker.retrieval import RetrievedRule
from packages.contracts import AdContract, CheckType, FindingContract, VerdictStatus


def sample_manifest() -> dict[str, dict[str, int]]:
    return {
        "square.png": {"width": 1080, "height": 1080},
        "bad_ratio.png": {"width": 1000, "height": 500},
        "bundle_v1_1080x1080.png": {"width": 1080, "height": 1080},
    }


def sample_platform_limits() -> dict[str, Any]:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return {
        "stale_if_last_verified_before": "2024-01-01",
        "platforms": {
            "facebook": {
                "text_max": {"value": 125, "last_verified": tomorrow},
                "headline_max": {"value": 40, "last_verified": tomorrow},
                "ratios": {"value": ["1:1", "4:5"], "last_verified": tomorrow},
            }
        },
    }


def sample_ad(
    ad_id: str = "ad-flow-1",
    client_id: str = "brightleaf",
    platform: str = "Facebook",
    headline: str = "Radiant Aloe Serum",
    ad_text: str = "Contains organic aloe vera for daily soothing hydration.",
    destination_url: str = "https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=serum_jan",
    creative_file: str | None = "bundle_v1_1080x1080.png",
) -> AdContract:
    return AdContract(
        ad_id=ad_id,
        batch_id="batch-1",
        client_id=client_id,
        row_number=1,
        platform=platform,
        headline=headline,
        ad_text=ad_text,
        destination_url=destination_url,
        creative_file=creative_file,
        row_hash="rowhash",
        sheet_hash="sheethash",
    )


def sample_retrieved_rules() -> list[RetrievedRule]:
    return [
        RetrievedRule(
            client_id="brightleaf",
            entry_number="BL-BB-1.1",
            source_file="brand_book.md",
            content="Brand Voice: Warm, natural, honest. Never exaggerate benefits.",
        ),
        RetrievedRule(
            client_id="brightleaf",
            entry_number="BL-C-01",
            source_file="claims_register.md",
            content="Claim: Contains organic aloe vera for daily soothing hydration.",
        ),
    ]


def make_mock_model_client(response_dict: dict) -> MagicMock:
    client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(response_dict)
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 100
    mock_usage.candidates_token_count = 50
    mock_resp.usage_metadata = mock_usage
    client.models.generate_content.return_value = mock_resp
    return client


class FakeDBConnection:
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def execute(self, query: str, params: tuple = ()):
        return self

    def fetchone(self):
        return (0.001,)

    def commit(self):
        pass


def test_create_ad_worker_graph_structure():
    graph = create_ad_worker_graph()
    assert isinstance(graph, CompiledStateGraph)
    node_names = set(graph.nodes.keys())
    assert {"mechanical", "page_check", "wording_check", "verdict"}.issubset(node_names)


def test_flow_clean_ad_full_pass_produces_ready_verdict():
    ad = sample_ad()
    rules = sample_retrieved_rules()
    mock_model = make_mock_model_client({
        "findings": [],
        "unknown": False,
        "needs_person": False,
    })
    conn = FakeDBConnection()

    result = run_ad_worker_flow(
        ad,
        retrieved_rules=rules,
        model_client=mock_model,
        connection=conn,
        run_page_checks=False,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
    )

    assert isinstance(result, WorkerGraphResult)
    assert result.verdict.status is VerdictStatus.READY
    assert len(result.findings) == 0
    assert result.step_count == 4  # mechanical, page_check, wording_check, verdict
    assert "Ready for launch" in result.verdict.summary
    assert result.shared_state.status == "ready"


def test_flow_mechanical_errors_produce_fix_needed():
    # Missing UTM in URL and headline too long
    ad = sample_ad(
        headline="A" * 60,
        destination_url="https://brightleaf.example/serum",  # No UTMs
    )
    rules = sample_retrieved_rules()
    mock_model = make_mock_model_client({
        "findings": [],
        "unknown": False,
        "needs_person": False,
    })
    conn = FakeDBConnection()

    result = run_ad_worker_flow(
        ad,
        retrieved_rules=rules,
        model_client=mock_model,
        connection=conn,
        run_page_checks=False,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
    )

    assert result.verdict.status is VerdictStatus.FIX_NEEDED
    assert len(result.findings) >= 2  # UTM missing + headline length
    assert result.shared_state.status == "fix needed"


def test_flow_step_cap_enforced_and_routes_to_verdict_early():
    ad = sample_ad()
    conn = FakeDBConnection()

    # Set initial step count to 1 and max_steps to 2
    # Node 1 (mechanical) runs -> step_count becomes 2.
    # Router sees step_count >= max_steps (2 >= 2) -> routes directly to verdict!
    result = run_ad_worker_flow(
        ad,
        initial_step_count=1,
        max_steps=2,
        connection=conn,
        run_page_checks=True,
        run_wording_checks=True,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
    )

    # Verdict node runs as step 3 (2 + 1)
    assert result.step_count == 3
    # Step cap was exceeded -> needs a person
    assert result.verdict.status is VerdictStatus.NEEDS_PERSON
    assert "Step cap exceeded" in result.verdict.summary


def test_flow_step_cap_zero_or_already_exhausted():
    ad = sample_ad()
    conn = FakeDBConnection()

    # Starts with step_count already at max_steps
    result = run_ad_worker_flow(
        ad,
        initial_step_count=5,
        max_steps=5,
        connection=conn,
        run_page_checks=True,
        run_wording_checks=True,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
    )

    assert result.verdict.status is VerdictStatus.NEEDS_PERSON
    assert "Step cap exceeded" in result.verdict.summary


def test_flow_wording_violation_reflected_in_verdict():
    ad = sample_ad(ad_text="Miracle cure for all blemishes permanently!")
    rules = sample_retrieved_rules()
    mock_model = make_mock_model_client({
        "findings": [
            {
                "check_type": "wording",
                "message": "Exaggerated claim violates honest tone.",
                "field": "ad_text",
                "proposed_fix": "Use natural claim.",
                "rule_id": "BL-BB-1.1",
            }
        ],
        "unknown": False,
        "needs_person": False,
    })
    conn = FakeDBConnection()

    result = run_ad_worker_flow(
        ad,
        retrieved_rules=rules,
        model_client=mock_model,
        connection=conn,
        run_page_checks=False,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
    )

    assert result.verdict.status is VerdictStatus.FIX_NEEDED
    assert any(f.rule_id == "BL-BB-1.1" for f in result.findings)


def test_flow_wording_unknown_claim_triggers_needs_person():
    ad = sample_ad()
    rules = sample_retrieved_rules()
    mock_model = make_mock_model_client({
        "findings": [],
        "unknown": True,
        "needs_person": True,
        "summary": "Glacial peptide claim cannot be verified.",
    })
    conn = FakeDBConnection()

    result = run_ad_worker_flow(
        ad,
        retrieved_rules=rules,
        model_client=mock_model,
        connection=conn,
        run_page_checks=False,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
    )

    assert result.verdict.status is VerdictStatus.NEEDS_PERSON
    assert result.needs_person is True


def test_flow_advisory_guard_floor_raises_verdict_to_needs_person():
    ad = sample_ad()
    rules = sample_retrieved_rules()
    mock_model = make_mock_model_client({"findings": [], "unknown": False, "needs_person": False})
    conn = FakeDBConnection()

    # Pass verdict_floor=NEEDS_PERSON from input guard
    result = run_ad_worker_flow(
        ad,
        retrieved_rules=rules,
        model_client=mock_model,
        connection=conn,
        run_page_checks=False,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        verdict_floor=VerdictStatus.NEEDS_PERSON,
    )

    assert result.verdict.status is VerdictStatus.NEEDS_PERSON
    assert result.verdict.finding_ids == []
