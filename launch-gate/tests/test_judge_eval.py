"""Tests for Task 1: Real & Fail-Closed LLM Judge (judge_eval.py)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest

from apps.worker.judge_eval import (
    JudgeEvaluationResult,
    JudgeEvaluationSchema,
    evaluate_with_llm_judge,
)
from packages.contracts import AdContract, CheckType, FindingContract


def sample_ad() -> AdContract:
    return AdContract(
        ad_id="ad-judge-1",
        batch_id="batch-1",
        client_id="brightleaf",
        row_number=1,
        platform="Facebook",
        headline="Radiant Aloe Serum",
        ad_text="Natural soothing aloe for daily hydration.",
        destination_url="https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=serum_jan",
        creative_file="bundle_v1_1080x1080.png",
        row_hash="rowhash",
        sheet_hash="sheethash",
    )


def sample_mechanical_finding(ad_id: str = "ad-judge-1") -> FindingContract:
    return FindingContract(
        finding_id=f"{ad_id}:utm_source",
        ad_id=ad_id,
        row_number=1,
        check_type=CheckType.MECHANICAL,
        message="Missing UTM parameter 'utm_source'.",
        field="destination_url",
        proposed_fix="Add ?utm_source=facebook",
    )


def sample_wording_finding(ad_id: str = "ad-judge-1") -> FindingContract:
    return FindingContract(
        finding_id=f"{ad_id}:wording:claim",
        ad_id=ad_id,
        row_number=1,
        check_type=CheckType.WORDING,
        message="Potentially ambiguous claim detected.",
        field="ad_text",
        proposed_fix="Qualify with 'clinical study showed'",
        rule_id="BL-BB-1.1",
    )


def make_mock_client(response_dict: dict, prompt_tokens: int = 120, completion_tokens: int = 60) -> MagicMock:
    client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(response_dict)
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = prompt_tokens
    mock_usage.candidates_token_count = completion_tokens
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


def test_empty_findings_returns_skipped():
    ad = sample_ad()
    result = evaluate_with_llm_judge(ad, findings=[])

    assert result.verdict == "SKIPPED"
    assert result.confidence_score == 1.0
    assert result.faithfulness_score == 1.0
    assert not result.needs_person
    assert not result.hallucinations_detected
    assert result.false_alarms_dismissed == []
    assert "judge skipped" in result.judge_reasoning.lower()


def test_fail_closed_when_api_key_missing(monkeypatch):
    monkeypatch.setattr("apps.worker.judge_eval.is_gemini_configured", lambda: False)

    ad = sample_ad()
    finding = sample_wording_finding()

    result = evaluate_with_llm_judge(ad, [finding], model_client=None)

    assert result.verdict == "REQUIRES_HUMAN"
    assert result.confidence_score == 0.0
    assert result.needs_person is True
    assert "MODEL_API_KEY is not configured" in result.judge_reasoning


def test_fail_closed_on_model_api_exception():
    ad = sample_ad()
    finding = sample_wording_finding()

    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("Quota exceeded or upstream timeout")

    result = evaluate_with_llm_judge(
        ad,
        [finding],
        model_client=client,
        connection=FakeDBConnection(),
    )

    assert result.verdict == "REQUIRES_HUMAN"
    assert result.confidence_score == 0.0
    assert result.needs_person is True
    assert "Judge unavailable:" in result.judge_reasoning
    assert "Quota exceeded" in result.judge_reasoning


def test_fail_closed_on_malformed_json_response():
    ad = sample_ad()
    finding = sample_wording_finding()

    client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "NOT VALID JSON {{"
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 50
    mock_usage.candidates_token_count = 10
    mock_resp.usage_metadata = mock_usage
    client.models.generate_content.return_value = mock_resp

    result = evaluate_with_llm_judge(
        ad,
        [finding],
        model_client=client,
        connection=FakeDBConnection(),
    )

    assert result.verdict == "REQUIRES_HUMAN"
    assert result.confidence_score == 0.0
    assert result.needs_person is True
    assert "Judge unavailable:" in result.judge_reasoning


def test_fail_closed_on_spend_cap_exceeded():
    from apps.worker.cost_ledger import SpendCapExceededError
    ad = sample_ad()
    finding = sample_wording_finding()

    client = MagicMock()
    client.models.generate_content.side_effect = SpendCapExceededError("Spend cap of $10.00 exceeded")

    result = evaluate_with_llm_judge(
        ad,
        [finding],
        model_client=client,
        connection=FakeDBConnection(),
    )

    assert result.verdict == "REQUIRES_HUMAN"
    assert result.confidence_score == 0.0
    assert result.needs_person is True
    assert "Judge unavailable:" in result.judge_reasoning
    assert "Spend cap" in result.judge_reasoning


def test_mechanical_finding_cannot_be_dismissed_by_llm():
    """Invariant: Mechanical violations (UTMs, character limits, ratios) cannot be overruled."""
    ad = sample_ad()
    mech_finding = sample_mechanical_finding()

    client = make_mock_client({
        "judge_verdict": "OVERRULED",
        "confidence_score": 0.95,
        "faithfulness_score": 0.95,
        "hallucinations_detected": False,
        "false_alarms_dismissed": [mech_finding.finding_id],
        "remediation_approved": True,
        "judge_reasoning": "I think UTMs are optional for this creative.",
    })

    result = evaluate_with_llm_judge(
        ad,
        [mech_finding],
        model_client=client,
        connection=FakeDBConnection(),
    )

    # Dismissal must be rejected, mechanical finding preserved, needs_person set to True
    assert mech_finding.finding_id not in result.false_alarms_dismissed
    assert result.needs_person is True


def test_unknown_finding_id_marks_needs_person():
    """If the LLM returns an invented/hallucinated finding ID, flag for human review."""
    ad = sample_ad()
    real_finding = sample_wording_finding()

    client = make_mock_client({
        "judge_verdict": "CONFIRMED",
        "confidence_score": 0.92,
        "faithfulness_score": 0.95,
        "hallucinations_detected": False,
        "false_alarms_dismissed": ["ghost_finding_id_not_in_input"],
        "remediation_approved": True,
        "judge_reasoning": "Dismissed ghost finding.",
    })

    result = evaluate_with_llm_judge(
        ad,
        [real_finding],
        model_client=client,
        connection=FakeDBConnection(),
    )

    assert result.needs_person is True
    assert result.false_alarms_dismissed == []


def test_empty_reasoning_rejects_dismissal():
    ad = sample_ad()
    wording_finding = sample_wording_finding()

    client = make_mock_client({
        "judge_verdict": "OVERRULED",
        "confidence_score": 0.95,
        "faithfulness_score": 0.95,
        "hallucinations_detected": False,
        "false_alarms_dismissed": [wording_finding.finding_id],
        "remediation_approved": True,
        "judge_reasoning": "    ",  # Whitespace only
    })

    result = evaluate_with_llm_judge(
        ad,
        [wording_finding],
        model_client=client,
        connection=FakeDBConnection(),
    )

    assert wording_finding.finding_id not in result.false_alarms_dismissed
    assert result.needs_person is True


def test_valid_dismissal_of_non_mechanical_finding():
    ad = sample_ad()
    wording_finding = sample_wording_finding()

    client = make_mock_client({
        "judge_verdict": "OVERRULED",
        "confidence_score": 0.95,
        "faithfulness_score": 0.98,
        "hallucinations_detected": False,
        "false_alarms_dismissed": [wording_finding.finding_id],
        "remediation_approved": True,
        "judge_reasoning": "The claim is substantiated by retrieved rule BL-C-01 in claims register.",
    })

    result = evaluate_with_llm_judge(
        ad,
        [wording_finding],
        model_client=client,
        connection=FakeDBConnection(),
    )

    assert result.verdict == "OVERRULED"
    assert wording_finding.finding_id in result.false_alarms_dismissed
    assert result.needs_person is False


def test_low_confidence_marks_needs_person():
    ad = sample_ad()
    wording_finding = sample_wording_finding()

    client = make_mock_client({
        "judge_verdict": "CONFIRMED",
        "confidence_score": 0.70,  # Below 0.85 threshold
        "faithfulness_score": 0.90,
        "hallucinations_detected": False,
        "false_alarms_dismissed": [],
        "remediation_approved": True,
        "judge_reasoning": "Ambiguous policy phrasing.",
    })

    result = evaluate_with_llm_judge(
        ad,
        [wording_finding],
        model_client=client,
        connection=FakeDBConnection(),
    )

    assert result.needs_person is True


def test_hallucination_detected_marks_needs_person():
    ad = sample_ad()
    wording_finding = sample_wording_finding()

    client = make_mock_client({
        "judge_verdict": "CONFIRMED",
        "confidence_score": 0.95,
        "faithfulness_score": 0.60,
        "hallucinations_detected": True,
        "false_alarms_dismissed": [],
        "remediation_approved": True,
        "judge_reasoning": "Worker cited text that does not exist in the ad.",
    })

    result = evaluate_with_llm_judge(
        ad,
        [wording_finding],
        model_client=client,
        connection=FakeDBConnection(),
    )

    assert result.needs_person is True
