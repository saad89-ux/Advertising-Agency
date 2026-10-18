"""Google Gemini model client with cost ledger tracking and spend cap enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import random
import re
import time
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

from apps.worker.cost_ledger import (
    DEFAULT_MODEL,
    CostLedgerEntry,
    check_spend_caps,
    record_cost_entry,
)

logger = logging.getLogger(__name__)


def is_retryable_error(exc: Exception) -> bool:
    """Check if an exception is a 429 RateLimit/Quota, 503 Unavailable, or network/timeout error."""
    msg = str(exc).lower()
    status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status_code in {429, 503}:
        return True
    if any(code in msg for code in ["429", "503", "resource_exhausted", "resourceexhausted", "unavailable", "timeout", "timed out", "connection error"]):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def get_retry_delay(exc: Exception, attempt: int, base_delay: float = 0.5) -> float:
    """Extract Retry-After header or calculate exponential backoff with jitter."""
    headers = getattr(exc, "headers", {}) or {}
    retry_after = None
    if isinstance(headers, dict):
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after is not None:
        try:
            val = float(retry_after)
            if val > 0:
                return min(val, 10.0)
        except ValueError:
            pass

    delay = (base_delay * (2 ** (attempt - 1))) + random.uniform(0.05, 0.25)
    return min(delay, 5.0)


@dataclass(frozen=True)
class ModelResponse:
    """Model execution response with usage and cost tracking metadata."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    cost_entry: CostLedgerEntry


def is_gemini_configured() -> bool:
    """Check if a valid Gemini API key is configured in the environment."""
    key = os.environ.get("MODEL_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    key = key.strip()
    return bool(key and "replace" not in key.lower() and "placeholder" not in key.lower())


def get_model_api_key() -> str:
    """Load model API key from environment (MODEL_API_KEY or GEMINI_API_KEY)."""
    key = os.environ.get("MODEL_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    key = key.strip()
    if not is_gemini_configured():
        raise RuntimeError(
            "MODEL_API_KEY must be set in the environment or .env file with a valid API key"
        )
    return key


def create_gemini_client(api_key: str | None = None) -> genai.Client:
    """Create an authenticated Google GenAI client instance."""
    resolved_key = api_key or get_model_api_key()
    return genai.Client(api_key=resolved_key)


def call_gemini_model(
    prompt: str,
    *,
    client_id: str,
    ad_id: str | None = None,
    batch_id: str | None = None,
    step_name: str = "wording_check",
    model_name: str = DEFAULT_MODEL,
    client: Any = None,
    connection: Any = None,
    system_instruction: str | None = None,
    temperature: float | None = None,
    response_mime_type: str | None = None,
) -> ModelResponse:
    """Invoke Google Gemini with bounded retry, enforce spend caps, and record cost in PostgreSQL ledger.

    Raises SpendCapExceededError if spend caps are breached.
    """
    if not prompt or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if not client_id or not isinstance(client_id, str):
        raise ValueError("client_id must be a non-empty string")

    # 1. Pre-flight check: ensure current spend hasn't already reached cap
    check_spend_caps(ad_id=ad_id, additional_cost=0.0, connection=connection)

    # 2. Invoke model with bounded retry (max 3 attempts for 429/503/network)
    genai_client = client or create_gemini_client()
    config_kwargs: dict[str, Any] = {}
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if response_mime_type:
        config_kwargs["response_mime_type"] = response_mime_type

    config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

    max_attempts = 3
    response = None
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = genai_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config,
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts and is_retryable_error(exc):
                delay = get_retry_delay(exc, attempt)
                logger.warning(
                    "Model invocation attempt %d/%d failed with retryable error (%s). Retrying in %.2fs...",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                raise last_exc

    if response is None and last_exc is not None:
        raise last_exc

    # 3. Extract text and usage tokens (including thinking/reasoning tokens as output tokens)
    text = response.text or ""
    usage = getattr(response, "usage_metadata", None)
    raw_prompt_tokens = getattr(usage, "prompt_token_count", 0)
    prompt_tokens = raw_prompt_tokens if isinstance(raw_prompt_tokens, int) else 0
    raw_candidates_tokens = getattr(usage, "candidates_token_count", 0)
    candidates_tokens = raw_candidates_tokens if isinstance(raw_candidates_tokens, int) else 0
    raw_thoughts_tokens = getattr(usage, "thoughts_token_count", 0)
    thoughts_tokens = raw_thoughts_tokens if isinstance(raw_thoughts_tokens, int) else 0
    raw_total_tokens = getattr(usage, "total_token_count", None)
    total_tokens_reported = raw_total_tokens if isinstance(raw_total_tokens, int) else (prompt_tokens + candidates_tokens + thoughts_tokens)

    # Thinking/reasoning tokens are billed as output tokens
    completion_tokens = max(
        candidates_tokens,
        total_tokens_reported - prompt_tokens,
        candidates_tokens + thoughts_tokens,
    )

    # 4. Record to cost ledger (enforces spend cap during persistence)
    cost_entry = record_cost_entry(
        client_id=client_id,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        ad_id=ad_id,
        batch_id=batch_id,
        step_name=step_name,
        connection=connection,
    )

    return ModelResponse(
        text=text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=cost_entry.total_tokens,
        cost_usd=cost_entry.cost_usd,
        cost_entry=cost_entry,
    )
