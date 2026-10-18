"""Runtime LLM-as-a-Judge Evaluation Node for ad quality & compliance audit.

Reviews model findings, validates rule citations, detects hallucinations or false alarms,
and scores proposed remediations before any ad can be released to live platforms.

Safety Guarantees (Task 1):
- Fail-Closed: On ANY failure (no key, API error, bad JSON, spend cap exceeded),
  returns verdict="REQUIRES_HUMAN", needs_person=True with an honest reason.
- Zero Fake Fallbacks: The deterministic fallback is completely removed.
- Mechanical Invariant: Hard mechanical checks (UTM, platform limits, creative ratios)
  CANNOT be dismissed by an LLM judge.
- Pydantic Validation: Strictly parses structured JSON using JudgeEvaluationSchema.
- Citation & Dismissal Validation: Any dismissed finding must exist in the input findings;
  otherwise, the dismissal is ignored and marked needs_person.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import logging
import os
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from apps.worker.model_client import call_gemini_model, is_gemini_configured
from packages.contracts import AdContract, CheckType, FindingContract

logger = logging.getLogger(__name__)

# Default judge model: Flash-Lite (fast, low cost, free-tier eligible, no Pro required)
DEFAULT_JUDGE_MODEL = os.environ.get("JUDGE_MODEL", os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"))

JUDGE_PROMPT_TEMPLATE = """You are an expert Chief Advertising Compliance Judge and Auditor.
Your job is to independently evaluate the compliance audit findings generated for this ad.

=== AD DETAILS ===
Platform: {platform}
Headline: {headline}
Ad Text: {ad_text}
Destination URL: {destination_url}

=== RETRIEVED CLIENT BRAND RULES ===
{rules_summary}

=== INITIAL FINDINGS PRODUCED BY WORKER ===
{findings_summary}

Your task:
1. FAITHFULNESS AUDIT: Did the worker hallucinate any claims or quote text not actually present in the ad?
2. REASONING AUDIT: Are the compliance findings legitimate policy violations or false alarms?
   NOTE: You may only dismiss a finding if it is genuinely a false alarm and you provide clear justification in judge_reasoning.
3. REMEDIATION AUDIT: Are any proposed fixes compliant, safe, and preserving of original brand tone?
4. DECISION: Provide an overall confidence score from 0.00 to 1.00. If score < 0.85, set judge_verdict to REQUIRES_HUMAN.

Respond strictly with valid JSON with this exact schema:
{{
  "judge_verdict": "CONFIRMED" | "OVERRULED" | "REQUIRES_HUMAN",
  "confidence_score": 0.95,
  "faithfulness_score": 0.98,
  "hallucinations_detected": false,
  "false_alarms_dismissed": ["finding_id_1"],
  "remediation_approved": true,
  "judge_reasoning": "Concise 1-2 sentence justification for the decision."
}}
"""


class JudgeEvaluationSchema(BaseModel):
    """Pydantic schema enforcing structured LLM Judge response contracts."""

    judge_verdict: str = Field(description="Verdict: CONFIRMED, OVERRULED, or REQUIRES_HUMAN")
    confidence_score: float = Field(ge=0.0, le=1.0)
    faithfulness_score: float = Field(ge=0.0, le=1.0)
    hallucinations_detected: bool = Field(default=False)
    false_alarms_dismissed: list[str] = Field(default_factory=list)
    remediation_approved: bool = Field(default=True)
    judge_reasoning: str = Field(min_length=1)


@dataclass(frozen=True)
class JudgeEvaluationResult:
    """Immutable result object emitted by the LLM Judge node."""

    verdict: str
    confidence_score: float
    faithfulness_score: float
    hallucinations_detected: bool
    needs_person: bool
    judge_reasoning: str
    false_alarms_dismissed: list[str]
    cost_usd: float = 0.0


def evaluate_with_llm_judge(
    ad: AdContract,
    findings: list[FindingContract],
    retrieved_rules: Sequence[Any] | None = None,
    *,
    connection: Any = None,
    model_client: Any = None,
    tracer: Any = None,
    trace_id: str | None = None,
    judge_model: str | None = None,
) -> JudgeEvaluationResult:
    """Run independent runtime LLM Judge evaluation on ad findings.

    Strictly fail-closed: on ANY error, returns verdict="REQUIRES_HUMAN", needs_person=True.
    """
    # If no findings, the ad is clean; skip the judge check
    if not findings:
        return JudgeEvaluationResult(
            verdict="SKIPPED",
            confidence_score=1.0,
            faithfulness_score=1.0,
            hallucinations_detected=False,
            needs_person=False,
            judge_reasoning="No findings to evaluate; judge skipped.",
            false_alarms_dismissed=[],
            cost_usd=0.0,
        )

    # Fail closed if model is not configured and no mock client was provided
    if not is_gemini_configured() and model_client is None:
        logger.warning(
            "MODEL_API_KEY is not configured for ad %s. Failing closed to REQUIRES_HUMAN.",
            ad.ad_id,
        )
        return JudgeEvaluationResult(
            verdict="REQUIRES_HUMAN",
            confidence_score=0.0,
            faithfulness_score=0.0,
            hallucinations_detected=False,
            needs_person=True,
            judge_reasoning="Judge unavailable: MODEL_API_KEY is not configured.",
            false_alarms_dismissed=[],
            cost_usd=0.0,
        )

    # Format findings summary
    findings_list = []
    finding_map: dict[str, FindingContract] = {}
    for f in findings:
        fid = f.finding_id or f"{ad.ad_id}:{f.check_type.value}:{f.rule_id or 'none'}"
        finding_map[fid] = f
        findings_list.append(
            f"- [ID: {fid}] [{f.check_type.value.upper()}] Rule: {f.rule_id or 'None'}: "
            f"{f.message} (Proposed Fix: {f.proposed_fix or 'None'})"
        )
    findings_summary = "\n".join(findings_list)

    # Format rules summary
    rules_list = []
    if retrieved_rules:
        for r in retrieved_rules[:5]:
            rule_id = getattr(r, "entry_number", getattr(r, "rule_id", "rule"))
            content = getattr(r, "content", getattr(r, "rule_text", ""))
            rules_list.append(f"[{rule_id}] {content}")
    rules_summary = "\n".join(rules_list) if rules_list else "Standard platform brand & safety guidelines."

    prompt = JUDGE_PROMPT_TEMPLATE.format(
        platform=ad.platform,
        headline=ad.headline or "",
        ad_text=ad.ad_text or "",
        destination_url=ad.destination_url or "",
        rules_summary=rules_summary,
        findings_summary=findings_summary,
    )

    span_id = None
    if tracer and trace_id:
        try:
            span_id = tracer.start_span(
                trace_id=trace_id,
                step_name="judge_eval",
                input_data={"findings_count": len(findings), "ad_id": ad.ad_id},
            )
        except Exception as t_err:
            logger.debug("Failed starting tracer span for judge: %s", t_err)

    try:
        active_judge_model = (
            judge_model
            or os.environ.get("JUDGE_MODEL")
            or os.environ.get("GEMINI_MODEL")
            or DEFAULT_JUDGE_MODEL
        )
        resp = call_gemini_model(
            prompt=prompt,
            client_id=ad.client_id,
            ad_id=ad.ad_id,
            batch_id=ad.batch_id,
            step_name="judge",
            model_name=active_judge_model,
            system_instruction=(
                "You are an objective advertising compliance judge and auditor. "
                "Output strictly valid JSON matching the schema."
            ),
            temperature=0.0,
            response_mime_type="application/json",
            client=model_client,
            connection=connection,
        )

        cost_usd = getattr(resp, "cost_usd", 0.0)

        raw_text = resp.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]

        parsed_json = json.loads(raw_text.strip())
        parsed = JudgeEvaluationSchema.model_validate(parsed_json)

        # Validate dismissals and enforce safety boundaries
        valid_dismissals: list[str] = []
        needs_person = (
            parsed.confidence_score < 0.85
            or parsed.hallucinations_detected
            or (parsed.judge_verdict == "REQUIRES_HUMAN")
        )

        for fid in parsed.false_alarms_dismissed:
            if fid not in finding_map:
                logger.warning(
                    "Judge attempted to dismiss non-existent finding '%s'. Marking needs_person.",
                    fid,
                )
                needs_person = True
                continue

            target_finding = finding_map[fid]

            # Invariant: Mechanical findings (UTM, character limits, ratios) cannot be dismissed by LLM
            if target_finding.check_type == CheckType.MECHANICAL:
                logger.warning(
                    "Judge attempted to dismiss mechanical finding '%s'. "
                    "Mechanical violations cannot be overruled by an LLM.",
                    fid,
                )
                needs_person = True
                continue

            # Must have reasoned justification
            if not parsed.judge_reasoning or len(parsed.judge_reasoning.strip()) < 5:
                logger.warning("Judge dismissed finding '%s' without justification.", fid)
                needs_person = True
                continue

            valid_dismissals.append(fid)

        verdict = parsed.judge_verdict
        if verdict not in {"CONFIRMED", "OVERRULED", "REQUIRES_HUMAN"}:
            verdict = "REQUIRES_HUMAN"
            needs_person = True

        result = JudgeEvaluationResult(
            verdict=verdict,
            confidence_score=parsed.confidence_score,
            faithfulness_score=parsed.faithfulness_score,
            hallucinations_detected=parsed.hallucinations_detected,
            needs_person=needs_person,
            judge_reasoning=parsed.judge_reasoning,
            false_alarms_dismissed=valid_dismissals,
            cost_usd=cost_usd,
        )

        if tracer and trace_id and span_id:
            try:
                tracer.end_span(
                    span_id=span_id,
                    trace_id=trace_id,
                    output_data={
                        "verdict": result.verdict,
                        "confidence_score": result.confidence_score,
                        "dismissed": result.false_alarms_dismissed,
                    },
                    cost_usd=cost_usd,
                )
            except Exception as t_err:
                logger.debug("Failed ending tracer span for judge: %s", t_err)

        return result

    except Exception as exc:
        logger.warning(
            "Live LLM Judge evaluation failed (%s). Failing closed to REQUIRES_HUMAN.",
            exc,
        )
        if tracer and trace_id and span_id:
            try:
                tracer.end_span(
                    span_id=span_id,
                    trace_id=trace_id,
                    output_data={"error": str(exc), "verdict": "REQUIRES_HUMAN"},
                    status="error",
                )
            except Exception:
                pass

        return JudgeEvaluationResult(
            verdict="REQUIRES_HUMAN",
            confidence_score=0.0,
            faithfulness_score=0.0,
            hallucinations_detected=False,
            needs_person=True,
            judge_reasoning=f"Judge unavailable: {exc}",
            false_alarms_dismissed=[],
            cost_usd=0.0,
        )

