from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, conint, constr, root_validator


NonEmptyString = constr(strip_whitespace=True, min_length=1)
PositiveRowNumber = conint(gt=0)


class ContractModel(BaseModel):
    class Config:
        extra = "forbid"


class CheckType(str, Enum):
    MECHANICAL = "mechanical"
    PAGE = "page"
    WORDING = "wording"


class VerdictStatus(str, Enum):
    READY = "ready"
    FIX_NEEDED = "fix needed"
    BLOCKED = "blocked"
    NEEDS_PERSON = "needs a person"


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

    @root_validator(skip_on_failure=True)
    def require_ad_text_or_headline(cls, values):
        if not values.get("ad_text") and not values.get("headline"):
            raise ValueError("ad_text or headline must be provided")
        return values


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

    @root_validator(skip_on_failure=True)
    def validate_batch_references(cls, values):
        batch_id = values.get("batch_id")
        ads = values.get("ads", [])
        findings = values.get("findings", [])
        verdicts = values.get("verdicts", [])

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

        return values
