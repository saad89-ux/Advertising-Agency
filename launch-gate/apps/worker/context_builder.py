"""Context builder for worker steps.

Per-step context engineering (Mini-PRD §5, Architecture §4):
1. Tool Selection: Selects allowed tools strictly per step (least privilege).
2. Resource Selection: Retrieves client-isolated rules (top-k, this client only).
   Skips rule retrieval for mechanical/page checks to minimize tokens and cost.
3. Summary Selection: Carries compact summary from thin SharedState.
4. On-demand File Store: Reads stored file outputs (full page text, prompts) on demand by reference.
5. Prompt Construction: Formats structured prompts combining instructions, thin summary,
   client rules, and on-demand file snippets with untrusted data wrappers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import logging
from typing import Any

from apps.worker.file_storage import LocalFileStore, UnifiedFileStore
from apps.worker.retrieval import (
    RetrievedRule,
    format_retrieved_rules_for_context,
    list_client_rules,
    retrieve_client_rules,
)
from apps.worker.shared_state import SharedState
from packages.contracts import AdContract
from packages.guards.input_guard import wrap_untrusted
from packages.guards.tool_policy import (
    FORBIDDEN_TOOLS,
    STEP_TOOL_ALLOWLIST,
    assert_tool_allowed,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StepContext:
    """Compact, validated context assembled for a specific workflow step."""

    step_name: str
    ad_id: str
    client_id: str
    allowed_tools: frozenset[str]
    summary: str | None
    rules: list[RetrievedRule] = field(default_factory=list)
    formatted_rules: str = ""
    file_contents: dict[str, str] = field(default_factory=dict)
    shared_state: SharedState | None = None
    ad: AdContract | None = None

    def build_prompt(
        self,
        task_instructions: str,
        *,
        include_rules: bool = True,
        include_summary: bool = True,
        include_files: bool = True,
        include_ad: bool = True,
    ) -> str:
        """Assemble structured prompt combining instructions, summary, rules, and files."""
        sections: list[str] = []

        # 1. Base task instructions
        sections.append(task_instructions.strip())

        # 2. Step Summary (from thin shared state)
        if include_summary and self.summary:
            sections.append(f"=== WORKFLOW SUMMARY ===\n{self.summary.strip()}")

        # 3. Client Rules (top-k retrieved chunks, strictly client-scoped)
        if include_rules and self.rules:
            rules_header = f"=== CLIENT RULES ({self.client_id}) ==="
            sections.append(f"{rules_header}\n{self.formatted_rules.strip()}")

        # 4. On-demand file contents (by reference)
        if include_files and self.file_contents:
            file_chunks: list[str] = []
            for ref, content in self.file_contents.items():
                file_chunks.append(f"--- File Ref: {ref} ---\n{content.strip()}")
            sections.append("=== STORED EVIDENCE FILES ===\n" + "\n\n".join(file_chunks))

        # 5. Ad content (wrapped as untrusted data)
        if include_ad and self.ad is not None:
            wrapped_headline = wrap_untrusted(self.ad.headline or "", "headline")
            wrapped_text = wrap_untrusted(self.ad.ad_text or "", "ad_text")
            ad_section = (
                f"=== AD CONTENT (UNTRUSTED DATA) ===\n"
                f"Ad ID: {self.ad.ad_id}\n"
                f"Platform: {self.ad.platform}\n"
                f"Headline:\n{wrapped_headline}\n\n"
                f"Ad Text:\n{wrapped_text}"
            )
            sections.append(ad_section)

        return "\n\n".join(sections)


class ContextBuilder:
    """Builds step-scoped context for worker workflow nodes."""

    def __init__(
        self,
        file_store: LocalFileStore | UnifiedFileStore | None = None,
        connection: Any = None,
        embed_fn: Callable[[str], Sequence[float]] | None = None,
    ) -> None:
        self.file_store = file_store
        self.connection = connection
        self.embed_fn = embed_fn

    def build(
        self,
        step_name: str,
        shared_state: SharedState,
        *,
        ad: AdContract | None = None,
        query_text: str | None = None,
        top_k: int = 5,
        retrieved_rules: Sequence[RetrievedRule] | None = None,
        retrieve_rules: bool | None = None,
        read_file_refs: Sequence[str] | bool = False,
        connection: Any = None,
        embed_fn: Callable[[str], Sequence[float]] | None = None,
    ) -> StepContext:
        """Assemble StepContext enforcing tool policy, client isolation, and thin state."""
        # 1. Validate step name and select allowed tools
        if step_name not in STEP_TOOL_ALLOWLIST:
            raise ValueError(
                f"Unknown workflow step: {step_name!r}. Allowed steps: {sorted(STEP_TOOL_ALLOWLIST.keys())}"
            )
        allowed_tools = STEP_TOOL_ALLOWLIST[step_name]
        assert not (allowed_tools & FORBIDDEN_TOOLS), f"Forbidden tools found in {step_name} allowlist"

        # 2. Validate client and ad identifiers
        if ad is not None:
            if ad.client_id != shared_state.client_id:
                raise ValueError(
                    f"Client isolation violation: ad.client_id ({ad.client_id!r}) != "
                    f"shared_state.client_id ({shared_state.client_id!r})"
                )
            if ad.ad_id != shared_state.ad_id:
                raise ValueError(
                    f"Ad identifier mismatch: ad.ad_id ({ad.ad_id!r}) != "
                    f"shared_state.ad_id ({shared_state.ad_id!r})"
                )

        client_id = shared_state.client_id
        active_conn = connection if connection is not None else self.connection
        active_embed_fn = embed_fn if embed_fn is not None else self.embed_fn

        # 3. Rule Selection (Client-isolated, top-k)
        # Default behavior: wording_check retrieves rules; other steps do not unless requested.
        should_retrieve = retrieve_rules if retrieve_rules is not None else (step_name == "wording_check")
        active_rules: list[RetrievedRule] = []

        if retrieved_rules is not None:
            # Caller supplied pre-retrieved rules; enforce strict client isolation
            for rule in retrieved_rules:
                if rule.client_id != client_id:
                    raise ValueError(
                        f"Cross-client rule detected: rule for client {rule.client_id!r} "
                        f"cannot be used in context for client {client_id!r}"
                    )
            active_rules = list(retrieved_rules)[:top_k]
        elif should_retrieve and top_k > 0:
            query = query_text
            if not query and ad is not None:
                query = f"{ad.headline or ''} {ad.ad_text or ''}".strip()

            try:
                if active_embed_fn is not None and query:
                    active_rules = retrieve_client_rules(
                        client_id,
                        query_text=query,
                        embed_text=active_embed_fn,
                        top_k=top_k,
                        connection=active_conn,
                    )
                elif active_conn is not None:
                    active_rules = list_client_rules(client_id, connection=active_conn)[:top_k]
            except Exception as exc:
                logger.warning("ContextBuilder rule retrieval failed for client %s: %s", client_id, exc)
                active_rules = []

        formatted_rules = format_retrieved_rules_for_context(active_rules) if active_rules else ""

        # 4. On-demand File Store Reading
        file_contents: dict[str, str] = {}
        if read_file_refs:
            active_store = self.file_store
            if active_store is None:
                raise ValueError("file_store must be provided to read_file_refs on demand")

            refs_to_read: list[str] = []
            if read_file_refs is True:
                refs_to_read = list(shared_state.file_refs)
            else:
                refs_to_read = list(read_file_refs)

            for ref in refs_to_read:
                # Security boundary: reference MUST be registered in shared_state.file_refs
                if ref not in shared_state.file_refs:
                    raise ValueError(
                        f"Unauthorized file reference: {ref!r} is not in shared_state.file_refs"
                    )
                try:
                    data = active_store.get(ref)
                    try:
                        file_contents[ref] = data.decode("utf-8")
                    except UnicodeDecodeError:
                        file_contents[ref] = f"<binary data: {len(data)} bytes>"
                except Exception as exc:
                    logger.warning("Could not read file ref %s from store: %s", ref, exc)

        return StepContext(
            step_name=step_name,
            ad_id=shared_state.ad_id,
            client_id=client_id,
            allowed_tools=allowed_tools,
            summary=shared_state.summary,
            rules=active_rules,
            formatted_rules=formatted_rules,
            file_contents=file_contents,
            shared_state=shared_state,
            ad=ad,
        )


def build_step_context(
    step_name: str,
    shared_state: SharedState,
    *,
    ad: AdContract | None = None,
    query_text: str | None = None,
    top_k: int = 5,
    retrieved_rules: Sequence[RetrievedRule] | None = None,
    retrieve_rules: bool | None = None,
    read_file_refs: Sequence[str] | bool = False,
    file_store: LocalFileStore | UnifiedFileStore | None = None,
    connection: Any = None,
    embed_fn: Callable[[str], Sequence[float]] | None = None,
) -> StepContext:
    """Convenience helper to build a step context."""
    builder = ContextBuilder(
        file_store=file_store,
        connection=connection,
        embed_fn=embed_fn,
    )
    return builder.build(
        step_name,
        shared_state,
        ad=ad,
        query_text=query_text,
        top_k=top_k,
        retrieved_rules=retrieved_rules,
        retrieve_rules=retrieve_rules,
        read_file_refs=read_file_refs,
        connection=connection,
        embed_fn=embed_fn,
    )
