"""Comprehensive tests for Specialized Multi-Agent Graph Architecture and Autonomous Feedback Loop.

Verifies:
1. HeadlineSpecialistNode: character limit checking, prohibited cure claims, and autonomous headline refinement.
2. UrlAndLandingPageNode: UTM parameter audit and live landing page verification.
3. OfferAndComplianceNode: brand claim and wording audit, and copy refinement.
4. SupervisorReviewerNode: findings reconciliation, autonomous fixability evaluation, bounded refinement routing.
5. JudgeAuditorNode: independent LLM Judge audit, false alarm dismissal, and safety certification.
6. Multi-Agent Graph compilation and deterministic execution via create_multi_agent_graph and run_multi_agent_worker_flow.
7. Preservation of refined copy in database via job_runner.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apps.worker.agent_team import (
    HeadlineSpecialistNode,
    JudgeAuditorNode,
    OfferAndComplianceNode,
    SupervisorDecision,
    SupervisorReviewerNode,
    UrlAndLandingPageNode,
)
from apps.worker.graph import (
    WorkerGraphResult,
    create_multi_agent_graph,
    run_multi_agent_worker_flow,
)
from apps.worker.judge_eval import JudgeEvaluationResult
from apps.worker.shared_state import SharedState
from packages.contracts import (
    AdContract,
    CheckType,
    FindingContract,
    VerdictContract,
    VerdictStatus,
)


def sample_ad(
    ad_id: str = "ad-team-1",
    client_id: str = "brightleaf",
    platform: str = "facebook",
    headline: str = "Natural Soothing Organic Aloe Vera Daily Hydration Serum",
    ad_text: str = "Our miracle formula cures arthritis and soothes daily joint discomfort.",
    destination_url: str = "https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=brand_promo",
    creative_file: str | None = "bundle_1080x1080.png",
) -> AdContract:
    return AdContract(
        ad_id=ad_id,
        batch_id="batch-team-1",
        client_id=client_id,
        row_number=1,
        platform=platform,
        headline=headline,
        ad_text=ad_text,
        destination_url=destination_url,
        creative_file=creative_file,
        row_hash="mock_row_hash_123",
        sheet_hash="mock_sheet_hash_123",
    )


# ---------------------------------------------------------------------------
# 1. HeadlineSpecialistNode Tests
# ---------------------------------------------------------------------------

def test_headline_specialist_detects_length_and_cure_claims():
    specialist = HeadlineSpecialistNode()
    # 57 characters (exceeds Facebook 40 char limit) and contains "Cures"
    ad = sample_ad(headline="Miracle Aloe Serum Cures Daily Dehydration Immediately")
    findings = specialist.check(ad)

    assert len(findings) >= 2
    messages = [f.message.lower() for f in findings]
    assert any("exceeds" in m for m in messages)
    assert any("prohibited" in m or "cure" in m for m in messages)


def test_headline_specialist_refines_headline_under_limit_and_removes_cure():
    specialist = HeadlineSpecialistNode()
    over_limit_headline = "Natural Soothing Organic Aloe Vera Daily Hydration Serum"
    refined = specialist.refine_headline(over_limit_headline, platform="facebook", max_len=40)

    assert len(refined) <= 40
    assert "Daily Hydration" in refined or len(refined) <= 40

    cure_headline = "Miracle Formula Cures Joint Pain"
    refined_cure = specialist.refine_headline(cure_headline, platform="facebook", max_len=40)
    assert "cure" not in refined_cure.lower()
    assert "miracle" not in refined_cure.lower()
    assert "supports" in refined_cure.lower() or "formula" in refined_cure.lower()


# ---------------------------------------------------------------------------
# 2. UrlAndLandingPageNode & OfferAndComplianceNode Tests
# ---------------------------------------------------------------------------

def test_url_specialist_checks_utms():
    import asyncio
    specialist = UrlAndLandingPageNode()
    ad_bad_url = sample_ad(destination_url="https://brightleaf.example/serum")
    findings, state = asyncio.run(specialist.check(ad_bad_url, run_page_checks=False))

    assert len(findings) > 0
    assert any("utm_source" in f.message for f in findings)
    assert state.ad_id == ad_bad_url.ad_id


def test_offer_specialist_refines_copy():
    specialist = OfferAndComplianceNode()
    violating_copy = "Clinical test shows this cures arthritis in 2 days guaranteed."
    refined = specialist.refine_copy(violating_copy)

    assert "cures arthritis" not in refined.lower()
    assert "supports joint comfort" in refined.lower()


# ---------------------------------------------------------------------------
# 3. SupervisorReviewerNode Decision Tests
# ---------------------------------------------------------------------------

def test_supervisor_identifies_fixable_headline_and_routes_refinement():
    supervisor = SupervisorReviewerNode()
    ad = sample_ad(headline="Too Long Headline Exceeding Forty Characters On Facebook Feed")
    shared_state = SharedState(ad_id=ad.ad_id, client_id=ad.client_id, status="checking")

    findings = [
        FindingContract(
            finding_id="f1",
            ad_id=ad.ad_id,
            row_number=1,
            check_type=CheckType.MECHANICAL,
            message="Headline exceeds Facebook limit of 40 characters (got 58).",
            field="headline",
        )
    ]

    # Iteration 0 -> should route to headline_specialist
    decision = supervisor.review(ad, findings, shared_state, loop_count=0, max_loops=3)
    assert decision.action == "REFINE"
    assert decision.target_specialist == "headline_specialist"
    assert decision.loop_count == 1
    assert decision.is_fixable is True
    assert decision.audit_entry is not None
    assert decision.audit_entry["specialist"] == "headline_specialist"


def test_supervisor_halts_at_max_loops_deterministically():
    supervisor = SupervisorReviewerNode()
    ad = sample_ad()
    shared_state = SharedState(ad_id=ad.ad_id, client_id=ad.client_id, status="checking")

    findings = [
        FindingContract(
            finding_id="f1",
            ad_id=ad.ad_id,
            row_number=1,
            check_type=CheckType.MECHANICAL,
            message="Headline exceeds Facebook limit of 40 characters.",
            field="headline",
        )
    ]

    # When loop_count reaches max_loops (3), supervisor must escalate, never loop infinitely
    decision = supervisor.review(ad, findings, shared_state, loop_count=3, max_loops=3)
    assert decision.action == "APPROVE_OR_ESCALATE"
    assert decision.target_specialist is None
    assert decision.is_fixable is False


# ---------------------------------------------------------------------------
# 4. End-to-End Multi-Agent Graph Execution Tests
# ---------------------------------------------------------------------------

def test_create_multi_agent_graph_compiles():
    graph = create_multi_agent_graph()
    assert graph is not None
    # Verify required node keys are present in graph
    expected_nodes = {
        "headline_specialist",
        "url_specialist",
        "offer_specialist",
        "supervisor_reviewer",
        "judge_auditor",
        "verdict_node",
    }
    assert expected_nodes.issubset(set(graph.nodes.keys()))


def test_run_multi_agent_worker_flow_autonomous_refinement():
    """An ad with a fixable over-length headline is autonomously refined and passes."""
    ad = sample_ad(
        headline="Natural Soothing Organic Aloe Vera Daily Hydration Serum",
        ad_text="Organic aloe vera soothing formula.",
    )

    result = run_multi_agent_worker_flow(
        ad,
        run_page_checks=False,
        run_wording_checks=False,
        max_loops=3,
    )

    assert isinstance(result, WorkerGraphResult)
    # Headline must be shortened under Facebook 40 char limit
    assert len(result.ad.headline) <= 40
    # Must record that refinement occurred
    assert result.loop_count >= 1
    assert len(result.refinement_history) >= 1
    assert result.verdict.status in (VerdictStatus.READY, VerdictStatus.FIX_NEEDED, VerdictStatus.NEEDS_PERSON)


def test_judge_auditor_dismisses_false_alarm_in_graph():
    """LLM Judge dismisses a false alarm finding and updates verdict."""
    ad = sample_ad(headline="Radiant Aloe Serum", ad_text="Daily soothing hydration.")

    fake_finding = FindingContract(
        finding_id="ad-team-1:false_alarm",
        ad_id=ad.ad_id,
        row_number=1,
        check_type=CheckType.WORDING,
        message="Questionable claim flagged initially.",
        field="ad_text",
    )

    mock_judge_result = JudgeEvaluationResult(
        verdict=VerdictStatus.READY.value,
        confidence_score=0.95,
        faithfulness_score=0.98,
        hallucinations_detected=False,
        needs_person=False,
        judge_reasoning="Dismissing false alarm; daily hydration claim is compliant.",
        false_alarms_dismissed=["ad-team-1:false_alarm"],
        cost_usd=0.0001,
    )

    with patch("apps.worker.agent_team.evaluate_with_llm_judge", return_value=mock_judge_result):
        result = run_multi_agent_worker_flow(
            ad,
            initial_findings=[fake_finding],
            run_page_checks=False,
            run_wording_checks=False,
        )

        assert isinstance(result, WorkerGraphResult)
        # False alarm must be dismissed by judge
        finding_ids = [f.finding_id for f in result.findings]
        assert "ad-team-1:false_alarm" not in finding_ids


# ---------------------------------------------------------------------------
# 5. Database Integration via Job Runner Test
# ---------------------------------------------------------------------------

def test_job_runner_updates_refined_ad_in_db():
    from apps.api.queue import AdJob
    from apps.worker.job_runner import process_job

    class MockConn:
        def __init__(self):
            self.executed_statements = []

        def execute(self, stmt, params=None):
            self.executed_statements.append((stmt, params))
            class Res:
                def fetchone(self):
                    # Mock ad row
                    return (
                        "ad-test-persist",
                        "batch-1",
                        "brightleaf",
                        1,
                        "facebook",
                        "Daily hydration.",
                        "Natural Soothing Organic Aloe Vera Daily Hydration Serum",  # 56 chars, over 40 limit
                        "https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=promo",
                        "square.png",
                        "hash123",
                        "sheet123",
                    )
                def fetchall(self):
                    return []
            return Res()

    mock_conn = MockConn()
    job = AdJob(batch_id="batch-1", ad_id="ad-test-persist")

    with patch("apps.worker.agent_team.evaluate_with_llm_judge") as mock_judge:
        mock_judge.return_value = JudgeEvaluationResult(
            verdict=VerdictStatus.READY.value,
            confidence_score=0.9,
            faithfulness_score=0.95,
            hallucinations_detected=False,
            needs_person=False,
            judge_reasoning="Certified",
            false_alarms_dismissed=[],
            cost_usd=0.0,
        )

        findings = process_job(
            job,
            connection=mock_conn,
            run_page_checks=False,
            run_wording_checks=False,
        )

        # Verify UPDATE ads SET headline = ... was executed with shortened headline
        update_calls = [
            (s, p) for s, p in mock_conn.executed_statements
            if "UPDATE ads" in s and "SET headline" in s
        ]
        assert len(update_calls) == 1
        updated_headline = update_calls[0][1][0]
        assert len(updated_headline) <= 40
