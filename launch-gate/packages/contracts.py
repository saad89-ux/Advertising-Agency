from enum import Enum
import json
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, conint, constr, field_validator, model_validator


NonEmptyString = constr(strip_whitespace=True, min_length=1)
PositiveRowNumber = conint(gt=0)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckType(str, Enum):
    MECHANICAL = "mechanical"
    PAGE = "page"
    WORDING = "wording"


class VerdictStatus(str, Enum):
    READY = "ready"
    FIX_NEEDED = "fix needed"
    BLOCKED = "blocked"
    NEEDS_PERSON = "needs a person"
    MODEL_UNAVAILABLE = "model unavailable"


class BatchRowContract(ContractModel):
    """One flat-file ad row carrying stable campaign and ad-set grouping keys."""

    ad_id: NonEmptyString | None = None
    campaign_key: NonEmptyString
    ad_set_key: NonEmptyString
    ad_key: NonEmptyString
    campaign_name: NonEmptyString
    campaign_objective: NonEmptyString
    campaign_budget_amount: int | None = Field(default=None, gt=0, description="Budget in Meta account minor currency units")
    campaign_special_category: NonEmptyString = "NONE"
    campaign_budget_strategy: NonEmptyString = "DAILY"
    creative_mime_type: NonEmptyString | None = None
    ad_set_name: NonEmptyString
    targeting_spec: dict[str, Any]
    optimization_goal: NonEmptyString
    billing_event: NonEmptyString
    ad_set_budget_amount: int | None = Field(default=None, gt=0)
    ad_name: NonEmptyString
    creative_url: NonEmptyString
    body_copy: NonEmptyString
    headline: NonEmptyString
    page_id: NonEmptyString
    platform: NonEmptyString
    destination_url: NonEmptyString
    instagram_actor_id: NonEmptyString | None = None
    placements: list[str] = Field(default_factory=list)
    schedule: dict[str, Any] = Field(default_factory=dict)

    @field_validator("targeting_spec", "schedule", mode="before")
    @classmethod
    def parse_json_objects(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("must be a valid JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("must be a JSON object")
        return value

    @field_validator("placements", mode="before")
    @classmethod
    def parse_placements(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("must be a JSON array of placements") from exc
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("must be a JSON array of non-empty placement names")
        return [item.strip() for item in value]

    @field_validator("campaign_objective")
    @classmethod
    def validate_campaign_objective(cls, value: str) -> str:
        allowed = {
            "OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT",
            "OUTCOME_LEADS", "OUTCOME_SALES", "OUTCOME_APP_PROMOTION",
        }
        if value not in allowed:
            raise ValueError("must be a supported Meta ODAX objective")
        return value

    @field_validator("campaign_budget_strategy")
    @classmethod
    def validate_budget_strategy(cls, value: str) -> str:
        allowed = {"DAILY", "LIFETIME", "AD_SET_DAILY", "AD_SET_LIFETIME"}
        if value not in allowed:
            raise ValueError("must be DAILY, LIFETIME, AD_SET_DAILY, or AD_SET_LIFETIME")
        return value

    @model_validator(mode="after")
    def validate_budget_placement(self):
        campaign_budget = self.campaign_budget_amount
        ad_set_budget = self.ad_set_budget_amount
        if self.campaign_budget_strategy == "DAILY" and (campaign_budget is None or ad_set_budget is not None):
            raise ValueError("DAILY strategy requires campaign_budget_amount and forbids ad_set_budget_amount")
        if self.campaign_budget_strategy == "LIFETIME" and (campaign_budget is None or ad_set_budget is not None):
            raise ValueError("LIFETIME strategy requires campaign_budget_amount and forbids ad_set_budget_amount")
        if self.campaign_budget_strategy in {"AD_SET_DAILY", "AD_SET_LIFETIME"} and (campaign_budget is not None or ad_set_budget is None):
            raise ValueError("ad-set budget strategy requires ad_set_budget_amount and forbids campaign_budget_amount")
        return self


def validate_batch_row_consistency(rows: list[dict[str, Any] | BatchRowContract]) -> list[BatchRowContract]:
    """Validate immutable keys and settings shared by campaign/ad-set groups."""
    parsed_rows: list[BatchRowContract] = []
    campaigns: dict[str, BatchRowContract] = {}
    ad_sets: dict[str, BatchRowContract] = {}
    ad_keys: set[str] = set()
    campaign_fields = (
        "campaign_name", "campaign_objective", "campaign_budget_amount",
        "campaign_special_category", "campaign_budget_strategy",
    )
    ad_set_fields = (
        "ad_set_name", "targeting_spec", "optimization_goal", "billing_event",
        "ad_set_budget_amount", "placements", "schedule",
    )

    def settings_differ(existing: BatchRowContract, current: BatchRowContract, fields: tuple[str, ...]) -> bool:
        return any(getattr(existing, field) != getattr(current, field) for field in fields)

    for row_index, raw_row in enumerate(rows, start=1):
        try:
            row = raw_row if isinstance(raw_row, BatchRowContract) else BatchRowContract.model_validate(raw_row)
        except Exception as exc:
            raise ValueError(f"Invalid hierarchy data at row {row_index}: {exc}") from exc
        if row.ad_key in ad_keys:
            raise ValueError(f"Duplicate ad_key '{row.ad_key}' at row {row_index}.")
        ad_keys.add(row.ad_key)

        previous_campaign = campaigns.get(row.campaign_key)
        if previous_campaign and settings_differ(previous_campaign, row, campaign_fields):
            raise ValueError(
                f"Conflict at row {row_index}: campaign_key '{row.campaign_key}' has inconsistent campaign settings."
            )
        campaigns.setdefault(row.campaign_key, row)

        previous_ad_set = ad_sets.get(row.ad_set_key)
        if previous_ad_set:
            if previous_ad_set.campaign_key != row.campaign_key:
                raise ValueError(
                    f"Conflict at row {row_index}: ad_set_key '{row.ad_set_key}' cannot belong to two campaigns."
                )
            if settings_differ(previous_ad_set, row, ad_set_fields):
                raise ValueError(
                    f"Conflict at row {row_index}: ad_set_key '{row.ad_set_key}' has inconsistent ad-set settings."
                )
        ad_sets.setdefault(row.ad_set_key, row)
        parsed_rows.append(row)
    return parsed_rows


class AdContract(ContractModel):
    ad_id: NonEmptyString
    batch_id: NonEmptyString
    client_id: NonEmptyString
    row_number: PositiveRowNumber
    platform: NonEmptyString
    ad_text: Optional[NonEmptyString] = None
    headline: Optional[NonEmptyString] = None
    destination_url: NonEmptyString
    creative_file: Optional[NonEmptyString] = None
    row_hash: NonEmptyString
    sheet_hash: NonEmptyString

    @model_validator(mode="after")
    def require_ad_text_or_headline(self):
        if not self.ad_text and not self.headline:
            raise ValueError("ad_text or headline must be provided")
        return self


class FindingContract(ContractModel):
    finding_id: NonEmptyString
    ad_id: NonEmptyString
    row_number: PositiveRowNumber
    check_type: CheckType
    message: NonEmptyString
    field: Optional[NonEmptyString] = None
    proposed_fix: Optional[NonEmptyString] = None
    rule_id: Optional[NonEmptyString] = None
    file_ref: Optional[NonEmptyString] = None
    actual_value: Optional[str] = None
    expected_format: Optional[str] = None


class VerdictContract(ContractModel):
    ad_id: NonEmptyString
    status: VerdictStatus
    finding_ids: List[NonEmptyString] = Field(default_factory=list)
    summary: Optional[NonEmptyString] = None


class BatchContract(ContractModel):
    batch_id: NonEmptyString
    ads: List[AdContract] = Field(default_factory=list)
    findings: List[FindingContract] = Field(default_factory=list)
    verdicts: List[VerdictContract] = Field(default_factory=list)
    summary: Optional[NonEmptyString] = None

    @model_validator(mode="after")
    def validate_batch_references(self):
        batch_id = self.batch_id
        ads = self.ads
        findings = self.findings
        verdicts = self.verdicts

        ad_ids = [ad.ad_id for ad in ads]
        if len(ad_ids) != len(set(ad_ids)):
            raise ValueError("ad IDs must be unique within a batch")
        if any(ad.batch_id != batch_id for ad in ads):
            raise ValueError("every ad must belong to the batch")

        known_ad_ids = set(ad_ids)
        if any(finding.ad_id not in known_ad_ids for finding in findings):
            raise ValueError("every finding must reference an ad in the batch")
        if any(verdict.ad_id not in known_ad_ids for verdict in verdicts):
            raise ValueError("every verdict must reference an ad in the batch")

        finding_ids = [finding.finding_id for finding in findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding IDs must be unique within a batch")
        known_finding_ids = set(finding_ids)
        if any(
            finding_id not in known_finding_ids
            for verdict in verdicts
            for finding_id in verdict.finding_ids
        ):
            raise ValueError("verdict findings must reference findings in the batch")

        return self
