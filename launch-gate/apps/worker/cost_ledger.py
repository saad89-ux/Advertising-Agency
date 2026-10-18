"""Cost ledger and spend cap enforcement for model calls."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
import logging
import os
from typing import Any
import uuid

from apps.api.database import get_session
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Default spend caps per PRD (D11, D16)
SPEND_CAP_PER_ROW_DEFAULT = 0.01   # $0.01 hard cap per ad row
SPEND_CAP_TOTAL_DEFAULT = 15.00    # $15.00 total assessment spend cap

# Pricing per million tokens (USD)
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

MODEL_PRICING: dict[str, dict[str, float]] = {
    # Gemini 3.x models: published table rows not yet in static ai.google.dev/pricing table.
    # UNVERIFIED: using conservative upper-bound rates until official rows are published.
    "gemini-3.5-flash-lite": {"input": 0.25 / 1_000_000, "output": 1.50 / 1_000_000},  # UNVERIFIED: conservative
    "gemini-3.1-flash-lite": {"input": 0.25 / 1_000_000, "output": 1.50 / 1_000_000},  # UNVERIFIED: conservative
    "gemini-3.8-flash": {"input": 0.75 / 1_000_000, "output": 3.75 / 1_000_000},       # UNVERIFIED: conservative
    "gemini-3.5-flash": {"input": 1.50 / 1_000_000, "output": 9.00 / 1_000_000},       # UNVERIFIED: conservative
    "gemini-3.1-pro-preview": {"input": 2.00 / 1_000_000, "output": 12.00 / 1_000_000}, # UNVERIFIED: conservative
    # Gemini 2.5 / 1.5 models: VERIFIED against official ai.google.dev/pricing
    "gemini-2.5-flash-lite": {"input": 0.10 / 1_000_000, "output": 0.40 / 1_000_000},  # VERIFIED
    "gemini-2.5-flash": {"input": 0.30 / 1_000_000, "output": 2.50 / 1_000_000},       # VERIFIED
    "gemini-flash-latest": {"input": 0.30 / 1_000_000, "output": 2.50 / 1_000_000},    # VERIFIED
    "gemini-2.5-pro": {"input": 1.25 / 1_000_000, "output": 10.00 / 1_000_000},       # VERIFIED (<=200k tokens)
    "gemini-pro-latest": {"input": 1.25 / 1_000_000, "output": 10.00 / 1_000_000},     # VERIFIED
    "gemini-2.0-flash": {"input": 0.10 / 1_000_000, "output": 0.40 / 1_000_000},       # VERIFIED
    "gemini-1.5-flash": {"input": 0.075 / 1_000_000, "output": 0.30 / 1_000_000},      # VERIFIED
    "gemini-1.5-pro": {"input": 1.25 / 1_000_000, "output": 5.00 / 1_000_000},         # VERIFIED
}

CREATE_COST_LEDGER_TABLE = """
CREATE TABLE IF NOT EXISTS cost_ledger (
    entry_id TEXT PRIMARY KEY CHECK (length(trim(entry_id)) > 0),
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    batch_id TEXT,
    ad_id TEXT,
    model_name TEXT NOT NULL CHECK (length(trim(model_name)) > 0),
    prompt_tokens INTEGER NOT NULL CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL CHECK (completion_tokens >= 0),
    total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
    cost_usd NUMERIC(10, 6) NOT NULL CHECK (cost_usd >= 0),
    step_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_client_id ON cost_ledger (client_id);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_ad_id ON cost_ledger (ad_id);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_batch_id ON cost_ledger (batch_id);
"""

INSERT_COST_ENTRY = """
INSERT INTO cost_ledger (
    entry_id, client_id, batch_id, ad_id, model_name,
    prompt_tokens, completion_tokens, total_tokens, cost_usd, step_name
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class SpendCapExceededError(RuntimeError):
    """Raised when a per-row or total spend cap is exceeded."""


@dataclass(frozen=True)
class CostLedgerEntry:
    """A record of tokens and cost spent on a model call."""

    entry_id: str
    client_id: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    ad_id: str | None = None
    batch_id: str | None = None
    step_name: str | None = None
    created_at: str | None = None


def get_spend_cap_per_row() -> float:
    """Return configured per-row spend cap from environment or default ($0.01)."""
    raw = os.environ.get("SPEND_CAP_PER_ROW")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return SPEND_CAP_PER_ROW_DEFAULT


def get_spend_cap_total() -> float:
    """Return configured total assessment spend cap from environment or default ($15.00)."""
    raw = os.environ.get("SPEND_CAP_TOTAL")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return SPEND_CAP_TOTAL_DEFAULT


def calculate_model_cost(
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Calculate cost in USD given model and token counts."""
    pricing = MODEL_PRICING.get(model_name)
    if pricing is None:
        pricing = MODEL_PRICING[DEFAULT_MODEL]
    cost = (prompt_tokens * pricing["input"]) + (completion_tokens * pricing["output"])
    return round(cost, 6)


def create_cost_ledger_table(connection: Any = None) -> None:
    """Create cost_ledger table if it does not exist."""
    def _execute(conn: Any) -> None:
        conn.execute(CREATE_COST_LEDGER_TABLE)

    if connection is not None:
        _execute(connection)
    else:
        with get_session() as session:
            _execute(session)


def check_spend_caps(
    ad_id: str | None = None,
    additional_cost: float = 0.0,
    *,
    spend_cap_per_row: float | None = None,
    spend_cap_total: float | None = None,
    connection: Any = None,
) -> None:
    """Check whether adding additional_cost would exceed row or total spend caps.

    Raises SpendCapExceededError if caps are breached.
    """
    row_cap = spend_cap_per_row if spend_cap_per_row is not None else get_spend_cap_per_row()
    total_cap = spend_cap_total if spend_cap_total is not None else get_spend_cap_total()

    def _execute(conn: Any) -> None:
        # 1. Total spend cap check
        cursor_total = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger")
        row_tot = cursor_total.fetchone()
        val_tot = (row_tot[0] if isinstance(row_tot, (tuple, list)) else (row_tot.get("sum", 0) if isinstance(row_tot, dict) else row_tot)) if row_tot is not None else 0.0
        current_total = float(val_tot or 0.0)
        if current_total + additional_cost > total_cap:
            raise SpendCapExceededError(
                f"Total spend cap of ${total_cap:.2f} exceeded: current spend is "
                f"${current_total:.4f}, requested addition of ${additional_cost:.6f} "
                f"would bring total to ${current_total + additional_cost:.4f}"
            )

        # 2. Per-row spend cap check
        if ad_id is not None:
            cursor_ad = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger WHERE ad_id = %s",
                (ad_id,),
            )
            row_ad = cursor_ad.fetchone()
            val_ad = (row_ad[0] if isinstance(row_ad, (tuple, list)) else (row_ad.get("sum", 0) if isinstance(row_ad, dict) else row_ad)) if row_ad is not None else 0.0
            current_ad = float(val_ad or 0.0)
            if current_ad + additional_cost > row_cap:
                raise SpendCapExceededError(
                    f"Spend cap per row of ${row_cap:.4f} exceeded for ad {ad_id!r}: "
                    f"current spend is ${current_ad:.6f}, requested addition of "
                    f"${additional_cost:.6f} would bring row total to ${current_ad + additional_cost:.6f}"
                )

    if connection is not None:
        _execute(connection)
    else:
        with get_session() as session:
            _execute(session)


def record_cost_entry(
    client_id: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    cost_usd: float | None = None,
    ad_id: str | None = None,
    batch_id: str | None = None,
    step_name: str | None = None,
    entry_id: str | None = None,
    spend_cap_per_row: float | None = None,
    spend_cap_total: float | None = None,
    connection: Any = None,
) -> CostLedgerEntry:
    """Record a model usage entry in PostgreSQL cost ledger with spend cap validation."""
    if not client_id or not isinstance(client_id, str):
        raise ValueError("client_id must be a non-empty string")
    if not model_name or not isinstance(model_name, str):
        raise ValueError("model_name must be a non-empty string")
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts must be non-negative integers")

    actual_cost = cost_usd if cost_usd is not None else calculate_model_cost(model_name, prompt_tokens, completion_tokens)
    if actual_cost < 0:
        raise ValueError("cost_usd cannot be negative")

    total_tokens = prompt_tokens + completion_tokens
    actual_entry_id = entry_id or f"cost-{uuid.uuid4()}"

    def _execute(conn: Any) -> CostLedgerEntry:
        # Check spend caps before persisting
        check_spend_caps(
            ad_id=ad_id,
            additional_cost=actual_cost,
            spend_cap_per_row=spend_cap_per_row,
            spend_cap_total=spend_cap_total,
            connection=conn,
        )

        conn.execute(
            INSERT_COST_ENTRY,
            (
                actual_entry_id,
                client_id.strip(),
                batch_id,
                ad_id,
                model_name.strip(),
                prompt_tokens,
                completion_tokens,
                total_tokens,
                Decimal(str(actual_cost)),
                step_name,
            ),
        )

        return CostLedgerEntry(
            entry_id=actual_entry_id,
            client_id=client_id.strip(),
            model_name=model_name.strip(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=actual_cost,
            ad_id=ad_id,
            batch_id=batch_id,
            step_name=step_name,
        )

    if connection is not None:
        return _execute(connection)
    with get_session() as session:
        return _execute(session)


def get_ad_spend(ad_id: str, *, connection: Any = None) -> float:
    """Get total spend for an ad row."""
    def _execute(conn: Any) -> float:
        cursor = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger WHERE ad_id = %s",
            (ad_id,),
        )
        row = cursor.fetchone()
        return float(row[0] if isinstance(row, (tuple, list)) else (row.get("sum", 0) if isinstance(row, dict) else row))

    if connection is not None:
        return _execute(connection)
    with get_session() as session:
        return _execute(session)


def get_batch_spend(batch_id: str, *, connection: Any = None) -> float:
    """Get total spend for a batch."""
    def _execute(conn: Any) -> float:
        cursor = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger WHERE batch_id = %s",
            (batch_id,),
        )
        row = cursor.fetchone()
        return float(row[0] if isinstance(row, (tuple, list)) else (row.get("sum", 0) if isinstance(row, dict) else row))

    if connection is not None:
        return _execute(connection)
    with get_session() as session:
        return _execute(session)


def get_client_spend(client_id: str, *, connection: Any = None) -> float:
    """Get total spend for a client."""
    def _execute(conn: Any) -> float:
        cursor = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger WHERE client_id = %s",
            (client_id,),
        )
        row = cursor.fetchone()
        return float(row[0] if isinstance(row, (tuple, list)) else (row.get("sum", 0) if isinstance(row, dict) else row))

    if connection is not None:
        return _execute(connection)
    with get_session() as session:
        return _execute(session)


def get_total_spend(*, connection: Any = None) -> float:
    """Get all-time total spend across the system."""
    def _execute(conn: Any) -> float:
        cursor = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM cost_ledger")
        row = cursor.fetchone()
        return float(row[0] if isinstance(row, (tuple, list)) else (row.get("sum", 0) if isinstance(row, dict) else row))

    if connection is not None:
        return _execute(connection)
    with get_session() as session:
        return _execute(session)
