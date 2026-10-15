"""Tests for model connection, cost ledger, and spend cap enforcement."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from apps.worker.cost_ledger import (
    DEFAULT_MODEL,
    SPEND_CAP_PER_ROW_DEFAULT,
    SPEND_CAP_TOTAL_DEFAULT,
    CostLedgerEntry,
    SpendCapExceededError,
    calculate_model_cost,
    check_spend_caps,
    get_ad_spend,
    get_spend_cap_per_row,
    get_spend_cap_total,
    get_total_spend,
    record_cost_entry,
)
from apps.worker.model_client import (
    ModelResponse,
    call_gemini_model,
    create_gemini_client,
    get_model_api_key,
)


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeCostDBConnection:
    """Simulates PostgreSQL cost_ledger table."""

    def __init__(self):
        self.calls: list[tuple[str, Any]] = []
        self.entries: list[dict[str, Any]] = []

    def execute(self, statement: str, params: Any = None):
        self.calls.append((statement, params))

        if "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger WHERE ad_id = %s" in statement:
            ad_id = params[0]
            ad_spend = sum(e["cost_usd"] for e in self.entries if e.get("ad_id") == ad_id)
            return FakeResult([(ad_spend,)])

        if "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger WHERE batch_id = %s" in statement:
            batch_id = params[0]
            batch_spend = sum(e["cost_usd"] for e in self.entries if e.get("batch_id") == batch_id)
            return FakeResult([(batch_spend,)])

        if "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger WHERE client_id = %s" in statement:
            client_id = params[0]
            client_spend = sum(e["cost_usd"] for e in self.entries if e.get("client_id") == client_id)
            return FakeResult([(client_spend,)])

        if "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger" in statement:
            total_spend = sum(e["cost_usd"] for e in self.entries)
            return FakeResult([(total_spend,)])

        if "INSERT INTO cost_ledger" in statement:
            self.entries.append(
                {
                    "entry_id": params[0],
                    "client_id": params[1],
                    "batch_id": params[2],
                    "ad_id": params[3],
                    "model_name": params[4],
                    "prompt_tokens": params[5],
                    "completion_tokens": params[6],
                    "total_tokens": params[7],
                    "cost_usd": float(params[8]),
                    "step_name": params[9],
                }
            )
            return FakeResult([])

        return FakeResult([])


# --- Cost Calculation Tests ---


def test_calculate_model_cost_gemini_flash():
    # 1,000 prompt tokens ($0.10/1M) = $0.0001
    # 500 completion tokens ($0.40/1M) = $0.0002
    # Total = $0.0003
    cost = calculate_model_cost("gemini-2.0-flash", 1000, 500)
    assert cost == pytest.approx(0.0003, abs=1e-6)


def test_cost_arithmetic_per_100_ads_under_prd_target():
    """Verify that average ad check (e.g. 800 in, 200 out) satisfies PRD < $0.25 / 100 ads."""
    cost_per_ad = calculate_model_cost("gemini-2.0-flash", 800, 200)
    cost_per_100 = cost_per_ad * 100
    # PRD Target is under $0.25 per 100 ads
    assert cost_per_100 < 0.25
    assert cost_per_100 == pytest.approx(0.016, abs=1e-3)


# --- Spend Cap In-Code Enforcement Tests ---


def test_spend_cap_per_row_exceeded():
    conn = FakeCostDBConnection()
    # Ad 1 has already spent $0.009
    conn.entries.append({"ad_id": "ad-1", "cost_usd": 0.009})

    # Attempting to add $0.002 brings total to $0.011, exceeding $0.01 cap
    with pytest.raises(SpendCapExceededError, match="Spend cap per row of \\$0.0100 exceeded"):
        check_spend_caps(ad_id="ad-1", additional_cost=0.002, connection=conn)


def test_total_assessment_spend_cap_exceeded():
    conn = FakeCostDBConnection()
    # System total has already reached $14.99
    conn.entries.append({"cost_usd": 14.99})

    # Adding $0.02 brings total to $15.01, exceeding $15.00 cap
    with pytest.raises(SpendCapExceededError, match="Total spend cap of \\$15.00 exceeded"):
        check_spend_caps(ad_id="ad-2", additional_cost=0.02, connection=conn)


def test_spend_caps_pass_when_under_limits():
    conn = FakeCostDBConnection()
    conn.entries.append({"ad_id": "ad-1", "cost_usd": 0.001})

    # Under both caps
    check_spend_caps(ad_id="ad-1", additional_cost=0.002, connection=conn)


def test_record_cost_entry_persists_in_ledger_and_enforces_caps():
    conn = FakeCostDBConnection()

    entry = record_cost_entry(
        client_id="brightleaf",
        model_name="gemini-2.0-flash",
        prompt_tokens=1000,
        completion_tokens=500,
        ad_id="ad-1",
        batch_id="batch-1",
        step_name="wording_check",
        connection=conn,
    )

    assert entry.client_id == "brightleaf"
    assert entry.prompt_tokens == 1000
    assert entry.completion_tokens == 500
    assert entry.total_tokens == 1500
    assert entry.cost_usd == pytest.approx(0.0003, abs=1e-6)
    assert len(conn.entries) == 1


def test_record_cost_entry_fails_when_cap_exceeded():
    conn = FakeCostDBConnection()
    conn.entries.append({"ad_id": "ad-1", "cost_usd": 0.0095})

    with pytest.raises(SpendCapExceededError):
        # 10,000 completion tokens -> $0.0040 cost -> brings ad spend to $0.0135 (> $0.01)
        record_cost_entry(
            client_id="brightleaf",
            model_name="gemini-2.0-flash",
            prompt_tokens=0,
            completion_tokens=10000,
            ad_id="ad-1",
            connection=conn,
        )


# --- Model Invocation & Client Tests ---


def test_call_gemini_model_success():
    conn = FakeCostDBConnection()

    # Mock Gemini client response
    mock_response = MagicMock()
    mock_response.text = '{"findings": []}'
    mock_response.usage_metadata.prompt_token_count = 500
    mock_response.usage_metadata.candidates_token_count = 150

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    res = call_gemini_model(
        prompt="Check wording against claims",
        client_id="brightleaf",
        ad_id="ad-1",
        batch_id="batch-1",
        client=mock_client,
        connection=conn,
    )

    assert res.text == '{"findings": []}'
    assert res.prompt_tokens == 500
    assert res.completion_tokens == 150
    assert res.total_tokens == 650
    assert res.cost_usd > 0.0
    assert res.cost_entry.ad_id == "ad-1"
    # Recorded in ledger
    assert len(conn.entries) == 1
    assert conn.entries[0]["ad_id"] == "ad-1"


def test_call_gemini_model_preflight_blocks_when_cap_already_reached():
    conn = FakeCostDBConnection()
    # Ad 1 has already hit $0.01
    conn.entries.append({"ad_id": "ad-1", "cost_usd": 0.0101})

    mock_client = MagicMock()

    with pytest.raises(SpendCapExceededError):
        call_gemini_model(
            prompt="Wording check",
            client_id="brightleaf",
            ad_id="ad-1",
            client=mock_client,
            connection=conn,
        )

    # Model was NEVER called because pre-flight check aborted execution
    mock_client.models.generate_content.assert_not_called()


# --- Aggregation & Environment Configuration Tests ---


def test_get_ad_spend_and_total_spend():
    conn = FakeCostDBConnection()
    conn.entries.append({"ad_id": "ad-1", "cost_usd": 0.002})
    conn.entries.append({"ad_id": "ad-1", "cost_usd": 0.003})
    conn.entries.append({"ad_id": "ad-2", "cost_usd": 0.004})

    assert get_ad_spend("ad-1", connection=conn) == pytest.approx(0.005)
    assert get_ad_spend("ad-2", connection=conn) == pytest.approx(0.004)
    assert get_total_spend(connection=conn) == pytest.approx(0.009)


def test_spend_cap_env_overrides(monkeypatch):
    assert get_spend_cap_per_row() == SPEND_CAP_PER_ROW_DEFAULT
    assert get_spend_cap_total() == SPEND_CAP_TOTAL_DEFAULT

    monkeypatch.setenv("SPEND_CAP_PER_ROW", "0.05")
    monkeypatch.setenv("SPEND_CAP_TOTAL", "25.0")

    assert get_spend_cap_per_row() == 0.05
    assert get_spend_cap_total() == 25.0


def test_get_model_api_key_refuses_empty_or_placeholder(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MODEL_API_KEY must be set"):
        get_model_api_key()

    monkeypatch.setenv("MODEL_API_KEY", "replace-with-your-key")
    with pytest.raises(RuntimeError, match="valid API key"):
        get_model_api_key()

    monkeypatch.setenv("MODEL_API_KEY", "valid-gemini-key-12345")
    assert get_model_api_key() == "valid-gemini-key-12345"


# --- Validation Error Tests ---


def test_record_cost_entry_validation_errors():
    with pytest.raises(ValueError, match="client_id"):
        record_cost_entry("", "gemini-2.0-flash", 100, 100)

    with pytest.raises(ValueError, match="model_name"):
        record_cost_entry("brightleaf", "", 100, 100)

    with pytest.raises(ValueError, match="token counts"):
        record_cost_entry("brightleaf", "gemini-2.0-flash", -1, 100)
