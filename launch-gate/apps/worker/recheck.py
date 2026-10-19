"""Bounded re-check loop after edit orchestration.

Architecture & PRD compliance:
- PRD D17: After an edit, the ad is re-checked in a bounded loop (cap in configuration, default 5).
  Brief: re-examine, "never endlessly" [B R5].
- CLAUDE.md §1 & §4: Reviewer edits and reasons are saved; changed ad is re-checked in a bounded loop.
- Needs: 34 (Bounded LangGraph flow wiring & step cap enforcement).
- Needs: 41 (Reviewer edit with reasons saved & audit trail).
- Checkpointer integration (PRD D18): Resets stale checkpoint thread state for new content versions.
- Stale findings replacement: Cleans previous findings for the ad, persisting only current findings.
- Tracing integration (PRD D8): Records re-check execution trace in worker_traces with iteration metadata.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import json
import logging
from typing import Any

from apps.api.database import get_session
from apps.api.edits import count_ad_edits, get_ad_edits_history
from apps.api.settings import DEFAULT_MAX_EDIT_LOOPS, max_edit_loops
from apps.worker.checkpointer import make_thread_id
from apps.worker.file_storage import LocalFileStore, UnifiedFileStore
from apps.worker.graph import run_ad_worker_flow
from apps.worker.job_runner import (
    load_creative_manifest,
    load_platform_limits,
)
from apps.worker.shared_state import SharedState
from apps.worker.verdict import (
    DEFAULT_MAX_STEPS_PER_ROW,
    aggregate_ad_verdict,
)
from packages.contracts import (
    AdContract,
    CheckType,
    FindingContract,
    VerdictContract,
    VerdictStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecheckResult:
    """Outcome of a bounded re-check loop execution on an edited ad."""

    ad: AdContract
    verdict: VerdictContract
    findings: list[FindingContract]
    edit_iteration: int
    max_loops: int
    is_loop_capped: bool
    needs_person: bool
    step_count: int
    shared_state: SharedState
    trace_id: str | None = None
    previous_row_hash: str | None = None
    new_row_hash: str = ""


def replace_ad_findings(
    connection: Any,
    ad_id: str,
    findings: Sequence[FindingContract],
) -> None:
    """Clean stale findings for an ad and persist current findings from re-check."""
    try:
        connection.execute("DELETE FROM findings WHERE ad_id = %s;", (ad_id,))
    except Exception as exc:
        logger.debug("Failed deleting stale findings for ad %s: %s", ad_id, exc)

    insert_sql = """
    INSERT INTO findings (
        finding_id, ad_id, row_number, check_type, message, field,
        proposed_fix, rule_id, file_ref
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (finding_id) DO NOTHING;
    """
    for finding in findings:
        try:
            connection.execute(
                insert_sql,
                (
                    finding.finding_id,
                    finding.ad_id,
                    finding.row_number,
                    finding.check_type.value,
                    finding.message,
                    finding.field,
                    finding.proposed_fix,
                    finding.rule_id,
                    finding.file_ref,
                ),
            )
        except Exception as exc:
            logger.debug("Failed inserting finding %s: %s", finding.finding_id, exc)


def record_recheck_trace(
    connection: Any,
    *,
    trace_id: str,
    ad: AdContract,
    verdict: VerdictContract,
    step_count: int,
    iteration: int,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Upsert worker_traces row reflecting the latest re-check result."""
    meta = {
        "edit_iteration": iteration,
        "recheck": True,
        "verdict_summary": verdict.summary,
        **(metadata or {}),
    }
    sql = """
    INSERT INTO worker_traces (
        trace_id, batch_id, ad_id, client_id, status, verdict, step_count, metadata, end_time
    ) VALUES (%s, %s, %s, %s, 'completed', %s, %s, %s, CURRENT_TIMESTAMP)
    ON CONFLICT (trace_id) DO UPDATE SET
        status = 'completed',
        verdict = EXCLUDED.verdict,
        step_count = EXCLUDED.step_count,
        metadata = EXCLUDED.metadata,
        end_time = CURRENT_TIMESTAMP;
    """
    try:
        connection.execute(
            sql,
            (
                trace_id,
                ad.batch_id,
                ad.ad_id,
                ad.client_id,
                verdict.status.value,
                step_count,
                json.dumps(meta),
            ),
        )
    except Exception as exc:
        logger.debug("Failed recording re-check trace for ad %s: %s", ad.ad_id, exc)


def get_ad_recheck_status(connection: Any, ad_id: str) -> dict[str, Any]:
    """Retrieve re-check iteration metrics and remaining loop allowance for an ad."""
    edit_count = count_ad_edits(connection, ad_id)
    loop_cap = max_edit_loops()
    remaining = max(0, loop_cap - edit_count)
    return {
        "ad_id": ad_id,
        "edit_count": edit_count,
        "max_edit_loops": loop_cap,
        "remaining_loops": remaining,
        "is_loop_capped": edit_count >= loop_cap,
    }


def recheck_ad(
    ad_id_or_ad: str | AdContract,
    *,
    connection: Any = None,
    checkpointer: Any = None,
    tracer: Any = None,
    manifest: Mapping[str, Mapping[str, Any]] | None = None,
    platform_limits: Mapping[str, Any] | None = None,
    run_page_checks: bool = False,
    run_wording_checks: bool = False,
    file_store: LocalFileStore | UnifiedFileStore | None = None,
    model_client: Any = None,
    embed_fn: Any = None,
    retrieved_rules: Sequence[Any] | None = None,
    allowed_origins: frozenset[str] | set[str] | None = None,
    page_timeout_ms: int = 5000,
    as_of: date | None = None,
    max_steps: int = DEFAULT_MAX_STEPS_PER_ROW,
    max_loops: int | None = None,
    explicit_edit_count: int | None = None,
    previous_row_hash: str | None = None,
) -> RecheckResult:
    """Execute bounded re-check flow on an edited ad, enforcing step and loop caps.

    Flow:
    1. Resolve ad record from database or provided AdContract.
    2. Determine current edit iteration from ad_edits and check bounded loop cap.
    3. Clear stale checkpointer thread state for new content version.
    4. Execute bounded LangGraph flow (Task 34) on the updated ad.
    5. If loop cap is reached and findings remain, escalate to 'needs a person' (PRD D17).
    6. Replace stale findings in PostgreSQL and record trace in worker_traces.
    7. Return structured RecheckResult.
    """
    effective_max_loops = max_loops if max_loops is not None else max_edit_loops()

    # 1. Resolve AdContract
    if isinstance(ad_id_or_ad, AdContract):
        ad = ad_id_or_ad
    else:
        ad_id = str(ad_id_or_ad)
        if connection is None:
            with get_session() as conn:
                return recheck_ad(
                    ad_id,
                    connection=conn,
                    checkpointer=checkpointer,
                    tracer=tracer,
                    manifest=manifest,
                    platform_limits=platform_limits,
                    run_page_checks=run_page_checks,
                    run_wording_checks=run_wording_checks,
                    file_store=file_store,
                    model_client=model_client,
                    embed_fn=embed_fn,
                    retrieved_rules=retrieved_rules,
                    allowed_origins=allowed_origins,
                    page_timeout_ms=page_timeout_ms,
                    as_of=as_of,
                    max_steps=max_steps,
                    max_loops=effective_max_loops,
                    explicit_edit_count=explicit_edit_count,
                    previous_row_hash=previous_row_hash,
                )

        cur = connection.execute(
            """
            SELECT ad_id, batch_id, client_id, row_number, platform, ad_text, headline,
                   destination_url, row_hash, sheet_hash, creative_file
            FROM ads
            WHERE ad_id = %s;
            """,
            (ad_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Ad {ad_id!r} not found in database")

        if isinstance(row, Mapping):
            ad = AdContract(
                ad_id=row["ad_id"],
                batch_id=row["batch_id"],
                client_id=row["client_id"],
                row_number=int(row["row_number"]),
                platform=row["platform"],
                ad_text=row.get("ad_text") or None,
                headline=row.get("headline") or None,
                destination_url=row["destination_url"],
                creative_file=row.get("creative_file") or None,
                row_hash=row["row_hash"],
                sheet_hash=row["sheet_hash"],
            )
        else:
            ad = AdContract(
                ad_id=row[0],
                batch_id=row[1],
                client_id=row[2],
                row_number=int(row[3]),
                platform=row[4],
                ad_text=row[5] or None,
                headline=row[6] or None,
                destination_url=row[7],
                row_hash=row[8],
                sheet_hash=row[9],
                creative_file=row[10] or None,
            )

    # 2. Determine edit iteration and check if bounded loop cap is reached
    if explicit_edit_count is not None:
        iteration = explicit_edit_count
    elif connection is not None:
        iteration = count_ad_edits(connection, ad.ad_id)
    else:
        iteration = 1

    is_loop_capped = iteration >= effective_max_loops

    # 3. Clean stale checkpointer thread state for fresh re-check
    thread_id = None
    if ad.batch_id and ad.ad_id:
        try:
            thread_id = make_thread_id(ad.batch_id, ad.ad_id)
            if checkpointer is not None and hasattr(checkpointer, "delete_thread"):
                checkpointer.delete_thread(thread_id)
        except Exception as exc:
            logger.debug("Failed deleting stale checkpointer thread: %s", exc)

    # 4. Execute LangGraph flow (Task 34)
    flow_result = run_ad_worker_flow(
        ad,
        connection=connection,
        file_store=file_store,
        checkpointer=checkpointer,
        thread_id=thread_id,
        resume=False,
        tracer=tracer,
        trace_id=thread_id,
        manifest=manifest,
        platform_limits=platform_limits,
        run_page_checks=run_page_checks,
        run_wording_checks=run_wording_checks,
        model_client=model_client,
        embed_fn=embed_fn,
        retrieved_rules=retrieved_rules,
        allowed_origins=allowed_origins,
        page_timeout_ms=page_timeout_ms,
        as_of=as_of,
        max_steps=max_steps,
    )

    findings = list(flow_result.findings)
    verdict = flow_result.verdict
    needs_person = flow_result.needs_person
    step_count = flow_result.step_count
    shared_state = flow_result.shared_state

    # 5. Enforce bounded loop cap (PRD D17):
    # If the ad reached maximum edit loops and still has unresolved findings,
    # force verdict to 'needs a person' with an explanatory finding.
    if is_loop_capped and len(findings) > 0:
        logger.warning(
            "Ad %s reached maximum edit loops (%d >= %d) with unresolved findings; routing to person",
            ad.ad_id,
            iteration,
            effective_max_loops,
        )
        cap_finding = FindingContract(
            finding_id=f"{ad.ad_id}:recheck:max_loops_exceeded",
            ad_id=ad.ad_id,
            row_number=ad.row_number,
            check_type=CheckType.MECHANICAL,
            message=f"Maximum edit loops ({effective_max_loops}) reached with unresolved findings; requires manual reviewer resolution.",
            field=None,
            proposed_fix="A human reviewer must resolve remaining issues or approve/reject with exception.",
            rule_id="gate.recheck_loop_cap",
            file_ref=None,
        )
        findings.append(cap_finding)
        needs_person = True
        verdict = aggregate_ad_verdict(
            ad,
            findings,
            needs_person=True,
            verdict_floor=VerdictStatus.NEEDS_PERSON,
            step_count=step_count,
            max_steps=max_steps,
        )
        shared_state.status = verdict.status.value
        shared_state.summary = verdict.summary

    # 6. Database updates: replace stale findings and record trace
    if connection is not None:
        replace_ad_findings(connection, ad.ad_id, findings)
        if thread_id is not None:
            record_recheck_trace(
                connection,
                trace_id=thread_id,
                ad=ad,
                verdict=verdict,
                step_count=step_count,
                iteration=iteration,
            )

    # Resolve previous row hash from edit history if available
    prev_hash = previous_row_hash
    if prev_hash is None and connection is not None:
        history = get_ad_edits_history(connection, ad.ad_id)
        if history:
            prev_hash = history[-1].get("previous_row_hash")

    # Broadcast SSE live update
    try:
        from apps.api.sse import notify_ad_update
        notify_ad_update(
            ad.batch_id,
            ad.ad_id,
            verdict=verdict.status.value,
            row_number=ad.row_number,
            client_id=ad.client_id,
            findings_count=len(findings),
        )
    except Exception as exc:
        logger.debug("Failed broadcasting SSE update for recheck on ad %s: %s", ad.ad_id, exc)

    return RecheckResult(
        ad=ad,
        verdict=verdict,
        findings=findings,
        edit_iteration=iteration,
        max_loops=effective_max_loops,
        is_loop_capped=is_loop_capped,
        needs_person=needs_person,
        step_count=step_count,
        shared_state=shared_state,
        trace_id=thread_id,
        previous_row_hash=prev_hash,
        new_row_hash=ad.row_hash,
    )
