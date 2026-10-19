"""Tests for Task 2: Fail-Closed Wording Check, Verbatim Quote Verification, and Pre-filter Rules."""

from __future__ import annotations

import json
from unittest.mock import MagicMock
import pytest

from apps.worker.cost_ledger import CostLedgerEntry, SpendCapExceededError
from apps.worker.retrieval import RetrievedRule
from apps.worker.wording_check import (
    DEFAULT_SPECIALIST_MODEL,
    WordingCheckResult,
    check_wording,
    run_keyword_prefilter,
    verify_quote_and_citation,
)
from packages.contracts import AdContract, CheckType, FindingContract


def sample_ad(**overrides) -> AdContract:
    values = {
        "ad_id": "ad-wording-fail-closed-1",
        "batch_id": "batch-1",
        "client_id": "brightleaf",
        "row_number": 1,
        "platform": "Facebook",
        "ad_text": "Gentle daily hydration with organic aloe vera serum for radiant skin.",
        "headline": "Pure Botanicals",
        "destination_url": "https://brightleaf.example/serum",
        "creative_file": None,
        "row_hash": "row-hash",
        "sheet_hash": "sheet-hash",
    }
    values.update(overrides)
    return AdContract(**values)


def sample_retrieved_rules() -> list[RetrievedRule]:
    return [
        RetrievedRule(
            client_id="brightleaf",
            entry_number="BL-BB-1.1",
            source_file="brand_book.md",
            content="Brand Voice: Warm, natural, honest. Never exaggerate benefits or make miracle cure claims.",
            distance=0.08,
        ),
        RetrievedRule(
            client_id="brightleaf",
            entry_number="BL-C-01",
            source_file="claims_register.md",
            content="Claim: Contains organic aloe vera and botanical hyaluronic acid.",
            distance=0.12,
        ),
    ]


class FakeCostDBConnection:
    """Mock database connection for spend cap queries."""

    def __init__(self, current_total: float = 0.0, current_ad: float = 0.0):
        self.current_total = current_total
        self.current_ad = current_ad
        self.entries: list[dict] = []

    def execute(self, statement: str, params=None):
        if "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger WHERE ad_id = %s" in statement:
            return FakeResult([(self.current_ad,)])
        if "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger" in statement:
            return FakeResult([(self.current_total,)])
        if "INSERT INTO cost_ledger" in statement:
            self.entries.append({"params": params})
            return FakeResult([])
        return FakeResult([])


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def make_mock_client(response_dict: dict, prompt_tokens: int = 200, completion_tokens: int = 50) -> MagicMock:
    client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(response_dict)
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = prompt_tokens
    mock_usage.candidates_token_count = completion_tokens
    mock_resp.usage_metadata = mock_usage
    client.models.generate_content.return_value = mock_resp
    return client


# --- Tests for Failure Paths (Fail-Closed) ---


def test_wording_check_fails_closed_when_api_key_missing(monkeypatch):
    """When MODEL_API_KEY is not configured and no client passed, return model_unavailable and needs_person=True."""
    monkeypatch.setattr("apps.worker.wording_check.is_gemini_configured", lambda: False)

    ad = sample_ad()
    rules = sample_retrieved_rules()
    conn = FakeCostDBConnection()

    result = check_wording(
        ad,
        retrieved_rules=rules,
        model_client=None,
        connection=conn,
    )

    assert result.needs_person is True
    assert result.status == "needs_person"
    assert any(f.finding_id.endswith(":wording:model_unavailable") for f in result.findings)
    assert any("MODEL_API_KEY is not configured" in f.message for f in result.findings)


def test_wording_check_fails_closed_on_model_api_exception():
    """When Gemini API call throws (e.g. 503, 429 Quota), return model_unavailable and needs_person=True."""
    ad = sample_ad()
    rules = sample_retrieved_rules()
    conn = FakeCostDBConnection()

    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("ResourceExhausted: 429 Quota exceeded")

    result = check_wording(
        ad,
        retrieved_rules=rules,
        model_client=client,
        connection=conn,
    )

    assert result.model_unavailable is True
    assert result.status == "model unavailable"
    assert any(f.finding_id.endswith(":wording:model_unavailable") for f in result.findings)
    assert any("429 Quota exceeded" in f.message for f in result.findings)


def test_wording_check_fails_closed_on_malformed_json_response():
    """When model returns broken JSON, fail closed with needs_person=True."""
    ad = sample_ad()
    rules = sample_retrieved_rules()
    conn = FakeCostDBConnection()

    client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "NOT VALID JSON {{"
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 50
    mock_usage.candidates_token_count = 10
    mock_resp.usage_metadata = mock_usage
    client.models.generate_content.return_value = mock_resp

    result = check_wording(
        ad,
        retrieved_rules=rules,
        model_client=client,
        connection=conn,
    )

    assert result.needs_person is True
    assert result.status == "needs_person"
    assert any(f.finding_id.endswith(":wording:model_unavailable") for f in result.findings)


def test_wording_check_fails_closed_on_spend_cap_exceeded():
    """When spend cap is exceeded, return spend_cap_exceeded and needs_person=True."""
    ad = sample_ad()
    rules = sample_retrieved_rules()
    conn = FakeCostDBConnection()

    client = MagicMock()
    client.models.generate_content.side_effect = SpendCapExceededError("Spend cap of $15.00 exceeded")

    result = check_wording(
        ad,
        retrieved_rules=rules,
        model_client=client,
        connection=conn,
    )

    assert result.needs_person is True
    assert result.status == "needs_person"
    assert any(f.finding_id.endswith(":wording:spend_cap_exceeded") for f in result.findings)


# --- Tests for Verbatim Quote and Citation Verification ---


def test_wording_check_drops_finding_with_unverified_quote():
    """If the model hallucinates a quote not verbatim in the ad, drop finding and flag needs_person=True."""
    ad = sample_ad(ad_text="Gentle daily hydration with organic aloe vera serum.")
    rules = sample_retrieved_rules()
    conn = FakeCostDBConnection()

    client = make_mock_client({
        "findings": [
            {
                "check_type": "wording",
                "message": "Claims to cure all aging blemishes completely.",
                "field": "ad_text",
                "proposed_fix": "Remove cure claim",
                "rule_id": "BL-BB-1.1",
                "quote": "cure all aging blemishes completely",  # NOT in ad text!
            }
        ],
        "unknown": False,
        "needs_person": False,
    })

    result = check_wording(
        ad,
        retrieved_rules=rules,
        model_client=client,
        connection=conn,
    )

    # Finding with fabricated quote must be dropped
    assert result.needs_person is True
    assert not any(f.message == "Claims to cure all aging blemishes completely." for f in result.findings)
    assert any("unverified_quote" in f.finding_id for f in result.findings)


def test_wording_check_accepts_finding_with_verbatim_quote():
    """When the quote is a verbatim substring of ad text and rule is valid, finding is accepted."""
    ad = sample_ad(ad_text="Experience miracle transformation overnight with pure botanicals.")
    rules = sample_retrieved_rules()
    conn = FakeCostDBConnection()

    client = make_mock_client({
        "findings": [
            {
                "check_type": "wording",
                "message": "Exaggerated transformation claim violates brand tone.",
                "field": "ad_text",
                "proposed_fix": "State realistic results",
                "rule_id": "BL-BB-1.1",
                "quote": "miracle transformation overnight",  # Verbatim substring!
            }
        ],
        "unknown": False,
        "needs_person": False,
    })

    result = check_wording(
        ad,
        retrieved_rules=rules,
        model_client=client,
        connection=conn,
    )

    accepted_wording = [f for f in result.findings if f.rule_id == "BL-BB-1.1"]
    assert len(accepted_wording) >= 1
    assert any("Exaggerated transformation" in f.message for f in accepted_wording)


def test_wording_check_drops_finding_with_unverified_citation():
    """If the model cites a non-existent or unretrieved rule ID, drop finding and flag needs_person=True."""
    ad = sample_ad(ad_text="Gentle daily hydration with organic aloe vera serum.")
    rules = sample_retrieved_rules()
    conn = FakeCostDBConnection()

    client = make_mock_client({
        "findings": [
            {
                "check_type": "wording",
                "message": "Invalid claim detected",
                "field": "ad_text",
                "proposed_fix": "Fix wording",
                "rule_id": "BL-INVENTED-999",  # Not in retrieved rules!
                "quote": "daily hydration",
            }
        ],
        "unknown": False,
        "needs_person": False,
    })

    result = check_wording(
        ad,
        retrieved_rules=rules,
        model_client=client,
        connection=conn,
    )

    assert result.needs_person is True
    assert any("unverified_citation" in f.finding_id for f in result.findings)
    assert not any(f.rule_id == "BL-INVENTED-999" for f in result.findings)


# --- Tests for Deterministic Keyword Pre-filter ---


def test_keyword_prefilter_labels_findings_with_source_keyword_rule():
    """Keyword pre-filter runs deterministically and marks source=keyword_rule."""
    ad = sample_ad(ad_text="This is a miracle cure guaranteed to work instantly.")
    rules = sample_retrieved_rules()

    findings = run_keyword_prefilter(ad, rules)

    assert len(findings) >= 1
    for f in findings:
        assert f.check_type == CheckType.WORDING
        assert "source=keyword_rule" in f.message
        assert f.finding_id.endswith(":keyword")


def test_specialist_model_env_var_is_used(monkeypatch):
    """The specialist model configured in SPECIALIST_MODEL is passed to the model client."""
    monkeypatch.setenv("SPECIALIST_MODEL", "gemini-3.5-flash-lite")

    ad = sample_ad()
    rules = sample_retrieved_rules()
    conn = FakeCostDBConnection()

    client = make_mock_client({"findings": [], "unknown": False, "needs_person": False})

    check_wording(
        ad,
        retrieved_rules=rules,
        model_client=client,
        connection=conn,
    )

    # Verify that the model argument used was gemini-3.5-flash-lite
    call_args = client.models.generate_content.call_args
    assert call_args is not None
    assert call_args.kwargs.get("model") == "gemini-3.5-flash-lite"
