"""Enterprise Multi-Agentic Supervisor Workflow for Ad Campaign Quality Intelligence.

Coordinates a team of specialized agents:
1. Supervisor Agent (Orchestration & Task Delegation)
2. Mechanical Inspector Agent (Technical Specs, UTMs, Ratios)
3. Landing Page Detective Agent (Live URL Verification & Disclaimer Audit)
4. Brand Safety & Compliance Agent (Vector Retrieval & Legal Claims Check)
5. Judge & Auditor Agent (Independent LLM-as-a-Judge Certification)

All agents share and update a synchronized `SharedState` and return structured findings.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
import logging
from typing import Any

from apps.worker.file_storage import UnifiedFileStore, get_file_store
from apps.worker.job_runner import _execute_page_checks, load_creative_manifest, load_platform_limits
from apps.worker.judge_eval import JudgeEvaluationResult, evaluate_with_llm_judge
from apps.worker.shared_state import SharedState
from apps.worker.verdict import aggregate_ad_verdict
from apps.worker.wording_check import check_wording
from packages.contracts import AdContract, FindingContract, VerdictContract, VerdictStatus
from packages.rules.creative_size_ratio import check_creative_size_ratio
from packages.rules.platform_text_limits import check_platform_text_limits
from packages.rules.stale_platform_limits import check_stale_platform_limits
from packages.rules.utm_naming import check_utm_naming
from packages.rules.utm_presence import check_utm_presence

logger = logging.getLogger(__name__)


@dataclass
class AgentTeamReport:
    supervisor_decision: str
    verdict: VerdictContract
    mechanical_findings: list[FindingContract] = field(default_factory=list)
    page_findings: list[FindingContract] = field(default_factory=list)
    compliance_findings: list[FindingContract] = field(default_factory=list)
    judge_result: JudgeEvaluationResult | None = None
    all_findings: list[FindingContract] = field(default_factory=list)
    shared_state: SharedState | None = None


class MechanicalInspectorAgent:
    """Specialized Agent: Validates technical specs, character limits, aspect ratios, and UTM parameters."""

    name = "MechanicalInspectorAgent"

    def execute(self, ad: AdContract, manifest: Any = None, limits_config: Any = None) -> list[FindingContract]:
        manifest = manifest or load_creative_manifest()
        limits_config = limits_config or load_platform_limits()
        platforms_map = limits_config.get("platforms", {}) if isinstance(limits_config, dict) else {}

        findings: list[FindingContract] = []
        findings.extend(check_utm_presence(ad))
        findings.extend(check_utm_naming(ad))
        findings.extend(check_platform_text_limits(ad, platforms_map))
        findings.extend(check_stale_platform_limits(ad, limits_config))
        findings.extend(check_creative_size_ratio(ad, manifest, platforms_map))
        return findings


class LandingPageDetectiveAgent:
    """Specialized Agent: Crawls destination URL, audits landing page offers, checks redirects, and captures evidence."""

    name = "LandingPageDetectiveAgent"

    async def execute(
        self,
        ad: AdContract,
        allowed_origins: set[str] | None = None,
        timeout_ms: int = 5000,
        file_store: Any = None,
        as_of: date | None = None,
    ) -> tuple[list[FindingContract], SharedState]:
        return await _execute_page_checks(
            ad,
            allowed_origins=allowed_origins,
            timeout_ms=timeout_ms,
            file_store=file_store,
            as_of=as_of,
        )


class BrandSafetyAgent:
    """Specialized Agent: Uses vector retrieval to cross-examine ad copy against client claims register and brand guidelines."""

    name = "BrandSafetyAgent"

    def execute(
        self,
        ad: AdContract,
        shared_state: SharedState,
        connection: Any = None,
        file_store: Any = None,
        retrieved_rules: Any = None,
    ) -> tuple[list[FindingContract], SharedState, bool]:
        wording_result = check_wording(
            ad,
            retrieved_rules=retrieved_rules,
            file_store=file_store,
            shared_state=shared_state,
            connection=connection,
        )
        return wording_result.findings, wording_result.shared_state, wording_result.needs_person


class JudgeAuditorAgent:
    """Specialized Agent: Independent LLM Judge auditing findings, dismissing false alarms, and certifying safety."""

    name = "JudgeAuditorAgent"

    def execute(
        self,
        ad: AdContract,
        findings: list[FindingContract],
        retrieved_rules: list[Any] | None = None,
    ) -> JudgeEvaluationResult:
        return evaluate_with_llm_judge(ad, findings, retrieved_rules=retrieved_rules)


class CampaignSupervisorAgent:
    """Chief Supervisor Agent: Directs workflow execution, delegates tasks to specialized team agents, and reconciles shared state."""

    def __init__(self, file_store: Any = None) -> None:
        self.file_store = file_store or get_file_store()
        self.mechanical_agent = MechanicalInspectorAgent()
        self.page_agent = LandingPageDetectiveAgent()
        self.brand_agent = BrandSafetyAgent()
        self.judge_agent = JudgeAuditorAgent()

    def run_campaign_audit(
        self,
        ad: AdContract,
        connection: Any = None,
        allowed_origins: set[str] | None = None,
    ) -> AgentTeamReport:
        """Coordinate multi-agentic audit pipeline for an individual ad."""
        shared_state = SharedState(ad_id=ad.ad_id, client_id=ad.client_id, status="supervisor_assigned")
        logger.info("[SUPERVISOR] Delegating task for Ad: %s (Client: %s)", ad.ad_id, ad.client_id)

        # 1. Dispatch Mechanical Inspector Agent
        logger.info("[SUPERVISOR -> %s] Checking technical specifications...", self.mechanical_agent.name)
        mech_findings = self.mechanical_agent.execute(ad)
        shared_state.status = "mechanical_inspected"

        # 2. Dispatch Landing Page Detective Agent
        page_findings: list[FindingContract] = []
        if ad.destination_url:
            logger.info("[SUPERVISOR -> %s] Auditing live destination URL: %s", self.page_agent.name, ad.destination_url)
            try:
                page_findings, shared_state = asyncio.run(
                    self.page_agent.execute(
                        ad,
                        allowed_origins=allowed_origins,
                        file_store=self.file_store,
                    )
                )
            except Exception as exc:
                logger.warning("Landing page detective check encountered non-fatal error: %s", exc)

        # 3. Dispatch Brand Safety & Compliance Agent
        logger.info("[SUPERVISOR -> %s] Performing semantic brand & claim compliance check...", self.brand_agent.name)
        comp_findings, shared_state, needs_person = self.brand_agent.execute(
            ad,
            shared_state=shared_state,
            connection=connection,
            file_store=self.file_store,
        )

        all_prelim_findings = mech_findings + page_findings + comp_findings

        # 4. Dispatch Independent Judge & Auditor Agent
        logger.info("[SUPERVISOR -> %s] Auditing preliminary findings with LLM-as-a-Judge...", self.judge_agent.name)
        judge_res = self.judge_agent.execute(ad, all_prelim_findings)
        if judge_res.needs_person:
            needs_person = True

        # Dismiss any false alarms identified by Judge
        active_findings = [
            f for f in all_prelim_findings
            if f.finding_id not in judge_res.false_alarms_dismissed
        ]

        # 5. Supervisor Final Resolution
        verdict = aggregate_ad_verdict(
            ad,
            active_findings,
            needs_person=needs_person,
            step_count=4,
            max_steps=5,
        )
        shared_state.status = verdict.status.value
        shared_state.summary = f"Multi-Agent Team Audit Complete. {verdict.summary} (Judge: {judge_res.verdict})"

        decision = "READY_FOR_DISPATCH" if verdict.status == VerdictStatus.READY else "REMEDIATION_REQUIRED"

        return AgentTeamReport(
            supervisor_decision=decision,
            verdict=verdict,
            mechanical_findings=mech_findings,
            page_findings=page_findings,
            compliance_findings=comp_findings,
            judge_result=judge_res,
            all_findings=active_findings,
            shared_state=shared_state,
        )
