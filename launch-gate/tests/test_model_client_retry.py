"""Unit tests for bounded retry logic in model_client.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from apps.worker.model_client import (
    call_gemini_model,
    get_retry_delay,
    is_retryable_error,
)


class MockRateLimitError(Exception):
    def __init__(self, message: str = "ResourceExhausted: 429 quota exceeded", retry_after: str | None = None):
        super().__init__(message)
        self.code = 429
        self.headers = {"retry-after": retry_after} if retry_after else {}


class MockUnavailableError(Exception):
    def __init__(self, message: str = "503 Service Unavailable"):
        super().__init__(message)
        self.code = 503
        self.headers = {}


class MockBadRequestError(Exception):
    def __init__(self, message: str = "400 Invalid argument"):
        super().__init__(message)
        self.code = 400
        self.headers = {}


def test_is_retryable_error_detects_codes_and_messages():
    assert is_retryable_error(MockRateLimitError()) is True
    assert is_retryable_error(MockUnavailableError()) is True
    assert is_retryable_error(TimeoutError("Connection timed out")) is True
    assert is_retryable_error(ConnectionError("Connection reset by peer")) is True
    assert is_retryable_error(Exception("429 Too Many Requests")) is True
    assert is_retryable_error(Exception("503 Backend unavailable")) is True

    # Non-retryable
    assert is_retryable_error(MockBadRequestError()) is False
    assert is_retryable_error(ValueError("Invalid argument format")) is False


def test_get_retry_delay_respects_retry_after():
    err_with_header = MockRateLimitError(retry_after="2.5")
    delay = get_retry_delay(err_with_header, attempt=1)
    assert delay == 2.5

    err_without_header = MockRateLimitError()
    delay_exp = get_retry_delay(err_without_header, attempt=2, base_delay=0.5)
    # 0.5 * 2^1 + jitter(0.05..0.25) -> between 1.05 and 1.25
    assert 1.0 <= delay_exp <= 2.0


@patch("apps.worker.model_client.time.sleep")
@patch("apps.worker.model_client.record_cost_entry")
@patch("apps.worker.model_client.check_spend_caps")
def test_retry_success_after_two_429s(mock_spend, mock_record, mock_sleep):
    mock_client = MagicMock()
    
    mock_response = MagicMock()
    mock_response.text = '{"findings": []}'
    mock_response.usage_metadata.prompt_token_count = 100
    mock_response.usage_metadata.candidates_token_count = 20
    mock_response.usage_metadata.total_token_count = 120

    # Fail twice with 429, then succeed on 3rd attempt
    mock_client.models.generate_content.side_effect = [
        MockRateLimitError(retry_after="0.1"),
        MockRateLimitError(retry_after="0.1"),
        mock_response,
    ]

    resp = call_gemini_model(
        prompt="Test prompt",
        ad_id="ad_test_retry",
        client_id="brightleaf",
        model_name="gemini-3.5-flash-lite",
        client=mock_client,
    )

    assert resp.text == '{"findings": []}'
    assert mock_client.models.generate_content.call_count == 3
    assert mock_sleep.call_count == 2
    # Verify cost is recorded exactly once (no double-counting)
    assert mock_record.call_count == 1


@patch("apps.worker.model_client.time.sleep")
@patch("apps.worker.model_client.record_cost_entry")
@patch("apps.worker.model_client.check_spend_caps")
def test_retry_exhaustion_fails_closed_without_recording_cost(mock_spend, mock_record, mock_sleep):
    mock_client = MagicMock()
    
    # Fail 3 consecutive times with 429
    mock_client.models.generate_content.side_effect = [
        MockRateLimitError(),
        MockRateLimitError(),
        MockRateLimitError(),
    ]

    with pytest.raises(MockRateLimitError):
        call_gemini_model(
            prompt="Test prompt",
            ad_id="ad_test_retry_fail",
            client_id="brightleaf",
            model_name="gemini-3.5-flash-lite",
            client=mock_client,
        )

    assert mock_client.models.generate_content.call_count == 3
    assert mock_sleep.call_count == 2
    # Verify zero cost is recorded when call fails
    assert mock_record.call_count == 0


@patch("apps.worker.model_client.time.sleep")
@patch("apps.worker.model_client.record_cost_entry")
@patch("apps.worker.model_client.check_spend_caps")
def test_non_retryable_error_fails_immediately(mock_spend, mock_record, mock_sleep):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = MockBadRequestError("400 Bad Request")

    with pytest.raises(MockBadRequestError):
        call_gemini_model(
            prompt="Test prompt",
            ad_id="ad_test_bad_request",
            client_id="brightleaf",
            model_name="gemini-3.5-flash-lite",
            client=mock_client,
        )

    # Immediately fails, does not retry
    assert mock_client.models.generate_content.call_count == 1
    assert mock_sleep.call_count == 0
    assert mock_record.call_count == 0
