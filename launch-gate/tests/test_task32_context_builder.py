"""Tests for Task 32: Context Builder (per-step context engineering)."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence
import pytest

from apps.worker import retrieval
from apps.worker.context_builder import ContextBuilder, StepContext, build_step_context
from apps.worker.file_storage import LocalFileStore
from apps.worker.retrieval import RetrievedRule
from apps.worker.shared_state import SharedState
from packages.contracts import AdContract
from packages.guards.tool_policy import FORBIDDEN_TOOLS, STEP_TOOL_ALLOWLIST


@pytest.fixture(autouse=True)
def reset_table_cache():
    retrieval.clear_table_cache()
    yield
    retrieval.clear_table_cache()


def sample_ad(
    ad_id: str = "ad-ctx-1",
    client_id: str = "brightleaf",
    platform: str = "Facebook",
    headline: str = "Glow Naturally",
    ad_text: str = "Pure organic serum for vibrant skin.",
) -> AdContract:
    return AdContract(
        ad_id=ad_id,
        batch_id="batch-1",
        client_id=client_id,
        row_number=1,
        platform=platform,
        headline=headline,
        ad_text=ad_text,
        destination_url="https://brightleaf.example/serum",
        creative_file=None,
        row_hash="rowhash",
        sheet_hash="sheethash",
    )


def sample_shared_state(
    ad_id: str = "ad-ctx-1",
    client_id: str = "brightleaf",
    status: str = "page_check_completed",
    summary: str = "Page check completed clean; offer matches.",
    file_refs: list[str] | None = None,
) -> SharedState:
    return SharedState(
        ad_id=ad_id,
        client_id=client_id,
        status=status,
        summary=summary,
        file_refs=file_refs or [],
    )


class FakeContextDBConnection:
    """Mock connection returning client rules."""

    def __init__(self, rules: list[dict[str, str]] | None = None):
        self.rules = rules or [
            {
                "client_id": "brightleaf",
                "entry_number": "BL-BB-1.1",
                "source_file": "brand_book.md",
                "content": "Brand Voice: Natural, warm, honest.",
            },
            {
                "client_id": "brightleaf",
                "entry_number": "BL-C-01",
                "source_file": "claims_register.md",
                "content": "Claim: Contains organic aloe vera and hyaluronic acid.",
            },
        ]

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def execute(self, query: str, params: tuple = ()):
        if "information_schema.tables" in query:
            self._is_info_schema = True
            return self
        self._is_info_schema = False
        client_id = params[0] if params else None
        self.matching = [r for r in self.rules if r["client_id"] == client_id]
        return self

    def fetchone(self):
        if getattr(self, "_is_info_schema", False):
            return (1,)
        rows = self.fetchall()
        return rows[0] if rows else None

    def fetchall(self):
        if getattr(self, "_is_info_schema", False):
            return [(1,)]
        return [
            (
                r["client_id"],
                r["entry_number"],
                r["source_file"],
                r["content"],
            )
            for r in self.matching
        ]


def test_context_builder_allowed_tools_per_step():
    state = sample_shared_state()
    builder = ContextBuilder()

    # Step: mechanical
    ctx_mech = builder.build("mechanical", state)
    assert ctx_mech.allowed_tools == STEP_TOOL_ALLOWLIST["mechanical"]
    assert "read_ad" in ctx_mech.allowed_tools

    # Step: page_check
    ctx_page = builder.build("page_check", state)
    assert ctx_page.allowed_tools == STEP_TOOL_ALLOWLIST["page_check"]
    assert "open_url_readonly" in ctx_page.allowed_tools

    # Step: wording_check
    ctx_word = builder.build("wording_check", state, retrieved_rules=[])
    assert ctx_word.allowed_tools == STEP_TOOL_ALLOWLIST["wording_check"]
    assert "retrieve_client_rules" in ctx_word.allowed_tools

    # Step: verdict
    ctx_verd = builder.build("verdict", state)
    assert ctx_verd.allowed_tools == STEP_TOOL_ALLOWLIST["verdict"]
    assert ctx_verd.allowed_tools == frozenset()

    # None of the steps should ever include forbidden tools
    for step in ("mechanical", "page_check", "wording_check", "verdict"):
        ctx = builder.build(step, state, retrieved_rules=[])
        assert not (ctx.allowed_tools & FORBIDDEN_TOOLS)


def test_context_builder_unknown_step_raises_value_error():
    state = sample_shared_state()
    builder = ContextBuilder()
    with pytest.raises(ValueError, match="Unknown workflow step"):
        builder.build("malicious_step", state)


def test_context_builder_enforces_client_isolation_on_ad():
    state = sample_shared_state(client_id="brightleaf")
    mismatched_ad = sample_ad(client_id="calmleaf")

    builder = ContextBuilder()
    with pytest.raises(ValueError, match="Client isolation violation"):
        builder.build("wording_check", state, ad=mismatched_ad)


def test_context_builder_enforces_ad_id_match():
    state = sample_shared_state(ad_id="ad-123", client_id="brightleaf")
    mismatched_ad = sample_ad(ad_id="ad-999", client_id="brightleaf")

    builder = ContextBuilder()
    with pytest.raises(ValueError, match="Ad identifier mismatch"):
        builder.build("wording_check", state, ad=mismatched_ad)


def test_context_builder_rejects_cross_client_rules():
    state = sample_shared_state(client_id="brightleaf")
    cross_client_rule = RetrievedRule(
        client_id="calmleaf",
        entry_number="CL-C-01",
        source_file="claims_register.md",
        content="Calmleaf sleep gummy claim",
    )

    builder = ContextBuilder()
    with pytest.raises(ValueError, match="Cross-client rule detected"):
        builder.build("wording_check", state, retrieved_rules=[cross_client_rule])


def test_context_builder_skips_rule_retrieval_for_mechanical_and_page_steps():
    conn = FakeContextDBConnection()
    state = sample_shared_state(client_id="brightleaf")
    builder = ContextBuilder(connection=conn)

    # Mechanical should NOT query rules
    ctx_mech = builder.build("mechanical", state)
    assert ctx_mech.rules == []
    assert ctx_mech.formatted_rules == ""

    # Page check should NOT query rules
    ctx_page = builder.build("page_check", state)
    assert ctx_page.rules == []
    assert ctx_page.formatted_rules == ""


def test_context_builder_retrieves_rules_for_wording_check():
    conn = FakeContextDBConnection()
    state = sample_shared_state(client_id="brightleaf")
    ad = sample_ad(client_id="brightleaf")

    builder = ContextBuilder(connection=conn)
    ctx = builder.build("wording_check", state, ad=ad, top_k=5)

    assert len(ctx.rules) == 2
    assert all(r.client_id == "brightleaf" for r in ctx.rules)
    assert "BL-BB-1.1" in ctx.formatted_rules
    assert "BL-C-01" in ctx.formatted_rules


def test_context_builder_reads_files_on_demand(tmp_path: Path):
    store = LocalFileStore(tmp_path / "storage")
    prompt_bytes = b"Wording prompt text with UNTRUSTED_DATA marker."
    page_bytes = b"Visible text of Brightleaf serum storefront."

    prompt_ref = store.put(prompt_bytes)
    page_ref = store.put(page_bytes)

    state = sample_shared_state(file_refs=[prompt_ref, page_ref])
    builder = ContextBuilder(file_store=store)

    # Read specific ref on demand
    ctx_single = builder.build("verdict", state, read_file_refs=[prompt_ref])
    assert prompt_ref in ctx_single.file_contents
    assert page_ref not in ctx_single.file_contents
    assert ctx_single.file_contents[prompt_ref] == prompt_bytes.decode("utf-8")

    # Read all refs on demand (read_file_refs=True)
    ctx_all = builder.build("verdict", state, read_file_refs=True)
    assert len(ctx_all.file_contents) == 2
    assert ctx_all.file_contents[prompt_ref] == prompt_bytes.decode("utf-8")
    assert ctx_all.file_contents[page_ref] == page_bytes.decode("utf-8")


def test_context_builder_refuses_unauthorized_file_ref(tmp_path: Path):
    store = LocalFileStore(tmp_path / "storage")
    unregistered_ref = store.put(b"Secret data of other ad.")

    # state only has ref-1, but caller asks for unregistered_ref
    state = sample_shared_state(file_refs=["0123456789abcdef0123456789abcdef"])
    builder = ContextBuilder(file_store=store)

    with pytest.raises(ValueError, match="Unauthorized file reference"):
        builder.build("verdict", state, read_file_refs=[unregistered_ref])


def test_context_builder_missing_store_when_reading_files_raises():
    state = sample_shared_state(file_refs=["0123456789abcdef0123456789abcdef"])
    builder = ContextBuilder(file_store=None)

    with pytest.raises(ValueError, match="file_store must be provided"):
        builder.build("verdict", state, read_file_refs=True)


def test_context_builder_build_prompt_formatting(tmp_path: Path):
    store = LocalFileStore(tmp_path / "storage")
    evidence_ref = store.put(b"Price on page is $34.00, matches ad.")

    state = sample_shared_state(
        summary="Mechanical & page checks passed.",
        file_refs=[evidence_ref],
    )
    ad = sample_ad(headline="100% Aloe Vera", ad_text="Organic botanical hydration.")
    rules = [
        RetrievedRule(
            client_id="brightleaf",
            entry_number="BL-C-01",
            source_file="claims_register.md",
            content="Claim: Contains organic aloe vera.",
        )
    ]

    ctx = build_step_context(
        "wording_check",
        state,
        ad=ad,
        retrieved_rules=rules,
        read_file_refs=[evidence_ref],
        file_store=store,
    )

    prompt = ctx.build_prompt("Evaluate whether the ad text matches client rules.")

    assert "Evaluate whether the ad text matches client rules." in prompt
    assert "=== WORKFLOW SUMMARY ===" in prompt
    assert "Mechanical & page checks passed." in prompt
    assert "=== CLIENT RULES (brightleaf) ===" in prompt
    assert "BL-C-01" in prompt
    assert "=== STORED EVIDENCE FILES ===" in prompt
    assert "Price on page is $34.00" in prompt
    assert "=== AD CONTENT (UNTRUSTED DATA) ===" in prompt
    assert "UNTRUSTED_DATA" in prompt
    assert "100% Aloe Vera" in prompt


def test_context_builder_leaves_shared_state_thin(tmp_path: Path):
    store = LocalFileStore(tmp_path / "storage")
    large_payload = b"X" * 50000
    large_ref = store.put(large_payload)

    state = sample_shared_state(file_refs=[large_ref])
    builder = ContextBuilder(file_store=store)

    ctx = builder.build("verdict", state, read_file_refs=True)

    # Large text is in ctx.file_contents
    assert len(ctx.file_contents[large_ref]) == 50000

    # state remains thin and unchanged
    assert state.file_refs == [large_ref]
    assert "50000" not in str(state.model_dump())
    assert not hasattr(state, "file_contents")
