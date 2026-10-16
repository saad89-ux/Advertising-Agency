"""Google Gemini model client with cost ledger tracking and spend cap enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
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
    load_dotenv()
    key = os.environ.get("MODEL_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    key = key.strip()
    return bool(key and "replace" not in key.lower() and "placeholder" not in key.lower())


def get_model_api_key() -> str:
    """Load model API key from environment (MODEL_API_KEY or GEMINI_API_KEY)."""
    load_dotenv()
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
) -> ModelResponse:
    """Invoke Google Gemini, enforce spend caps, and record cost in PostgreSQL ledger.

    Raises SpendCapExceededError if spend caps are breached.
    """
    if not prompt or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if not client_id or not isinstance(client_id, str):
        raise ValueError("client_id must be a non-empty string")

    # 1. Pre-flight check: ensure current spend hasn't already reached cap
    check_spend_caps(ad_id=ad_id, additional_cost=0.0, connection=connection)

    # 2. Invoke model
    genai_client = client or create_gemini_client()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
    ) if system_instruction else None

    response = genai_client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=config,
    )

    # 3. Extract text and usage tokens
    text = response.text or ""
    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
    completion_tokens = getattr(usage, "candidates_token_count", 0) or 0

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
