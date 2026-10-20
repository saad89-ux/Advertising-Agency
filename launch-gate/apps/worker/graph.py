"""Bounded LangGraph workflow wiring and step cap enforcement for ad verification.

Orchestration (PRD §4, Architecture §2, CLAUDE.md §1):
1. Fixed, bounded DAG: mechanical -> page_check -> wording_check -> verdict.
2. Step cap enforcement: hard cap of max 5 steps per row.
   If step_count reaches max_steps, immediately routes to verdict and sets needs_person=True.
3. Thin state carried across nodes (SharedState).
4. Deterministic termination (no infinite loops, max_steps bounded).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from apps.worker.agent_team import (
    HeadlineSpecialistNode,
    JudgeAuditorNode,
    OfferAndComplianceNode,
    SupervisorDecision,
    SupervisorReviewerNode,
    UrlAndLandingPageNode,
)
from apps.worker.file_storage import LocalFileStore, UnifiedFileStore
from apps.worker.job_runner import (
    _execute_page_checks,
    load_creative_manifest,
    load_platform_limits,
)
from apps.worker.shared_state import SharedState
from apps.worker.verdict import (
    DEFAULT_MAX_STEPS_PER_ROW,
    aggregate_ad_verdict,
)
from apps.worker.wording_check import check_wording
from apps.worker.judge_eval import evaluate_with_llm_judge
from packages.contracts import (
    AdContract,
    CheckType,
    FindingContract,
    VerdictContract,
    VerdictStatus,
)
from packages.rules.creative_size_ratio import check_creative_size_ratio
from packages.rules.stale_platform_limits import check_stale_platform_limits
from packages.rules.platform_text_limits import check_platform_text_limits
from packages.rules.utm_naming import check_utm_naming
from packages.rules.utm_presence import check_utm_presence

logger = logging.getLogger(__name__)


class WorkerGraphState(TypedDict, total=False):
    """Execution state carried across LangGraph worker nodes."""

    ad: AdContract
    shared_state: SharedState
    findings: list[FindingContract]
    verdict: VerdictContract | None
    step_count: int
    max_steps: int
    needs_person: bool
    verdict_floor: VerdictStatus | None
    error: str | None
    # Node execution toggles and context overrides
    run_page_checks: bool
    run_wording_checks: bool
    connection: Any
    file_store: LocalFileStore | UnifiedFileStore | None
    model_client: Any
    embed_fn: Any
    retrieved_rules: Sequence[Any] | None
    manifest: Mapping[str, Mapping[str, Any]] | None
    platform_limits: Mapping[str, Any] | None
    allowed_origins: frozenset[str] | set[str] | None
    page_timeout_ms: int
    as_of: date | None
    # Multi-Agent feedback loop state
    loop_count: int
    max_loops: int
    needs_refinement: bool
    target_specialist: str | None
    headline_feedback: str | None
    copy_feedback: str | None
    refinement_history: list[dict[str, Any]]
    judge_result: Any | None
    tracer: Any
    trace_id: str | None
    emit_progress: bool


@dataclass(frozen=True)
class WorkerGraphResult:
    """Output summary of running the worker graph on an ad."""

    ad: AdContract
    verdict: VerdictContract
    findings: list[FindingContract]
    shared_state: SharedState
    step_count: int
    needs_person: bool
    final_state: WorkerGraphState
    trace_id: str | None = None
    loop_count: int = 0
    refinement_history: list[dict[str, Any]] = field(default_factory=list)


# --- Node Definitions ---

def mechanical_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node 1: Run deterministic mechanical rules."""
    step_count = state.get("step_count", 0) + 1
    max_steps = state.get("max_steps", DEFAULT_MAX_STEPS_PER_ROW)
    ad = state["ad"]
    tracer = state.get("tracer")
    trace_id = state.get("trace_id")

    span_id = None
    if tracer and trace_id:
        span_id = tracer.start_span(
            trace_id=trace_id,
            step_name="mechanical",
            input_data={
                "platform": ad.platform,
                "headline": ad.headline,
                "ad_text": ad.ad_text,
                "destination_url": ad.destination_url,
                "creative_file": ad.creative_file,
            },
        )

    if step_count >= max_steps:
        logger.warning("Step cap reached at mechanical node for ad %s", ad.ad_id)
        if tracer and trace_id and span_id:
            tracer.end_span(span_id, trace_id, output_data={"step_cap_reached": True}, status="capped")
        return {"step_count": step_count, "needs_person": True}

    manifest = state.get("manifest") or load_creative_manifest()
    limits_config = state.get("platform_limits") or load_platform_limits()
    platforms_map = limits_config.get("platforms", {}) if isinstance(limits_config, Mapping) else {}

    mech_findings: list[FindingContract] = []
    mech_findings.extend(check_utm_presence(ad))
    mech_findings.extend(check_utm_naming(ad))
    mech_findings.extend(check_platform_text_limits(ad, platforms_map))
    mech_findings.extend(check_stale_platform_limits(ad, limits_config))
    mech_findings.extend(check_creative_size_ratio(ad, manifest, platforms_map))

    existing_findings = list(state.get("findings", []))
    all_findings = existing_findings + mech_findings

    shared_state = state.get("shared_state") or SharedState(
        ad_id=ad.ad_id,
        client_id=ad.client_id,
        status="in_progress",
    )
    shared_state.status = "mechanical_completed"
    shared_state.summary = f"Mechanical check completed with {len(mech_findings)} finding(s)."

    if tracer and trace_id and span_id:
        tracer.end_span(
            span_id=span_id,
            trace_id=trace_id,
            output_data={
                "findings_count": len(mech_findings),
                "findings": [f.model_dump(mode="json") for f in mech_findings],
                "summary": shared_state.summary,
            },
            status="completed",
            cost_usd=0.0,
        )

    return {
        "step_count": step_count,
        "findings": all_findings,
        "shared_state": shared_state,
    }


def page_check_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node 2: Run read-only page offer and redirect checks."""
    step_count = state.get("step_count", 0) + 1
    max_steps = state.get("max_steps", DEFAULT_MAX_STEPS_PER_ROW)
    ad = state["ad"]
    tracer = state.get("tracer")
    trace_id = state.get("trace_id")

    span_id = None
    if tracer and trace_id:
        span_id = tracer.start_span(
            trace_id=trace_id,
            step_name="page_check",
            input_data={
                "destination_url": ad.destination_url,
                "run_page_checks": state.get("run_page_checks", True),
                "page_timeout_ms": state.get("page_timeout_ms", 5000),
            },
        )

    if step_count >= max_steps:
        logger.warning("Step cap reached at page_check node for ad %s", ad.ad_id)
        if tracer and trace_id and span_id:
            tracer.end_span(span_id, trace_id, output_data={"step_cap_reached": True}, status="capped")
        return {"step_count": step_count, "needs_person": True}

    if not state.get("run_page_checks", True):
        if tracer and trace_id and span_id:
            tracer.end_span(span_id, trace_id, output_data={"skipped": True}, status="skipped")
        return {"step_count": step_count}

    shared_state = state.get("shared_state")
    page_findings, updated_state = asyncio.run(
        _execute_page_checks(
            ad,
            allowed_origins=state.get("allowed_origins"),
            timeout_ms=state.get("page_timeout_ms", 5000),
            file_store=state.get("file_store"),
            as_of=state.get("as_of"),
        )
    )

    # Preserve any existing file refs in shared state
    if shared_state is not None:
        for ref in shared_state.file_refs:
            if ref not in updated_state.file_refs:
                updated_state.file_refs.append(ref)

    existing_findings = list(state.get("findings", []))
    all_findings = existing_findings + page_findings

    if tracer and trace_id and span_id:
        tracer.end_span(
            span_id=span_id,
            trace_id=trace_id,
            output_data={
                "findings_count": len(page_findings),
                "findings": [f.model_dump(mode="json") for f in page_findings],
                "file_refs": updated_state.file_refs,
                "summary": updated_state.summary,
            },
            status="completed",
            cost_usd=0.0,
        )

    return {
        "step_count": step_count,
        "findings": all_findings,
        "shared_state": updated_state,
    }


def wording_check_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node 3: Run client-isolated wording and claims check."""
    step_count = state.get("step_count", 0) + 1
    max_steps = state.get("max_steps", DEFAULT_MAX_STEPS_PER_ROW)
    ad = state["ad"]
    tracer = state.get("tracer")
    trace_id = state.get("trace_id")

    span_id = None
    if tracer and trace_id:
        span_id = tracer.start_span(
            trace_id=trace_id,
            step_name="wording_check",
            input_data={
                "client_id": ad.client_id,
                "headline": ad.headline,
                "ad_text": ad.ad_text,
                "run_wording_checks": state.get("run_wording_checks", True),
            },
        )

    if step_count >= max_steps:
        logger.warning("Step cap reached at wording_check node for ad %s", ad.ad_id)
        if tracer and trace_id and span_id:
            tracer.end_span(span_id, trace_id, output_data={"step_cap_reached": True}, status="capped")
        return {"step_count": step_count, "needs_person": True}

    if not state.get("run_wording_checks", True):
        if tracer and trace_id and span_id:
            tracer.end_span(span_id, trace_id, output_data={"skipped": True}, status="skipped")
        return {"step_count": step_count}

    shared_state = state.get("shared_state")
    wording_result = check_wording(
        ad,
        retrieved_rules=state.get("retrieved_rules"),
        model_client=state.get("model_client"),
        embed_fn=state.get("embed_fn"),
        file_store=state.get("file_store"),
        shared_state=shared_state,
        connection=state.get("connection"),
        tracer=tracer,
        trace_id=trace_id,
    )

    existing_findings = list(state.get("findings", []))
    all_findings = existing_findings + wording_result.findings
    needs_person = state.get("needs_person", False) or wording_result.needs_person

    if tracer and trace_id and span_id:
        tracer.end_span(
            span_id=span_id,
            trace_id=trace_id,
            output_data={
                "findings_count": len(wording_result.findings),
                "findings": [f.model_dump(mode="json") for f in wording_result.findings],
                "needs_person": wording_result.needs_person,
            },
            status="completed",
            cost_usd=getattr(wording_result, "cost_usd", 0.0) or 0.0,
        )

    return {
        "step_count": step_count,
        "findings": all_findings,
        "shared_state": wording_result.shared_state,
        "needs_person": needs_person,
    }


def verdict_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node 4: Aggregate findings and guard triggers into final verdict."""
    step_count = state.get("step_count", 0) + 1
    ad = state["ad"]
    findings = state.get("findings", [])
    max_steps = state.get("max_steps", DEFAULT_MAX_STEPS_PER_ROW)
    needs_person = state.get("needs_person", False)
    verdict_floor = state.get("verdict_floor")
    tracer = state.get("tracer")
    trace_id = state.get("trace_id")

    span_id = None
    if tracer and trace_id:
        span_id = tracer.start_span(
            trace_id=trace_id,
            step_name="verdict",
            input_data={
                "total_findings": len(findings),
                "needs_person": needs_person,
                "verdict_floor": verdict_floor.value if verdict_floor else None,
            },
        )

    # Execute runtime LLM-as-a-Judge compliance audit
    judge_result = evaluate_with_llm_judge(
        ad,
        findings,
        retrieved_rules=state.get("retrieved_rules"),
        connection=state.get("connection"),
        model_client=state.get("model_client"),
        tracer=tracer,
        trace_id=trace_id,
    )
    if judge_result.needs_person:
        needs_person = True

    # Filter out any findings that the judge explicitly identified as false alarms
    active_findings = [
        f for f in findings
        if f.finding_id not in judge_result.false_alarms_dismissed
    ]

    verdict = aggregate_ad_verdict(
        ad,
        active_findings,
        verdict_floor=verdict_floor,
        needs_person=needs_person,
        step_count=step_count,
        max_steps=max_steps,
    )

    shared_state = state.get("shared_state") or SharedState(
        ad_id=ad.ad_id,
        client_id=ad.client_id,
        status="completed",
    )
    shared_state.status = verdict.status.value
    shared_state.summary = verdict.summary

    if tracer and trace_id and span_id:
        tracer.end_span(
            span_id=span_id,
            trace_id=trace_id,
            output_data={
                "verdict": verdict.status.value,
                "summary": verdict.summary,
                "finding_ids": verdict.finding_ids,
            },
            status="completed",
            cost_usd=judge_result.cost_usd,
        )

    return {
        "step_count": step_count,
        "verdict": verdict,
        "shared_state": shared_state,
        "needs_person": needs_person,
    }


# --- Routing Decisions ---

def _route_after_mechanical(state: WorkerGraphState) -> str:
    """Conditionally route after mechanical node based on step cap."""
    step_count = state.get("step_count", 0)
    max_steps = state.get("max_steps", DEFAULT_MAX_STEPS_PER_ROW)
    if step_count >= max_steps:
        return "verdict"
    return "continue"


def _route_after_page_check(state: WorkerGraphState) -> str:
    """Conditionally route after page check node based on step cap."""
    step_count = state.get("step_count", 0)
    max_steps = state.get("max_steps", DEFAULT_MAX_STEPS_PER_ROW)
    if step_count >= max_steps:
        return "verdict"
    return "continue"


# --- Graph Construction ---

def create_ad_worker_graph(checkpointer: Any = None) -> CompiledStateGraph:
    """Wire and compile the bounded LangGraph DAG with step cap enforcement."""
    builder = StateGraph(WorkerGraphState)

    # Register nodes
    builder.add_node("mechanical", mechanical_node)
    builder.add_node("page_check", page_check_node)
    builder.add_node("wording_check", wording_check_node)
    builder.add_node("verdict", verdict_node)

    # Wire edges
    builder.add_edge(START, "mechanical")

    builder.add_conditional_edges(
        "mechanical",
        _route_after_mechanical,
        {"continue": "page_check", "verdict": "verdict"},
    )

    builder.add_conditional_edges(
        "page_check",
        _route_after_page_check,
        {"continue": "wording_check", "verdict": "verdict"},
    )

    builder.add_edge("wording_check", "verdict")
    builder.add_edge("verdict", END)

    return builder.compile(checkpointer=checkpointer)


def run_ad_worker_flow(
    ad: AdContract,
    *,
    shared_state: SharedState | None = None,
    initial_findings: Sequence[FindingContract] | None = None,
    verdict_floor: VerdictStatus | None = None,
    needs_person: bool = False,
    max_steps: int = DEFAULT_MAX_STEPS_PER_ROW,
    initial_step_count: int = 0,
    connection: Any = None,
    file_store: LocalFileStore | UnifiedFileStore | None = None,
    model_client: Any = None,
    embed_fn: Any = None,
    retrieved_rules: Sequence[Any] | None = None,
    run_page_checks: bool = True,
    run_wording_checks: bool = True,
    checkpointer: Any = None,
    thread_id: str | None = None,
    resume: bool = False,
    tracer: Any = None,
    trace_id: str | None = None,
    manifest: Mapping[str, Mapping[str, Any]] | None = None,
    platform_limits: Mapping[str, Any] | None = None,
    allowed_origins: frozenset[str] | set[str] | None = None,
    page_timeout_ms: int = 5000,
    as_of: date | None = None,
) -> WorkerGraphResult:
    """Convenience function to execute the bounded LangGraph worker flow on an ad."""
    graph = create_ad_worker_graph(checkpointer=checkpointer)

    current_state = shared_state or SharedState(
        ad_id=ad.ad_id,
        client_id=ad.client_id,
        status="in_progress",
    )

    config: dict[str, Any] = {}
    effective_thread_id = thread_id
    if effective_thread_id is None and (checkpointer is not None or tracer is not None) and ad.batch_id and ad.ad_id:
        from apps.worker.checkpointer import make_thread_id
        effective_thread_id = make_thread_id(ad.batch_id, ad.ad_id)

    effective_trace_id = trace_id or effective_thread_id

    if effective_thread_id is not None:
        config["configurable"] = {"thread_id": effective_thread_id}

    if tracer is not None and effective_trace_id is not None:
        tracer.start_trace(
            trace_id=effective_trace_id,
            batch_id=getattr(ad, "batch_id", "") or "unknown",
            ad_id=ad.ad_id,
            client_id=ad.client_id,
            metadata={"platform": ad.platform, "headline": ad.headline},
        )

    initial_state: WorkerGraphState = {
        "ad": ad,
        "shared_state": current_state,
        "findings": list(initial_findings or []),
        "verdict": None,
        "step_count": initial_step_count,
        "max_steps": max_steps,
        "needs_person": needs_person,
        "verdict_floor": verdict_floor,
        "run_page_checks": run_page_checks,
        "run_wording_checks": run_wording_checks,
        "emit_progress": run_page_checks or run_wording_checks,
        "connection": connection,
        "file_store": file_store,
        "model_client": model_client,
        "embed_fn": embed_fn,
        "retrieved_rules": retrieved_rules,
        "manifest": manifest,
        "platform_limits": platform_limits,
        "allowed_origins": allowed_origins,
        "page_timeout_ms": page_timeout_ms,
        "as_of": as_of,
        "tracer": tracer,
        "trace_id": effective_trace_id,
    }

    input_payload = None if resume else initial_state
    final_state = graph.invoke(input_payload, config=config or None)

    ad_out = final_state.get("ad") or ad
    verdict = final_state.get("verdict")
    if verdict is None:
        verdict = aggregate_ad_verdict(
            ad_out,
            final_state.get("findings", []),
            verdict_floor=final_state.get("verdict_floor"),
            needs_person=final_state.get("needs_person", False),
            step_count=final_state.get("step_count", 0),
            max_steps=max_steps,
        )

    if tracer is not None and effective_trace_id is not None:
        tracer.end_trace(
            trace_id=effective_trace_id,
            verdict=verdict.status.value,
            step_count=final_state.get("step_count", 0),
        )

    effective_needs_person = final_state.get("needs_person", False) or (verdict.status == VerdictStatus.NEEDS_PERSON)

    return WorkerGraphResult(
        ad=ad_out,
        verdict=verdict,
        findings=final_state.get("findings", []),
        shared_state=final_state.get("shared_state", current_state),
        step_count=final_state.get("step_count", 0),
        needs_person=effective_needs_person,
        final_state=final_state,
        trace_id=effective_trace_id if tracer else None,
    )


# ===========================================================================
# Multi-Agent Specialist Nodes & Autonomous Feedback Loop
# ===========================================================================

def headline_specialist_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node: Headline Specialist audits and autonomously refines ad headlines."""
    ad = state["ad"]
    feedback = state.get("headline_feedback")
    specialist = HeadlineSpecialistNode()

    try:
        from apps.api.sse import notify_ad_update
        if state.get("emit_progress", True) and ad.batch_id:
            notify_ad_update(ad.batch_id, ad.ad_id, stage="inspecting_headline", client_id=ad.client_id, row_number=ad.row_number)
    except Exception:
        pass

    # 1. Apply supervisor refinement feedback if routed back in feedback loop
    if feedback:
        refined_headline = specialist.refine_headline(
            ad.headline or "",
            feedback=feedback,
            platform=ad.platform,
        )
        ad = ad.model_copy(update={"headline": refined_headline})

    # 2. Audit headline
    headline_findings = specialist.check(ad, platform_limits=state.get("platform_limits"))

    # Replace old headline findings with new ones
    existing = [f for f in state.get("findings", []) if f.field != "headline"]
    all_findings = existing + headline_findings

    return {
        "ad": ad,
        "findings": all_findings,
        "headline_feedback": None,
        "step_count": state.get("step_count", 0) + 1,
    }


def url_specialist_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node: URL & Landing Page Specialist checks UTMs, opens headlessly, and captures snapshots."""
    ad = state["ad"]

    try:
        from apps.api.sse import notify_ad_update
        if state.get("emit_progress", True) and ad.batch_id:
            notify_ad_update(ad.batch_id, ad.ad_id, stage="auditing_url", client_id=ad.client_id, row_number=ad.row_number)
    except Exception:
        pass

    specialist = UrlAndLandingPageNode()
    run_page_checks = state.get("run_page_checks", True)

    url_findings: list[FindingContract] = []
    shared_state = state.get("shared_state") or SharedState(ad_id=ad.ad_id, client_id=ad.client_id, status="checking_url")

    try:
        url_findings, updated_state = asyncio.run(
            specialist.check(
                ad,
                allowed_origins=state.get("allowed_origins"),
                timeout_ms=state.get("page_timeout_ms", 5000),
                file_store=state.get("file_store"),
                as_of=state.get("as_of"),
                run_page_checks=run_page_checks,
            )
        )
        shared_state = updated_state
    except Exception as exc:
        logger.warning("URL specialist encountered non-fatal error: %s", exc)

    existing = [f for f in state.get("findings", []) if f.check_type != CheckType.PAGE and f.field != "destination_url"]
    all_findings = existing + url_findings

    return {
        "findings": all_findings,
        "shared_state": shared_state,
        "step_count": state.get("step_count", 0) + 1,
    }


def offer_specialist_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node: Offer & Compliance Specialist audits claims, vector rules, and refines body copy."""
    ad = state["ad"]
    feedback = state.get("copy_feedback")
    specialist = OfferAndComplianceNode()

    # 1. Apply supervisor refinement feedback if routed back
    if feedback:
        refined_text = specialist.refine_copy(ad.ad_text or "", feedback=feedback)
        ad = ad.model_copy(update={"ad_text": refined_text})

    comp_findings: list[FindingContract] = []
    shared_state = state.get("shared_state") or SharedState(ad_id=ad.ad_id, client_id=ad.client_id, status="checking_offer")
    needs_person = state.get("needs_person", False)

    if state.get("run_wording_checks", True):
        try:
            comp_findings, shared_state, comp_needs_person = specialist.check(
                ad,
                shared_state=shared_state,
                connection=state.get("connection"),
                file_store=state.get("file_store"),
                retrieved_rules=state.get("retrieved_rules"),
                model_client=state.get("model_client"),
                embed_fn=state.get("embed_fn"),
                tracer=state.get("tracer"),
                trace_id=state.get("trace_id"),
            )
            needs_person = needs_person or comp_needs_person
        except Exception as exc:
            logger.warning("Offer specialist encountered non-fatal error: %s", exc)

    existing = [
        f for f in state.get("findings", [])
        if f.check_type != CheckType.WORDING or f.field == "headline"
    ]
    all_findings = existing + comp_findings

    return {
        "ad": ad,
        "findings": all_findings,
        "shared_state": shared_state,
        "copy_feedback": None,
        "needs_person": needs_person,
        "step_count": state.get("step_count", 0) + 1,
    }


def supervisor_reviewer_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node: Chief Supervisor reconciles findings, evaluates fixability, and routes refinement."""
    ad = state["ad"]

    try:
        from apps.api.sse import notify_ad_update
        if state.get("emit_progress", True) and ad.batch_id:
            notify_ad_update(ad.batch_id, ad.ad_id, stage="supervisor_review", client_id=ad.client_id, row_number=ad.row_number)
    except Exception:
        pass

    reviewer = SupervisorReviewerNode()
    findings = state.get("findings", [])
    loop_count = state.get("loop_count", 0)
    max_loops = state.get("max_loops", 3)
    shared_state = state.get("shared_state") or SharedState(ad_id=ad.ad_id, client_id=ad.client_id, status="in_review")

    decision = reviewer.review(
        ad,
        findings,
        shared_state,
        loop_count=loop_count,
        max_loops=max_loops,
        platform_limits=state.get("platform_limits"),
    )

    refinement_history = list(state.get("refinement_history", []))
    if decision.audit_entry:
        refinement_history.append(decision.audit_entry)

    shared_state.status = "supervisor_reviewed"

    return {
        "needs_refinement": decision.action == "REFINE",
        "target_specialist": decision.target_specialist,
        "headline_feedback": decision.feedback if decision.target_specialist == "headline_specialist" else None,
        "copy_feedback": decision.feedback if decision.target_specialist == "offer_specialist" else None,
        "loop_count": decision.loop_count,
        "refinement_history": refinement_history,
        "shared_state": shared_state,
        "step_count": state.get("step_count", 0) + 1,
    }


def judge_auditor_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node: Independent Judge Auditor certifies safety and dismisses false alarms."""
    ad = state["ad"]

    try:
        from apps.api.sse import notify_ad_update
        if state.get("emit_progress", True) and ad.batch_id:
            notify_ad_update(ad.batch_id, ad.ad_id, stage="judge_certification", client_id=ad.client_id, row_number=ad.row_number)
    except Exception:
        pass

    findings = state.get("findings", [])
    judge = JudgeAuditorNode()
    judge_result = judge.evaluate(
        ad,
        findings,
        retrieved_rules=state.get("retrieved_rules"),
        connection=state.get("connection"),
        model_client=state.get("model_client"),
        tracer=state.get("tracer"),
        trace_id=state.get("trace_id"),
    )

    active_findings = [
        f for f in findings
        if f.finding_id not in judge_result.false_alarms_dismissed
    ]
    needs_person = state.get("needs_person", False) or judge_result.needs_person

    return {
        "findings": active_findings,
        "judge_result": judge_result,
        "needs_person": needs_person,
        "step_count": state.get("step_count", 0) + 1,
    }


def multi_agent_verdict_node(state: WorkerGraphState) -> dict[str, Any]:
    """Node: Finalize team verdict and update shared state."""
    ad = state["ad"]
    findings = state.get("findings", [])
    max_steps = state.get("max_steps", DEFAULT_MAX_STEPS_PER_ROW)
    needs_person = state.get("needs_person", False)
    verdict_floor = state.get("verdict_floor")
    step_count = state.get("step_count", 0) + 1

    verdict = aggregate_ad_verdict(
        ad,
        findings,
        verdict_floor=verdict_floor,
        needs_person=needs_person,
        step_count=step_count,
        max_steps=max_steps,
    )

    shared_state = state.get("shared_state") or SharedState(
        ad_id=ad.ad_id,
        client_id=ad.client_id,
        status="completed",
    )
    shared_state.status = verdict.status.value
    shared_state.summary = verdict.summary

    try:
        from apps.api.sse import notify_ad_update
        if state.get("emit_progress", True) and ad.batch_id:
            notify_ad_update(
                ad.batch_id,
                ad.ad_id,
                stage="complete",
                verdict=verdict.status.value,
                client_id=ad.client_id,
                row_number=ad.row_number,
                findings_count=len(findings),
            )
    except Exception:
        pass

    return {
        "verdict": verdict,
        "shared_state": shared_state,
        "step_count": step_count,
    }


def _route_after_headline(state: WorkerGraphState) -> str:
    """If in refinement loop (>0), route directly back to supervisor; otherwise proceed to url_specialist."""
    if state.get("loop_count", 0) > 0:
        return "supervisor_reviewer"
    return "url_specialist"


def _route_after_supervisor(state: WorkerGraphState) -> str:
    """Conditionally route back to specialist for refinement or forward to judge."""
    if state.get("needs_refinement") and state.get("loop_count", 0) <= 3:
        target = state.get("target_specialist")
        if target == "headline_specialist":
            return "headline_specialist"
        elif target == "offer_specialist":
            return "offer_specialist"
    return "judge_auditor"


def create_multi_agent_graph(checkpointer: Any = None) -> CompiledStateGraph:
    """Create and compile the specialized multi-agent workflow graph with autonomous feedback loop."""
    builder = StateGraph(WorkerGraphState)

    # Register nodes
    builder.add_node("headline_specialist", headline_specialist_node)
    builder.add_node("url_specialist", url_specialist_node)
    builder.add_node("offer_specialist", offer_specialist_node)
    builder.add_node("supervisor_reviewer", supervisor_reviewer_node)
    builder.add_node("judge_auditor", judge_auditor_node)
    builder.add_node("verdict_node", multi_agent_verdict_node)

    # Wire edges
    builder.add_edge(START, "headline_specialist")

    builder.add_conditional_edges(
        "headline_specialist",
        _route_after_headline,
        {
            "supervisor_reviewer": "supervisor_reviewer",
            "url_specialist": "url_specialist",
        },
    )

    builder.add_edge("url_specialist", "offer_specialist")
    builder.add_edge("offer_specialist", "supervisor_reviewer")

    builder.add_conditional_edges(
        "supervisor_reviewer",
        _route_after_supervisor,
        {
            "headline_specialist": "headline_specialist",
            "offer_specialist": "offer_specialist",
            "judge_auditor": "judge_auditor",
        },
    )

    builder.add_edge("judge_auditor", "verdict_node")
    builder.add_edge("verdict_node", END)

    return builder.compile(checkpointer=checkpointer)


def run_multi_agent_worker_flow(
    ad: AdContract,
    *,
    shared_state: SharedState | None = None,
    initial_findings: Sequence[FindingContract] | None = None,
    verdict_floor: VerdictStatus | None = None,
    needs_person: bool = False,
    max_steps: int = 20,
    max_loops: int = 3,
    initial_step_count: int = 0,
    connection: Any = None,
    file_store: LocalFileStore | UnifiedFileStore | None = None,
    model_client: Any = None,
    embed_fn: Any = None,
    retrieved_rules: Sequence[Any] | None = None,
    run_page_checks: bool = True,
    run_wording_checks: bool = True,
    checkpointer: Any = None,
    thread_id: str | None = None,
    resume: bool = False,
    tracer: Any = None,
    trace_id: str | None = None,
    manifest: Mapping[str, Mapping[str, Any]] | None = None,
    platform_limits: Mapping[str, Any] | None = None,
    allowed_origins: frozenset[str] | set[str] | None = None,
    page_timeout_ms: int = 5000,
    as_of: date | None = None,
) -> WorkerGraphResult:
    """Primary execution entry point for the Multi-Agent Worker Team with feedback loop."""
    graph = create_multi_agent_graph(checkpointer=checkpointer)

    current_state = shared_state or SharedState(
        ad_id=ad.ad_id,
        client_id=ad.client_id,
        status="in_progress",
    )

    config: dict[str, Any] = {}
    effective_thread_id = thread_id
    if effective_thread_id is None and (checkpointer is not None or tracer is not None) and ad.batch_id and ad.ad_id:
        from apps.worker.checkpointer import make_thread_id
        effective_thread_id = make_thread_id(ad.batch_id, ad.ad_id)

    effective_trace_id = trace_id or effective_thread_id
    if effective_thread_id is not None:
        config["configurable"] = {"thread_id": effective_thread_id}

    if tracer is not None and effective_trace_id is not None:
        tracer.start_trace(
            trace_id=effective_trace_id,
            batch_id=getattr(ad, "batch_id", "") or "unknown",
            ad_id=ad.ad_id,
            client_id=ad.client_id,
            metadata={"platform": ad.platform, "headline": ad.headline},
        )

    initial_state: WorkerGraphState = {
        "ad": ad,
        "shared_state": current_state,
        "findings": list(initial_findings or []),
        "verdict": None,
        "step_count": initial_step_count,
        "max_steps": max_steps,
        "max_loops": max_loops,
        "loop_count": 0,
        "needs_refinement": False,
        "target_specialist": None,
        "headline_feedback": None,
        "copy_feedback": None,
        "refinement_history": [],
        "needs_person": needs_person,
        "verdict_floor": verdict_floor,
        "run_page_checks": run_page_checks,
        "run_wording_checks": run_wording_checks,
        "emit_progress": run_page_checks or run_wording_checks,
        "connection": connection,
        "file_store": file_store,
        "model_client": model_client,
        "embed_fn": embed_fn,
        "retrieved_rules": retrieved_rules,
        "manifest": manifest,
        "platform_limits": platform_limits,
        "allowed_origins": allowed_origins,
        "page_timeout_ms": page_timeout_ms,
        "as_of": as_of,
        "tracer": tracer,
        "trace_id": effective_trace_id,
    }

    input_payload = None if resume else initial_state
    final_state = graph.invoke(input_payload, config=config or None)

    ad_out = final_state.get("ad") or ad
    verdict = final_state.get("verdict")
    if verdict is None:
        verdict = aggregate_ad_verdict(
            ad_out,
            final_state.get("findings", []),
            verdict_floor=final_state.get("verdict_floor"),
            needs_person=final_state.get("needs_person", False),
            step_count=final_state.get("step_count", 0),
            max_steps=max_steps,
        )

    if tracer is not None and effective_trace_id is not None:
        tracer.end_trace(
            trace_id=effective_trace_id,
            verdict=verdict.status.value,
            step_count=final_state.get("step_count", 0),
        )

    effective_needs_person = final_state.get("needs_person", False) or (verdict.status == VerdictStatus.NEEDS_PERSON)

    return WorkerGraphResult(
        ad=ad_out,
        verdict=verdict,
        findings=final_state.get("findings", []),
        shared_state=final_state.get("shared_state", current_state),
        step_count=final_state.get("step_count", 0),
        needs_person=effective_needs_person,
        final_state=final_state,
        trace_id=effective_trace_id if tracer else None,
        loop_count=final_state.get("loop_count", 0),
        refinement_history=final_state.get("refinement_history", []),
    )
