"""Specialized Multi-Agent Team with Autonomous Feedback Loop.

Decomposes campaign review into specialist agent nodes orchestrated by a Chief Supervisor:
1. HeadlineSpecialistNode: Checks platform text limits, prohibited cure/medical claims, style guidelines.
   Provides autonomous refinement via `refine_headline`.
2. UrlAndLandingPageNode: Destination URL audit, headless page capture, HTTP status, UTM verification.
3. OfferAndComplianceNode: Pricing, discounts, expiry dates, and pgvector brand safety compliance.
   Provides autonomous refinement via `refine_copy`.
4. SupervisorReviewerNode: The Head of the Team reconciling findings, evaluating fixability,
   routing to specialists for bounded autonomous refinement (max 3 loops), and logging audit history.
5. JudgeAuditorNode: Independent LLM Judge evaluating preliminary findings, dismissing false alarms,
   and certifying safety before final verdict generation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
import logging
import re
from typing import Any

from apps.worker.file_storage import LocalFileStore, UnifiedFileStore, get_file_store
from apps.worker.judge_eval import JudgeEvaluationResult, evaluate_with_llm_judge
from apps.worker.shared_state import SharedState
from apps.worker.verdict import aggregate_ad_verdict
from apps.worker.wording_check import check_wording
from packages.contracts import (
    AdContract,
    CheckType,
    FindingContract,
    VerdictContract,
    VerdictStatus,
)
from packages.rules.creative_size_ratio import check_creative_size_ratio
from packages.rules.platform_text_limits import check_platform_text_limits
from packages.rules.stale_platform_limits import check_stale_platform_limits
from packages.rules.utm_naming import check_utm_naming
from packages.rules.utm_presence import check_utm_presence

logger = logging.getLogger(__name__)

# Common default headline character limits per platform
DEFAULT_HEADLINE_LIMITS = {
    "facebook": 40,
    "instagram": 40,
    "google": 30,
    "tiktok": 40,
    "x": 70,
    "snapchat": 34,
}

PROHIBITED_CURE_PATTERN = re.compile(
    r"\b(cures?|miracle(?:\s+cure)?|heals?|treats?\s+cancer|reverses?\s+aging)\b",
    re.IGNORECASE,
)


@dataclass
class SupervisorDecision:
    """Decision emitted by SupervisorReviewerNode on whether to refine or escalate."""
    action: str  # "REFINE" or "APPROVE_OR_ESCALATE"
    target_specialist: str | None = None
    feedback: str | None = None
    loop_count: int = 0
    is_fixable: bool = False
    audit_entry: dict[str, Any] | None = None


class HeadlineSpecialistNode:
    """Specialist Node: Focuses strictly on ad headline compliance and autonomous refinement."""

    name = "headline_specialist"

    def check(
        self,
        ad: AdContract,
        platform_limits: Mapping[str, Any] | None = None,
    ) -> list[FindingContract]:
        """Audit headline for platform length limits and prohibited cure/medical claims."""
        findings: list[FindingContract] = []
        headline = (ad.headline or "").strip()
        platform_key = (ad.platform or "facebook").strip().lower()

        # 1. Platform character limit check
        platforms_map = (
            platform_limits.get("platforms", {})
            if isinstance(platform_limits, dict)
            else {}
        )
        if platforms_map:
            limit_findings = check_platform_text_limits(ad, platforms_map)
            findings.extend([f for f in limit_findings if f.field == "headline"])
        else:
            limit = DEFAULT_HEADLINE_LIMITS.get(platform_key, 40)
            if len(headline) > limit:
                findings.append(
                    FindingContract(
                        finding_id=f"{ad.ad_id}:headline:length_exceeded",
                        ad_id=ad.ad_id,
                        row_number=ad.row_number,
                        check_type=CheckType.MECHANICAL,
                        message=f"Headline exceeds {ad.platform} limit of {limit} characters (got {len(headline)}).",
                        field="headline",
                        proposed_fix=f"Shorten headline to {limit} characters or less.",
                    )
                )

        # 2. Prohibited medical/cure claims in headline
        if headline:
            match = PROHIBITED_CURE_PATTERN.search(headline)
            if match:
                matched_word = match.group(0)
                findings.append(
                    FindingContract(
                        finding_id=f"{ad.ad_id}:headline:prohibited_claim",
                        ad_id=ad.ad_id,
                        row_number=ad.row_number,
                        check_type=CheckType.WORDING,
                        message=f"Headline contains prohibited medical/cure claim: '{matched_word}'.",
                        field="headline",
                        proposed_fix=f"Replace '{matched_word}' with compliant phrasing like 'supports' or 'helps maintain'.",
                        rule_id="PROHIBITED_CURE_CLAIM",
                    )
                )

        return findings

    def refine_headline(
        self,
        current_headline: str,
        feedback: str | None = None,
        platform: str = "facebook",
        max_len: int | None = None,
    ) -> str:
        """Autonomously rewrite or shorten headline based on supervisor critique."""
        if not current_headline:
            return current_headline

        platform_key = platform.strip().lower()
        limit = max_len or DEFAULT_HEADLINE_LIMITS.get(platform_key, 40)
        refined = current_headline.strip()

        # 1. Replace prohibited cure keywords
        def _replace_cure(match: re.Match) -> str:
            w = match.group(0).lower()
            if w in ("cure", "cures"):
                return "Supports" if match.group(0)[0].isupper() else "supports"
            if "miracle" in w:
                return "Advanced Formula" if match.group(0)[0].isupper() else "advanced formula"
            if w in ("heal", "heals"):
                return "Soothes" if match.group(0)[0].isupper() else "soothes"
            if "revers" in w:
                return "Renews" if match.group(0)[0].isupper() else "renews"
            return "Helps Maintain"

        refined = PROHIBITED_CURE_PATTERN.sub(_replace_cure, refined)

        # 2. Shorten if length exceeds platform limit
        if len(refined) > limit:
            # Common word replacements to save space
            replacements = [
                ("Promotion", "Promo"),
                ("Special Offer", "Offer"),
                ("Discount", "Off"),
                ("Natural Soothing", "Soothing"),
                ("for daily soothing hydration", "Daily Hydration"),
                ("for daily hydration", "Daily Hydration"),
                ("Daily Soothing", "Daily"),
            ]
            for orig, rep in replacements:
                if len(refined) <= limit:
                    break
                refined = re.sub(re.escape(orig), rep, refined, flags=re.IGNORECASE)

            # If still over limit, truncate at nearest clean word boundary
            if len(refined) > limit:
                truncated = refined[:limit].rstrip()
                last_space = truncated.rfind(" ")
                if last_space > 10:
                    truncated = truncated[:last_space].rstrip(" ,.-:;!?")
                refined = truncated

        logger.info("[HeadlineSpecialist] Refined '%s' -> '%s' (len=%d, limit=%d)", current_headline, refined, len(refined), limit)
        return refined


class UrlAndLandingPageNode:
    """Specialist Node: Audits destination URLs, UTM parameters, and captures landing page text/snapshots."""

    name = "url_specialist"

    async def check(
        self,
        ad: AdContract,
        allowed_origins: frozenset[str] | set[str] | None = None,
        timeout_ms: int = 5000,
        file_store: LocalFileStore | UnifiedFileStore | None = None,
        as_of: date | None = None,
        run_page_checks: bool = True,
    ) -> tuple[list[FindingContract], SharedState]:
        """Perform URL parameter validation and live headless landing page verification."""
        findings: list[FindingContract] = []
        shared_state = SharedState(ad_id=ad.ad_id, client_id=ad.client_id, status="checking_url")

        # 1. Deterministic UTM checks
        findings.extend(check_utm_presence(ad))
        findings.extend(check_utm_naming(ad))

        # 2. Headless landing page verification
        if run_page_checks and ad.destination_url:
            from apps.worker.job_runner import _execute_page_checks
            try:
                page_findings, page_state = await _execute_page_checks(
                    ad,
                    allowed_origins=allowed_origins,
                    timeout_ms=timeout_ms,
                    file_store=file_store,
                    as_of=as_of,
                )
                findings.extend(page_findings)
                shared_state = page_state
            except Exception as exc:
                logger.warning("Landing page check encountered non-fatal error: %s", exc)

        return findings, shared_state


class OfferAndComplianceNode:
    """Specialist Node: Checks brand claims, pricing/discount terms, and vector compliance rules."""

    name = "offer_specialist"

    def check(
        self,
        ad: AdContract,
        shared_state: SharedState | None = None,
        connection: Any = None,
        file_store: LocalFileStore | UnifiedFileStore | None = None,
        retrieved_rules: Sequence[Any] | None = None,
        model_client: Any = None,
        embed_fn: Any = None,
        tracer: Any = None,
        trace_id: str | None = None,
    ) -> tuple[list[FindingContract], SharedState, bool]:
        """Audit ad wording, claims, and offer alignment against client compliance rules."""
        wording_res = check_wording(
            ad,
            retrieved_rules=retrieved_rules,
            model_client=model_client,
            embed_fn=embed_fn,
            file_store=file_store,
            shared_state=shared_state,
            connection=connection,
            tracer=tracer,
            trace_id=trace_id,
        )
        return wording_res.findings, wording_res.shared_state, wording_res.needs_person

    def refine_copy(self, current_text: str, feedback: str | None = None) -> str:
        """Autonomously adjust non-compliant claims in ad body copy based on feedback."""
        if not current_text:
            return current_text

        refined = current_text

        # Soften aggressive medical or unapproved curative claims
        replacements = [
            (r"\bcures?\s+arthritis(?:\s+in\s+\d+\s+days)?\b", "supports joint comfort with daily use"),
            (r"\bcures?\s+all\s+pain\b", "provides soothing relief"),
            (r"\bcures?\b", "soothes"),
            (r"\b100%\s+guaranteed\s+cure\b", "formulated to support wellness"),
            (r"\bmiracle\s+treatment\b", "proven botanical formula"),
        ]
        for pattern, replacement in replacements:
            refined = re.sub(pattern, replacement, refined, flags=re.IGNORECASE)

        logger.info("[OfferSpecialist] Refined copy: '%s' -> '%s'", current_text[:60], refined[:60])
        return refined


class SupervisorReviewerNode:
    """The Chief Supervisor Node: Reconciles specialist findings, manages the feedback loop, and enforces bounds."""

    name = "supervisor_reviewer"
    MAX_REFINEMENT_LOOPS = 3

    def review(
        self,
        ad: AdContract,
        findings: list[FindingContract],
        shared_state: SharedState,
        loop_count: int,
        max_loops: int = 3,
        platform_limits: Mapping[str, Any] | None = None,
    ) -> SupervisorDecision:
        """Reconcile findings, evaluate autonomous fixability, and route to specialists or approval."""
        bounded_max = min(max_loops, self.MAX_REFINEMENT_LOOPS)

        # Check if max loops have been exhausted
        if loop_count >= bounded_max:
            logger.info("[SUPERVISOR] Max refinement loops reached (%d/%d) for Ad %s. Escalating to Judge.", loop_count, bounded_max, ad.ad_id)
            return SupervisorDecision(
                action="APPROVE_OR_ESCALATE",
                target_specialist=None,
                feedback=None,
                loop_count=loop_count,
                is_fixable=False,
                audit_entry={
                    "iteration": loop_count,
                    "action": "ESCALATE_MAX_LOOPS_REACHED",
                    "remaining_findings": len(findings),
                },
            )

        # 1. Check for fixable headline issues
        headline = (ad.headline or "").strip()
        headline_findings = [f for f in findings if f.field == "headline"]
        for hf in headline_findings:
            is_length = "exceeds" in hf.message.lower() or "limit" in hf.message.lower()
            is_cure = "prohibited" in hf.message.lower() or "cure" in hf.message.lower()
            if is_length or is_cure:
                instruction = (
                    f"Shorten headline to under platform limits. Original: '{headline}'"
                    if is_length
                    else f"Replace prohibited claim '{hf.message}' with compliant supporting claim."
                )
                logger.info("[SUPERVISOR] Fixable headline finding detected: %s. Routing to HeadlineSpecialist (Loop %d).", hf.message, loop_count + 1)
                return SupervisorDecision(
                    action="REFINE",
                    target_specialist="headline_specialist",
                    feedback=instruction,
                    loop_count=loop_count + 1,
                    is_fixable=True,
                    audit_entry={
                        "iteration": loop_count + 1,
                        "specialist": "headline_specialist",
                        "finding_id": hf.finding_id,
                        "instruction": instruction,
                        "original_value": headline,
                    },
                )

        # 2. Check for fixable body copy issues
        copy_findings = [
            f for f in findings
            if f.field in ("ad_text", "body") or f.check_type == CheckType.WORDING
        ]
        for cf in copy_findings:
            if "cure" in cf.message.lower() or "prohibited" in cf.message.lower() or "claim" in cf.message.lower():
                instruction = f"Adjust medical claim in ad text: '{cf.message}'"
                logger.info("[SUPERVISOR] Fixable copy finding detected: %s. Routing to OfferSpecialist (Loop %d).", cf.message, loop_count + 1)
                return SupervisorDecision(
                    action="REFINE",
                    target_specialist="offer_specialist",
                    feedback=instruction,
                    loop_count=loop_count + 1,
                    is_fixable=True,
                    audit_entry={
                        "iteration": loop_count + 1,
                        "specialist": "offer_specialist",
                        "finding_id": cf.finding_id,
                        "instruction": instruction,
                        "original_value": ad.ad_text,
                    },
                )

        # If no fixable issues found, approve or escalate to Judge
        logger.info("[SUPERVISOR] All preliminary specialist checks completed with no further autonomous refinements needed. Proceeding to Judge.")
        return SupervisorDecision(
            action="APPROVE_OR_ESCALATE",
            target_specialist=None,
            feedback=None,
            loop_count=loop_count,
            is_fixable=False,
            audit_entry={
                "iteration": loop_count,
                "action": "PROCEED_TO_JUDGE",
                "findings_count": len(findings),
            },
        )


class JudgeAuditorNode:
    """Specialist Node: Independent LLM Judge auditing findings, dismissing false alarms, and certifying safety."""

    name = "judge_auditor"

    def evaluate(
        self,
        ad: AdContract,
        findings: list[FindingContract],
        retrieved_rules: Sequence[Any] | None = None,
        connection: Any = None,
        model_client: Any = None,
        tracer: Any = None,
        trace_id: str | None = None,
    ) -> JudgeEvaluationResult:
        """Run independent LLM-as-a-Judge audit against remaining findings."""
        return evaluate_with_llm_judge(
            ad,
            findings,
            retrieved_rules=retrieved_rules,
            connection=connection,
            model_client=model_client,
            tracer=tracer,
            trace_id=trace_id,
        )


# ===========================================================================
# Live Tracking Specialist Multi-Agent Nodes (PRD Live Ops & Guardrails)
# ===========================================================================

@dataclass
class RevenueAnalysisResult:
    """Performance analysis, ROAS categorization, and budget recommendations from RevenueTrackingSpecialistNode."""
    ad_id: str
    platform: str
    spend: float
    revenue: float
    roas: float | None
    orders: int
    impressions: int
    ctr: float
    profitability_status: str  # "UNPROFITABLE_BLEEDING", "SCALING_CANDIDATE", "MODERATE_PERFORMING", "LEARNING_PHASE"
    recommendation: str
    ai_insights: str | None = None


@dataclass
class IssueAuditResult:
    """Live platform audit, blocker diagnosis, and remediation alert emitted by IssueTrackingSpecialistNode."""
    ad_id: str
    client_id: str
    platform: str
    dispatch_status: str
    is_blocked: bool
    severity: str  # "CRITICAL_BLOCKER", "WARNING", "CLEAN"
    root_cause: str
    proposed_fix: str
    alert_dispatched: bool = False
    notification_result: dict[str, Any] | None = None


class RevenueTrackingSpecialistNode:
    """Specialist Node: Dedicated to real-time revenue analytics, ROAS tracking, and LLM budget scaling guidance."""

    name = "revenue_tracking_specialist"

    def analyze_performance(
        self,
        ad_id: str,
        platform: str,
        metrics_data: dict[str, Any],
        model_client: Any = None,
        use_llm: bool = False,
    ) -> RevenueAnalysisResult:
        """Evaluate real-time ad performance metrics and generate actionable budget guidance."""
        spend = float(metrics_data.get("spend_usd") if metrics_data.get("spend_usd") is not None else metrics_data.get("spend") or 0.0)
        revenue = float(metrics_data.get("inbound_sales_usd") if metrics_data.get("inbound_sales_usd") is not None else metrics_data.get("revenue") or 0.0)
        orders = int(metrics_data.get("inbound_orders") if metrics_data.get("inbound_orders") is not None else metrics_data.get("orders") or 0)
        impressions = int(metrics_data.get("impressions") or 0)
        clicks = int(metrics_data.get("clicks") or 0)

        raw_ctr = metrics_data.get("ctr_percent") if metrics_data.get("ctr_percent") is not None else metrics_data.get("ctr")
        if raw_ctr is not None:
            ctr = float(raw_ctr)
        else:
            ctr = round((clicks / impressions) * 100, 2) if impressions > 0 else 0.0

        raw_roas = metrics_data.get("roas")
        if raw_roas is not None:
            roas = float(raw_roas)
        else:
            roas = round(revenue / spend, 2) if spend > 0 else (0.0 if revenue == 0.0 else None)

        # 1. Deterministic Profitability Threshold Calculation
        if spend <= 0.0 or impressions < 50:
            profitability_status = "LEARNING_PHASE"
            fallback_rec = f"Campaign on {platform.title()} is in the initial learning phase with ${spend:.2f} spend. Allow impressions to accumulate before adjusting budgets."
        elif roas is not None and roas < 1.2:
            profitability_status = "UNPROFITABLE_BLEEDING"
            fallback_rec = f"ROAS of {roas}x on {platform.title()} is below the 1.2x breakeven threshold. Reduce ad budget by 40% or pause low-converting audiences."
        elif roas is not None and roas >= 2.5:
            profitability_status = "SCALING_CANDIDATE"
            fallback_rec = f"ROAS of {roas}x on {platform.title()} demonstrates high profitability. Scale campaign budget by 20-30% while monitoring CTR stability."
        else:
            profitability_status = "MODERATE_PERFORMING"
            fallback_rec = f"ROAS of {roas if roas is not None else 0.0}x on {platform.title()} is stable. Maintain current ad budget allocation and test incremental creative variations."

        # 2. Compact Context LLM Guidance (Fail-Closed)
        recommendation = fallback_rec
        ai_insights = None

        has_llm = (model_client is not None) or use_llm
        if use_llm and model_client is None:
            try:
                from apps.worker.model_client import is_gemini_configured
                has_llm = is_gemini_configured()
            except Exception:
                has_llm = False

        if has_llm:
            prompt = (
                f"You are a Senior Performance Marketing AI Specialist for Launch Gate.\n"
                f"Evaluate the following real-time campaign performance metrics for Ad '{ad_id}' on {platform.title()}:\n"
                f"- Spend: ${spend:.2f}\n"
                f"- Inbound Revenue: ${revenue:.2f}\n"
                f"- ROAS: {roas if roas is not None else 'N/A'}\n"
                f"- Orders: {orders}\n"
                f"- Impressions: {impressions}\n"
                f"- CTR: {ctr}%\n"
                f"- Calculated Status: {profitability_status}\n\n"
                f"Provide exactly 2 concise, actionable sentences recommending whether to increase, decrease, or maintain ad spend, and why. Avoid any hallucination."
            )
            try:
                ai_text = ""
                if hasattr(model_client, "generate") and callable(getattr(model_client, "generate", None)):
                    res_val = model_client.generate(prompt)
                    ai_text = str(res_val).strip() if res_val is not None else ""
                elif hasattr(model_client, "models") and hasattr(model_client.models, "generate_content"):
                    resp = model_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
                    ai_text = str(resp.text).strip() if hasattr(resp, "text") and resp.text else ""
                else:
                    from apps.worker.model_client import call_gemini_model
                    resp = call_gemini_model(
                        prompt,
                        client_id=platform,
                        ad_id=ad_id,
                        step_name="revenue_tracking_analysis",
                        client=model_client,
                    )
                    ai_text = str(resp.text).strip() if hasattr(resp, "text") and resp.text else ""

                if ai_text and isinstance(ai_text, str):
                    ai_insights = ai_text
                    recommendation = ai_text
            except Exception as exc:
                logger.warning(
                    "[REVENUE SPECIALIST] LLM performance analysis failed (%s); failing closed to deterministic recommendation: %s",
                    ad_id,
                    exc,
                )

        return RevenueAnalysisResult(
            ad_id=ad_id,
            platform=platform,
            spend=spend,
            revenue=revenue,
            roas=roas,
            orders=orders,
            impressions=impressions,
            ctr=ctr,
            profitability_status=profitability_status,
            recommendation=recommendation,
            ai_insights=ai_insights,
        )


class IssueTrackingSpecialistNode:
    """Specialist Node: Monitors live crawler feedback, detects platform rejections/suspensions, and dispatches immediate launcher alerts."""

    name = "issue_tracking_specialist"

    BLOCKER_STATUSES = {
        "blocked",
        "blocked_connector_not_configured",
        "rejected",
        "suspended",
        "flagged_by_platform",
        "failed_dispatch",
        "dead_letter",
    }

    def audit_live_status(
        self,
        ad_id: str,
        client_id: str,
        platform: str,
        dispatch_status: str,
        crawler_feedback: str | None = None,
        contact_email: str | None = None,
        contact_phone: str | None = None,
        model_client: Any = None,
        trigger_alert: bool = True,
        use_llm: bool = False,
    ) -> IssueAuditResult:
        """Audit live ad status and crawler feedback. Dispatches immediate alerts if blocked or suspended."""
        status_lower = (dispatch_status or "").strip().lower()
        feedback_text = (crawler_feedback or "").strip()
        feedback_lower = feedback_text.lower()

        is_blocked = (
            status_lower in self.BLOCKER_STATUSES
            or any(k in status_lower for k in ("blocked", "reject", "suspend", "flagged", "failed", "dead_letter"))
            or any(k in feedback_lower for k in ("suspend", "reject", "prohibit", "violat", "disallow", "blocked"))
        )

        if not is_blocked:
            return IssueAuditResult(
                ad_id=ad_id,
                client_id=client_id,
                platform=platform,
                dispatch_status=dispatch_status,
                is_blocked=False,
                severity="CLEAN",
                root_cause="No platform policy issues or review blocks detected.",
                proposed_fix="Ad is compliant and actively serving live impressions.",
                alert_dispatched=False,
                notification_result=None,
            )

        # Critical blocker detected
        severity = "CRITICAL_BLOCKER"
        is_suspension = "suspend" in status_lower or "suspend" in feedback_lower
        alert_type = "campaign_suspension" if is_suspension else "platform_rejection"

        root_cause = feedback_text or f"Platform dispatch status '{dispatch_status}' indicates an active policy block or review rejection."
        proposed_fix = "Revise copy to eliminate non-compliant or medical claims, verify landing page URL parameters, and submit for human reviewer approval."

        # Optional LLM root-cause diagnostic
        has_llm = (model_client is not None) or use_llm
        if use_llm and model_client is None:
            try:
                from apps.worker.model_client import is_gemini_configured
                has_llm = is_gemini_configured()
            except Exception:
                has_llm = False

        if has_llm:
            prompt = (
                f"You are a Platform Policy Compliance Specialist for Launch Gate.\n"
                f"Ad '{ad_id}' for client '{client_id}' on {platform.title()} was blocked/suspended with status '{dispatch_status}'.\n"
                f"Platform Feedback: '{root_cause}'\n\n"
                f"Provide exactly one clear sentence identifying the root cause, and one compliant proposed copy fix."
            )
            try:
                ai_text = ""
                if hasattr(model_client, "generate") and callable(getattr(model_client, "generate", None)):
                    res_val = model_client.generate(prompt)
                    ai_text = str(res_val).strip() if res_val is not None else ""
                elif hasattr(model_client, "models") and hasattr(model_client.models, "generate_content"):
                    resp = model_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
                    ai_text = str(resp.text).strip() if hasattr(resp, "text") and resp.text else ""
                else:
                    from apps.worker.model_client import call_gemini_model
                    resp = call_gemini_model(
                        prompt,
                        client_id=client_id,
                        ad_id=ad_id,
                        step_name="issue_tracking_diagnostic",
                        client=model_client,
                    )
                    ai_text = str(resp.text).strip() if hasattr(resp, "text") and resp.text else ""

                if ai_text and isinstance(ai_text, str):
                    proposed_fix = ai_text
            except Exception as exc:
                logger.warning("[ISSUE SPECIALIST] LLM diagnostic failed (%s); using deterministic fix: %s", ad_id, exc)

        # Trigger an immediate SMTP email alert to the campaign launcher.
        alert_dispatched = False
        notification_result = None

        if trigger_alert and (contact_email or contact_phone):
            try:
                from apps.api.notifications import NotificationDispatcher
                subject = f"CRITICAL ALERT: Ad {ad_id} {dispatch_status.replace('_', ' ').title()} on {platform.title()}"
                message_body = (
                    f"Launch Gate Issue Tracking Specialist Node detected a critical blocker:\n\n"
                    f"Client: {client_id}\n"
                    f"Ad ID: {ad_id}\n"
                    f"Platform: {platform.title()}\n"
                    f"Status: {dispatch_status}\n"
                    f"Root Cause: {root_cause}\n"
                    f"Proposed Remediation: {proposed_fix}\n\n"
                    f"Please log in to the Launch Gate console to remediate and resume live serving."
                )
                notification_result = NotificationDispatcher.dispatch_campaign_alert(
                    client_id=client_id,
                    recipient_email=contact_email,
                    recipient_phone=contact_phone,
                    alert_type=alert_type,
                    subject=subject,
                    message_body=message_body,
                    ad_id=ad_id,
                    platform=platform,
                )
                alert_dispatched = True
            except Exception as exc:
                logger.exception("[ISSUE SPECIALIST] Failed dispatching alert for blocked ad %s: %s", ad_id, exc)

        return IssueAuditResult(
            ad_id=ad_id,
            client_id=client_id,
            platform=platform,
            dispatch_status=dispatch_status,
            is_blocked=True,
            severity=severity,
            root_cause=root_cause,
            proposed_fix=proposed_fix,
            alert_dispatched=alert_dispatched,
            notification_result=notification_result,
        )
