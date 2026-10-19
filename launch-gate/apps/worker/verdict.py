"""Verdict engine and aggregation for ads and batches.

Produces ad and batch verdicts across four defined statuses (PRD §4.5, Contracts §1):
1. 'ready': ad passed all checks with 0 findings and no human review flags.
2. 'fix needed': ad has mechanical, page, or wording findings that a reviewer can correct.
3. 'blocked': ad has fatal violations (expired offer, forbidden claim, blocked host).
4. 'needs a person': ad requires human judgment (unknown/ambiguous claim, unverified citation,
   stale platform limits, advisory guard floor, spend cap breach, or step cap breach).

Enforces:
- Hard cap on steps per row (max 5 steps).
- Hard cap on spend per row (delegated from cost ledger).
- Structured summary naming what is missing or required for human review.
- Batch-level one-screen summary with verified referential integrity (BatchContract).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import re
from typing import Any

from packages.contracts import (
    AdContract,
    BatchContract,
    CheckType,
    FindingContract,
    VerdictContract,
    VerdictStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS_PER_ROW = 5

_BLOCKED_FINDING_PATTERNS = re.compile(
    r"(page_expired_offer|expired|sale has ended|offer ended|forbidden_claim|"
    r"forbidden claim|remove the claim|blocked_host|private_ip|host_not_allowed|"
    r"destination url is not allowed|blocked|page_not_found|page:not_found|404|handmade_misuse)",
    re.IGNORECASE,
)

_NEEDS_PERSON_FINDING_PATTERNS = re.compile(
    r"(unknown_claim|cannot be verified|not in the register|ambiguous|"
    r"needs a person|needs_person|unverified_citation|unverified|invented rule|"
    r"spend_cap_exceeded|spend cap exceeded|stale_platform_limits|older than|"
    r"confirm limits|stale|no_retrieved_rules|no client rules|model_error|"
    r"rejected_model_output|safety guard|rejected by guard)",
    re.IGNORECASE,
)


_MODEL_UNAVAILABLE_FINDING_PATTERNS = re.compile(
    r"(model_unavailable|rate_limit|rate limit|quota|429|resource_exhausted|model unavailable)",
    re.IGNORECASE,
)


def classify_finding_severity(finding: FindingContract) -> VerdictStatus:
    """Classify the severity of an individual finding into VerdictStatus."""
    finding_id = finding.finding_id.casefold()
    message = finding.message.casefold()
    field = (finding.field or "").casefold()

    # 1. Check for MODEL_UNAVAILABLE severity
    if (
        "model_unavailable" in finding_id
        or _MODEL_UNAVAILABLE_FINDING_PATTERNS.search(finding_id)
        or _MODEL_UNAVAILABLE_FINDING_PATTERNS.search(message)
    ):
        return VerdictStatus.MODEL_UNAVAILABLE

    # 2. Check for BLOCKED severity
    if (
        "page_expired_offer" in finding_id
        or "forbidden_claim" in finding_id
        or _BLOCKED_FINDING_PATTERNS.search(finding_id)
        or _BLOCKED_FINDING_PATTERNS.search(message)
    ):
        return VerdictStatus.BLOCKED

    # 3. Check for NEEDS_PERSON severity (real uncertainty: unknown/ambiguous/unverified)
    if (
        "unknown" in finding_id
        or "unverified" in finding_id
        or "spend_cap" in finding_id
        or "stale" in finding_id
        or "guard" in finding_id
        or "needs_person" in finding_id
        or _NEEDS_PERSON_FINDING_PATTERNS.search(finding_id)
        or _NEEDS_PERSON_FINDING_PATTERNS.search(message)
    ):
        return VerdictStatus.NEEDS_PERSON

    # 4. Default for mechanical and standard page/wording findings is FIX_NEEDED
    return VerdictStatus.FIX_NEEDED


def aggregate_ad_verdict(
    ad: AdContract,
    findings: Sequence[FindingContract],
    *,
    verdict_floor: VerdictStatus | None = None,
    needs_person: bool = False,
    model_unavailable: bool = False,
    step_count: int | None = None,
    max_steps: int = DEFAULT_MAX_STEPS_PER_ROW,
    summary: str | None = None,
) -> VerdictContract:
    """Aggregate findings and guards into a single VerdictContract for an ad."""
    # Ensure all findings belong to this ad
    ad_findings: list[FindingContract] = []
    for f in findings:
        if f.ad_id == ad.ad_id:
            ad_findings.append(f)
        else:
            logger.warning("Finding %s ignored: ad_id mismatch with %s", f.finding_id, ad.ad_id)

    finding_ids = [f.finding_id for f in ad_findings]

    # Check step cap per row
    step_cap_exceeded = False
    if step_count is not None and step_count > max_steps:
        step_cap_exceeded = True
        needs_person = True

    # Classify individual findings
    has_blocked = False
    has_model_unavailable = model_unavailable
    has_needs_person = needs_person
    has_fix_needed = False

    blocked_reasons: list[str] = []
    model_unavailable_reasons: list[str] = []
    needs_person_reasons: list[str] = []
    fix_fields: list[str] = []

    if step_cap_exceeded:
        needs_person_reasons.append(f"Step cap exceeded ({step_count} > {max_steps} steps)")

    for f in ad_findings:
        sev = classify_finding_severity(f)
        if sev is VerdictStatus.BLOCKED:
            has_blocked = True
            blocked_reasons.append(f.message)
        elif sev is VerdictStatus.MODEL_UNAVAILABLE:
            has_model_unavailable = True
            model_unavailable_reasons.append(f.message)
        elif sev is VerdictStatus.NEEDS_PERSON:
            has_needs_person = True
            needs_person_reasons.append(f.message)
        else:
            has_fix_needed = True
            field_name = f.field or "general"
            if field_name not in fix_fields:
                fix_fields.append(field_name)

    # Evaluate verdict floor from advisory input/page guards
    if verdict_floor is VerdictStatus.BLOCKED:
        has_blocked = True
    elif verdict_floor is VerdictStatus.MODEL_UNAVAILABLE:
        has_model_unavailable = True
    elif verdict_floor is VerdictStatus.NEEDS_PERSON:
        has_needs_person = True
        needs_person_reasons.append("Advisory guard floor: human review required")

    # Determine final status according to hierarchy:
    # BLOCKED > MODEL_UNAVAILABLE > NEEDS_PERSON > FIX_NEEDED > READY
    if has_blocked:
        status = VerdictStatus.BLOCKED
        if not summary:
            first_reason = blocked_reasons[0] if blocked_reasons else "Fatal policy or offer violation"
            summary = f"Blocked: {first_reason}."
    elif has_model_unavailable:
        status = VerdictStatus.MODEL_UNAVAILABLE
        if not summary:
            first_reason = model_unavailable_reasons[0] if model_unavailable_reasons else "Upstream model rate-limited or temporarily unavailable"
            summary = f"Model unavailable: {first_reason}."
    elif has_needs_person:
        status = VerdictStatus.NEEDS_PERSON
        if not summary:
            missing_detail = needs_person_reasons[0] if needs_person_reasons else "Unverified claim or guard trigger"
            summary = f"Needs human review: {missing_detail}."
    elif has_fix_needed or ad_findings:
        status = VerdictStatus.FIX_NEEDED
        if not summary:
            fields_str = ", ".join(fix_fields) if fix_fields else "content"
            summary = f"Fix needed: {len(ad_findings)} finding(s) on {fields_str}."
    else:
        status = VerdictStatus.READY
        if not summary:
            summary = "Ready for launch: all checks passed with 0 findings."

    return VerdictContract(
        ad_id=ad.ad_id,
        status=status,
        finding_ids=finding_ids,
        summary=summary,
    )


@dataclass(frozen=True)
class BatchVerdictReport:
    """Summary and roll-up of all verdicts across an uploaded ad batch."""

    batch_id: str
    total_ads: int
    ready_count: int
    fix_needed_count: int
    blocked_count: int
    needs_person_count: int
    verdicts: list[VerdictContract]
    summary: str
    batch_contract: BatchContract
    model_unavailable_count: int = 0


def aggregate_batch_verdicts(
    batch_id: str,
    ads: Sequence[AdContract],
    findings: Sequence[FindingContract],
    *,
    verdict_floors: Mapping[str, VerdictStatus] | None = None,
    needs_person_flags: Mapping[str, bool] | None = None,
    step_counts: Mapping[str, int] | None = None,
    max_steps: int = DEFAULT_MAX_STEPS_PER_ROW,
) -> BatchVerdictReport:
    """Aggregate individual ad verdicts into a verified BatchContract and report."""
    floors = verdict_floors or {}
    person_flags = needs_person_flags or {}
    steps = step_counts or {}

    # Group findings strictly by ad_id
    findings_by_ad: dict[str, list[FindingContract]] = {ad.ad_id: [] for ad in ads}
    for f in findings:
        if f.ad_id in findings_by_ad:
            findings_by_ad[f.ad_id].append(f)
        else:
            logger.warning("Finding %s references ad_id %s not in batch %s", f.finding_id, f.ad_id, batch_id)

    verdicts: list[VerdictContract] = []
    ready_count = 0
    fix_needed_count = 0
    blocked_count = 0
    needs_person_count = 0
    model_unavailable_count = 0

    for ad in ads:
        ad_findings = findings_by_ad.get(ad.ad_id, [])
        floor = floors.get(ad.ad_id)
        needs_person = person_flags.get(ad.ad_id, False)
        step_count = steps.get(ad.ad_id)

        verdict = aggregate_ad_verdict(
            ad,
            ad_findings,
            verdict_floor=floor,
            needs_person=needs_person,
            step_count=step_count,
            max_steps=max_steps,
        )
        verdicts.append(verdict)

        if verdict.status is VerdictStatus.READY:
            ready_count += 1
        elif verdict.status is VerdictStatus.FIX_NEEDED:
            fix_needed_count += 1
        elif verdict.status is VerdictStatus.BLOCKED:
            blocked_count += 1
        elif verdict.status is VerdictStatus.NEEDS_PERSON:
            needs_person_count += 1
        elif verdict.status is VerdictStatus.MODEL_UNAVAILABLE:
            model_unavailable_count += 1

    total_ads = len(ads)
    summary = (
        f"Batch summary ({batch_id}): {total_ads} ad(s) checked. "
        f"{ready_count} ready, {fix_needed_count} fix needed, "
        f"{blocked_count} blocked, {needs_person_count} needs a person."
    )

    batch_contract = BatchContract(
        batch_id=batch_id,
        ads=list(ads),
        findings=list(findings),
        verdicts=verdicts,
        summary=summary,
    )

    return BatchVerdictReport(
        batch_id=batch_id,
        total_ads=total_ads,
        ready_count=ready_count,
        fix_needed_count=fix_needed_count,
        blocked_count=blocked_count,
        needs_person_count=needs_person_count,
        model_unavailable_count=model_unavailable_count,
        verdicts=verdicts,
        summary=summary,
        batch_contract=batch_contract,
    )
