"""Worker runner for consuming ad jobs from Redis and executing mechanical and page checks."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import csv
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
from typing import Any

from psycopg import Connection
from redis import Redis

from apps.api.client_inference import allowed_origins_for_client, load_domain_map
from apps.api.database import get_session
from apps.api.findings import persist_findings_on_connection
from apps.api.queue import AD_JOB_QUEUE, AdJob, _redis_client
from apps.worker.file_storage import LocalFileStore, UnifiedFileStore, get_file_store
from apps.worker.page_browser import PageOpenError
from apps.worker.page_offer_check import check_offer_price_dates
from apps.worker.page_text_capture import capture_page_snapshot
from apps.worker.shared_state import SharedState
from apps.worker.utm_redirect_check import check_utm_survives_redirect
from packages.contracts import AdContract, CheckType, FindingContract
from packages.rules.creative_size_ratio import check_creative_size_ratio
from packages.rules.platform_text_limits import check_platform_text_limits
from packages.rules.stale_platform_limits import check_stale_platform_limits
from packages.rules.utm_naming import check_utm_naming
from packages.rules.utm_presence import check_utm_presence

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class WorkerJob:
    """Worker job representation supporting client_id scoping."""

    batch_id: str
    ad_id: str
    client_id: str | None = None


@dataclass(frozen=True)
class WorkerFailure:
    """Record of a failed or skipped worker job execution."""

    job: Any
    reason: str
    error: str | None = None


RECORDED_FAILURES: list[WorkerFailure] = []
LATEST_SHARED_STATE: dict[str, SharedState] = {}


def record_failure(job: Any, reason: str, *, error: str | None = None) -> WorkerFailure:
    """Record a failure for inspection and logging."""
    failure = WorkerFailure(job=job, reason=reason, error=error)
    RECORDED_FAILURES.append(failure)
    logger.error("Worker failure: %s (job=%r, error=%s)", reason, job, error)
    return failure


def get_recorded_failures() -> list[WorkerFailure]:
    """Return all recorded failures."""
    return list(RECORDED_FAILURES)


def clear_recorded_failures() -> None:
    """Clear recorded failures."""
    RECORDED_FAILURES.clear()


def get_latest_shared_state(ad_id: str) -> SharedState | None:
    """Return latest thin SharedState for an ad."""
    return LATEST_SHARED_STATE.get(ad_id)


def clear_latest_shared_state() -> None:
    """Clear recorded shared state."""
    LATEST_SHARED_STATE.clear()


@lru_cache(maxsize=1)
def load_creative_manifest(path: str | Path | None = None) -> dict[str, dict[str, int]]:
    """Load the creatives manifest file into a dictionary."""
    manifest_path = Path(path) if path else REPO_ROOT / "data" / "creatives_manifest.csv"
    with manifest_path.open(newline="", encoding="utf-8") as source:
        return {
            row["file"]: {"width": int(row["width"]), "height": int(row["height"])}
            for row in csv.DictReader(source)
        }


@lru_cache(maxsize=1)
def load_platform_limits(path: str | Path | None = None) -> dict[str, Any]:
    """Load the decoded platform limits JSON configuration."""
    limits_path = Path(path) if path else REPO_ROOT / "data" / "platform_limits.json"
    return json.loads(limits_path.read_text(encoding="utf-8"))


def _extract_job_identifiers(job: Any, explicit_client_id: str | None = None) -> tuple[str, str, str | None]:
    """Extract ad_id, batch_id, and client_id from various job representations."""
    if isinstance(job, Mapping):
        ad_id = str(job.get("ad_id", "")).strip()
        batch_id = str(job.get("batch_id", "")).strip()
        client_id = explicit_client_id or job.get("client_id")
        return ad_id, batch_id, client_id
    if isinstance(job, str):
        return job.strip(), "", explicit_client_id
    ad_id = str(getattr(job, "ad_id", "")).strip()
    batch_id = str(getattr(job, "batch_id", "")).strip()
    client_id = explicit_client_id or getattr(job, "client_id", None)
    return ad_id, batch_id, client_id


def _page_open_error_finding(
    ad: AdContract,
    error: PageOpenError,
    *,
    file_ref: str | None = None,
) -> FindingContract:
    reason = getattr(error, "reason_code", "page_open_failed")
    return FindingContract(
        finding_id=f"{ad.ad_id}:page_load:{reason}",
        ad_id=ad.ad_id,
        row_number=ad.row_number,
        check_type=CheckType.PAGE,
        message=f"Page failed to open ({reason}): {error}",
        field="destination_url",
        proposed_fix="Ensure the destination URL is valid, reachable, on the allowed domain, and loads within the timeout.",
        rule_id=f"page.{reason}",
        file_ref=file_ref,
        actual_value=f"HTTP {error.http_status}" if error.http_status is not None else None,
        expected_format="successful HTTP response with readable page content",
    )


async def _execute_page_checks(
    ad: AdContract,
    *,
    allowed_origins: frozenset[str] | set[str] | None = None,
    timeout_ms: int = 5000,
    file_store: LocalFileStore | UnifiedFileStore | None = None,
    as_of: date | None = None,
) -> tuple[list[FindingContract], SharedState]:
    """Execute UTM redirect checks and page offer checks against rendered page text."""
    store = file_store or get_file_store()
    if allowed_origins is None:
        domain_map = load_domain_map(include_local_storefront=True)
        origins = allowed_origins_for_client(ad.client_id, domain_map)
    else:
        origins = frozenset(allowed_origins)

    as_of_date = as_of or date.today()
    page_findings: list[FindingContract] = []
    file_refs: list[str] = []

    # 1. UTM-survives-redirect
    try:
        utm_findings = await check_utm_survives_redirect(
            ad,
            timeout_ms=timeout_ms,
            file_store=store,
        )
        page_findings.extend(utm_findings)
        for f in utm_findings:
            if f.file_ref and f.file_ref not in file_refs:
                file_refs.append(f.file_ref)
    except PageOpenError as error:
        finding = _page_open_error_finding(ad, error)
        page_findings.append(finding)
        shared_state = SharedState(
            ad_id=ad.ad_id,
            client_id=ad.client_id,
            status="page_check_failed",
            summary=f"Page check failed: {error.reason_code}",
            file_refs=file_refs,
        )
        return page_findings, shared_state

    # 2. Offer/price/date comparison with the ad
    capture_result = await capture_page_snapshot(
        ad.destination_url,
        allowed_origins=origins,
        timeout_ms=timeout_ms,
        file_store=store,
    )

    if capture_result.page_error is not None:
        finding = _page_open_error_finding(ad, capture_result.page_error)
        page_findings.append(finding)
    else:
        snapshot = capture_result.page_snapshot
        if snapshot.get("screenshot_ref") and snapshot["screenshot_ref"] not in file_refs:
            file_refs.append(snapshot["screenshot_ref"])
        if snapshot.get("text_ref") and snapshot["text_ref"] not in file_refs:
            file_refs.append(snapshot["text_ref"])

        offer_findings = check_offer_price_dates(ad, snapshot, as_of=as_of_date)
        page_findings.extend(offer_findings)
        for f in offer_findings:
            if f.file_ref and f.file_ref not in file_refs:
                file_refs.append(f.file_ref)

    summary = (
        f"Page check completed with {len(page_findings)} finding(s)."
        if page_findings
        else "Page check completed clean."
    )
    shared_state = SharedState(
        ad_id=ad.ad_id,
        client_id=ad.client_id,
        status="page_check_completed",
        summary=summary,
        file_refs=file_refs,
    )
    return page_findings, shared_state


def process_job(
    job: Any,
    *,
    client_id: str | None = None,
    connection: Connection | None = None,
    manifest: Mapping[str, Mapping[str, Any]] | None = None,
    platform_limits: Mapping[str, Any] | None = None,
    run_page_checks: bool = True,
    allowed_origins: frozenset[str] | set[str] | None = None,
    page_timeout_ms: int = 5000,
    file_store: LocalFileStore | None = None,
    as_of: date | None = None,
    run_wording_checks: bool = False,
    model_client: Any = None,
    embed_fn: Any = None,
    retrieved_rules: Sequence[Any] | None = None,
) -> list[FindingContract] | None:
    """Process one ad job by id, running mechanical checks, page checks, wording checks, and persisting findings.

    Returns the list of generated findings, or None if the job was skipped/failed.
    """
    ad_id, batch_id, expected_client_id = _extract_job_identifiers(job, client_id)
    if not ad_id:
        record_failure(job, "missing ad_id in job")
        return None

    manifest_map = manifest if manifest is not None else load_creative_manifest()
    limits_config = platform_limits if platform_limits is not None else load_platform_limits()
    platforms_map = limits_config.get("platforms", {})

    def _execute(conn: Connection) -> list[FindingContract] | None:
        if expected_client_id is not None:
            # Query scoped to client_id
            cursor = conn.execute(
                """
                SELECT ad_id, batch_id, client_id, row_number, platform, ad_text, headline,
                       destination_url, creative_file, row_hash, sheet_hash
                FROM ads
                WHERE ad_id = %s AND client_id = %s
                """,
                (ad_id, expected_client_id),
            )
            row = cursor.fetchone()
            if row is None:
                # Check whether the ad exists under another client
                exists_cursor = conn.execute(
                    "SELECT client_id FROM ads WHERE ad_id = %s",
                    (ad_id,),
                )
                mismatch_row = exists_cursor.fetchone()
                if mismatch_row is not None:
                    actual_client = mismatch_row[0] if isinstance(mismatch_row, (tuple, list)) else mismatch_row.get("client_id")
                    record_failure(
                        job,
                        f"client_id mismatch: job specified {expected_client_id!r}, ad belongs to {actual_client!r}",
                    )
                else:
                    record_failure(job, f"ad_id {ad_id!r} not found in database")
                return None
        else:
            cursor = conn.execute(
                """
                SELECT ad_id, batch_id, client_id, row_number, platform, ad_text, headline,
                       destination_url, creative_file, row_hash, sheet_hash
                FROM ads
                WHERE ad_id = %s
                """,
                (ad_id,),
            )
            row = cursor.fetchone()
            if row is None:
                record_failure(job, f"ad_id {ad_id!r} not found in database")
                return None

        # Build AdContract
        if isinstance(row, Mapping):
            ad_contract = AdContract(
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
            ad_contract = AdContract(
                ad_id=row[0],
                batch_id=row[1],
                client_id=row[2],
                row_number=int(row[3]),
                platform=row[4],
                ad_text=row[5] or None,
                headline=row[6] or None,
                destination_url=row[7],
                creative_file=row[8] or None,
                row_hash=row[9],
                sheet_hash=row[10],
            )

        # 0. Initialize Tracing (Postgres + Langfuse if configured)
        from apps.worker.tracing import get_default_tracer
        tracer = get_default_tracer(conn)
        trace_id = f"{ad_contract.batch_id}:{ad_contract.ad_id}"
        try:
            tracer.start_trace(
                trace_id=trace_id,
                batch_id=ad_contract.batch_id,
                ad_id=ad_contract.ad_id,
                client_id=ad_contract.client_id,
                metadata={"platform": ad_contract.platform, "headline": ad_contract.headline},
            )
        except Exception as exc:
            logger.debug("Failed start_trace for %s: %s", trace_id, exc)

        # 1. Collect mechanical spec findings (ratios, stale limits, text limits)
        initial_findings: list[FindingContract] = []
        initial_findings.extend(check_stale_platform_limits(ad_contract, limits_config))
        initial_findings.extend(check_creative_size_ratio(ad_contract, manifest_map, platforms_map))
        initial_findings.extend(check_platform_text_limits(ad_contract, platforms_map))

        # 2. Execute Specialized Multi-Agent Worker Team with Autonomous Feedback Loop
        from apps.worker.graph import run_multi_agent_worker_flow

        result = run_multi_agent_worker_flow(
            ad_contract,
            shared_state=LATEST_SHARED_STATE.get(ad_contract.ad_id),
            initial_findings=initial_findings,
            connection=conn,
            file_store=file_store,
            manifest=manifest_map,
            platform_limits=limits_config,
            run_page_checks=run_page_checks,
            run_wording_checks=run_wording_checks,
            model_client=model_client,
            embed_fn=embed_fn,
            retrieved_rules=retrieved_rules,
            tracer=tracer,
            trace_id=trace_id,
            allowed_origins=allowed_origins,
            page_timeout_ms=page_timeout_ms,
            as_of=as_of,
        )

        findings = result.findings
        refined_ad = result.ad
        LATEST_SHARED_STATE[ad_contract.ad_id] = result.shared_state
        if hasattr(job, "__dict__"):
            try:
                setattr(job, "shared_state", result.shared_state)
            except Exception:
                pass

        # 3. If ad was refined autonomously by the supervisor loop, preserve in database
        if refined_ad.headline != ad_contract.headline or refined_ad.ad_text != ad_contract.ad_text:
            logger.info(
                "[AUTONOMOUS REFINEMENT] Preserving refined ad in DB for Ad %s: headline='%s' (was '%s')",
                ad_contract.ad_id, refined_ad.headline, ad_contract.headline,
            )
            from apps.api.ingest import hash_normalized_row
            new_row_hash = hash_normalized_row({
                "platform": refined_ad.platform,
                "headline": refined_ad.headline or "",
                "ad_text": refined_ad.ad_text or "",
                "destination_url": refined_ad.destination_url,
                "creative_file": refined_ad.creative_file or "",
            })
            try:
                conn.execute(
                    """
                    UPDATE ads
                    SET headline = %s,
                        ad_text = %s,
                        row_hash = %s
                    WHERE ad_id = %s;
                    """,
                    (refined_ad.headline, refined_ad.ad_text, new_row_hash, ad_contract.ad_id),
                )
            except Exception as exc:
                logger.warning("Failed updating refined ad in database: %s", exc)

        # If this ad was edited, delete stale findings from prior iterations before persisting new findings
        from apps.api.edits import count_ad_edits
        try:
            if count_ad_edits(conn, ad_contract.ad_id) > 0:
                conn.execute("DELETE FROM findings WHERE ad_id = %s;", (ad_contract.ad_id,))
        except Exception as exc:
            logger.debug("Failed checking or deleting stale findings for ad %s: %s", ad_contract.ad_id, exc)

        # 4. Persist findings idempotently
        persist_findings_on_connection(conn, findings)

        # 5. Broadcast SSE live update & Finalize Trace
        v_res = result.verdict

        try:
            tracer.end_trace(
                trace_id=trace_id,
                verdict=v_res.status.value,
                step_count=result.step_count,
            )
            tracer.flush()
        except Exception as exc:
            logger.debug("Failed finalizing trace %s: %s", trace_id, exc)

        try:
            from apps.api.sse import notify_ad_update
            notify_ad_update(
                ad_contract.batch_id,
                ad_contract.ad_id,
                stage="complete",
                verdict=v_res.status.value,
                row_number=ad_contract.row_number,
                client_id=ad_contract.client_id,
                findings_count=len(findings),
            )
        except Exception as exc:
            logger.debug("Failed broadcasting SSE live update for ad %s: %s", ad_contract.ad_id, exc)

        return findings

    if connection is not None:
        return _execute(connection)

    with get_session() as conn:
        return _execute(conn)


def run_once(
    *,
    redis_client: Redis | None = None,
    connection: Connection | None = None,
    manifest: Mapping[str, Mapping[str, Any]] | None = None,
    platform_limits: Mapping[str, Any] | None = None,
    run_page_checks: bool = True,
    allowed_origins: frozenset[str] | set[str] | None = None,
    page_timeout_ms: int = 5000,
    file_store: LocalFileStore | None = None,
    as_of: date | None = None,
    run_wording_checks: bool = True,
    model_client: Any = None,
    embed_fn: Any = None,
    retrieved_rules: Sequence[Any] | None = None,
    reraise: bool = False,
) -> AdJob | None:
    """Pop ONE job from Redis, process it, and acknowledge it only after findings are saved.

    If processing raises, the job is not lost (left retryable in Redis) and the failure is recorded.
    """
    client = redis_client or _redis_client()
    should_close_redis = redis_client is None
    try:
        raw_message = client.lpop(AD_JOB_QUEUE)
        if raw_message is None:
            return None

        try:
            payload = json.loads(raw_message)
            if (
                not isinstance(payload, dict)
                or not {"batch_id", "ad_id"}.issubset(set(payload))
                or not isinstance(payload["batch_id"], str)
                or not isinstance(payload["ad_id"], str)
                or not payload["batch_id"].strip()
                or not payload["ad_id"].strip()
            ):
                raise ValueError("invalid job structure in Redis queue")
            job = AdJob(batch_id=payload["batch_id"], ad_id=payload["ad_id"])
            job_client_id = payload.get("client_id")
        except Exception as exc:
            record_failure(None, "malformed job payload in Redis queue", error=str(exc))
            return None

        try:
            process_job(
                job,
                client_id=job_client_id,
                connection=connection,
                manifest=manifest,
                platform_limits=platform_limits,
                run_page_checks=run_page_checks,
                allowed_origins=allowed_origins,
                page_timeout_ms=page_timeout_ms,
                file_store=file_store,
                as_of=as_of,
                run_wording_checks=run_wording_checks,
                model_client=model_client,
                embed_fn=embed_fn,
                retrieved_rules=retrieved_rules,
            )
            # Acknowledged: findings are saved, job was popped from Redis and not pushed back.
            return job
        except Exception as exc:
            # Leave job retryable by pushing it back to Redis
            client.lpush(AD_JOB_QUEUE, raw_message)
            record_failure(job, "job processing raised an exception", error=str(exc))
            if reraise:
                raise
            return None
    finally:
        if should_close_redis:
            client.close()
