import pytest
from pydantic import ValidationError

from packages.contracts import (
    AdContract,
    BatchContract,
    CheckType,
    FindingContract,
    VerdictContract,
    VerdictStatus,
)


def valid_ad(**overrides):
    values = {
        "ad_id": "ad-1",
        "batch_id": "batch-1",
        "client_id": "client-1",
        "row_number": 2,
        "platform": "facebook",
        "ad_text": "A sample ad",
        "destination_url": "example.test/page",
        "row_hash": "row-hash",
        "sheet_hash": "sheet-hash",
    }
    values.update(overrides)
    return values


def test_ad_requires_identifiers_row_and_copy():
    ad = AdContract(**valid_ad(headline="A sample headline", ad_text=None))

    assert ad.row_number == 2
    assert ad.headline == "A sample headline"
    with pytest.raises(ValidationError):
        AdContract(**valid_ad(row_number=0))
    with pytest.raises(ValidationError):
        AdContract(**valid_ad(ad_text=None))


def test_contracts_reject_empty_and_unexpected_fields():
    with pytest.raises(ValidationError):
        AdContract(**valid_ad(client_id="  "))
    with pytest.raises(ValidationError):
        AdContract(**valid_ad(unlisted_field="unexpected"))


def test_finding_and_verdict_validate_check_and_status_values():
    finding = FindingContract(
        finding_id="finding-1",
        ad_id="ad-1",
        row_number=2,
        check_type=CheckType.MECHANICAL,
        message="UTM is missing",
        field="destination_url",
        proposed_fix="Add the required UTM parameter",
    )
    verdict = VerdictContract(
        ad_id="ad-1",
        status=VerdictStatus.FIX_NEEDED,
        finding_ids=[finding.finding_id],
    )

    assert finding.check_type is CheckType.MECHANICAL
    assert verdict.status is VerdictStatus.FIX_NEEDED
    with pytest.raises(ValidationError):
        FindingContract(
            finding_id="finding-2",
            ad_id="ad-1",
            row_number=0,
            check_type="other",
            message="Invalid finding",
        )
    with pytest.raises(ValidationError):
        VerdictContract(ad_id="ad-1", status="unknown")


def test_batch_checks_ad_and_finding_references():
    ad = AdContract(**valid_ad())
    finding = FindingContract(
        finding_id="finding-1",
        ad_id=ad.ad_id,
        row_number=ad.row_number,
        check_type="mechanical",
        message="UTM is missing",
    )
    verdict = VerdictContract(
        ad_id=ad.ad_id,
        status="fix needed",
        finding_ids=[finding.finding_id],
    )

    batch = BatchContract(
        batch_id=ad.batch_id,
        ads=[ad],
        findings=[finding],
        verdicts=[verdict],
    )
    assert batch.ads[0].batch_id == batch.batch_id

    with pytest.raises(ValidationError):
        BatchContract(batch_id="batch-2", ads=[ad])
    with pytest.raises(ValidationError):
        BatchContract(
            batch_id=ad.batch_id,
            ads=[ad],
            verdicts=[
                VerdictContract(
                    ad_id=ad.ad_id,
                    status="ready",
                    finding_ids=["missing-finding"],
                )
            ],
        )
