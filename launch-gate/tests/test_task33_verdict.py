"""Tests for Task 33: Verdict Aggregation (ready / fix needed / blocked / needs a person)."""

from __future__ import annotations

import pytest

from apps.worker.verdict import (
    BatchVerdictReport,
    aggregate_ad_verdict,
    aggregate_batch_verdicts,
    classify_finding_severity,
)
from packages.contracts import (
    AdContract,
    BatchContract,
    CheckType,
    FindingContract,
    VerdictContract,
    VerdictStatus,
)


def sample_ad(
    ad_id: str = "ad-v-1",
    batch_id: str = "batch-1",
    client_id: str = "brightleaf",
    platform: str = "Facebook",
    headline: str = "Natural Skincare",
    ad_text: str = "Pure organic serum.",
    row_number: int = 1,
) -> AdContract:
    return AdContract(
        ad_id=ad_id,
        batch_id=batch_id,
        client_id=client_id,
        row_number=row_number,
        platform=platform,
        headline=headline,
        ad_text=ad_text,
        destination_url="https://brightleaf.example/serum",
        creative_file=None,
        row_hash="rowhash",
        sheet_hash="sheethash",
    )


def sample_finding(
    finding_id: str,
    ad_id: str = "ad-v-1",
    check_type: CheckType = CheckType.MECHANICAL,
    message: str = "Problem detected",
    field: str | None = "ad_text",
    proposed_fix: str | None = "Fix issue",
    rule_id: str | None = None,
    row_number: int = 1,
) -> FindingContract:
    return FindingContract(
        finding_id=finding_id,
        ad_id=ad_id,
        row_number=row_number,
        check_type=check_type,
        message=message,
        field=field,
        proposed_fix=proposed_fix,
        rule_id=rule_id,
    )


def test_verdict_ready_for_clean_ad():
    ad = sample_ad()
    verdict = aggregate_ad_verdict(ad, findings=[])

    assert verdict.ad_id == ad.ad_id
    assert verdict.status is VerdictStatus.READY
    assert verdict.finding_ids == []
    assert "Ready for launch" in verdict.summary


def test_verdict_fix_needed_for_mechanical_issues():
    ad = sample_ad()
    f1 = sample_finding(
        "ad-v-1:mechanical:utm_missing",
        field="destination_url",
        message="Missing utm_source in tracking parameters",
    )
    f2 = sample_finding(
        "ad-v-1:mechanical:headline_length",
        field="headline",
        message="Headline exceeds 40 characters limit",
    )

    verdict = aggregate_ad_verdict(ad, [f1, f2])

    assert verdict.status is VerdictStatus.FIX_NEEDED
    assert verdict.finding_ids == [f1.finding_id, f2.finding_id]
    assert "Fix needed" in verdict.summary
    assert "destination_url" in verdict.summary
    assert "headline" in verdict.summary


def test_verdict_blocked_for_expired_offer():
    ad = sample_ad()
    f = sample_finding(
        "ad-v-1:page_offer:page_expired_offer:offer",
        check_type=CheckType.PAGE,
        field="offer",
        message="The offer ended on 2026-09-30; page says the sale has ended",
    )

    assert classify_finding_severity(f) is VerdictStatus.BLOCKED
    verdict = aggregate_ad_verdict(ad, [f])

    assert verdict.status is VerdictStatus.BLOCKED
    assert verdict.finding_ids == [f.finding_id]
    assert "Blocked" in verdict.summary
    assert "sale has ended" in verdict.summary


def test_verdict_blocked_for_forbidden_claim():
    ad = sample_ad()
    f = sample_finding(
        "ad-v-1:wording:forbidden_claim",
        check_type=CheckType.WORDING,
        field="ad_text",
        message="Forbidden claim: eco-friendly timber claim is prohibited",
        rule_id="OH-C-04",
    )

    assert classify_finding_severity(f) is VerdictStatus.BLOCKED
    verdict = aggregate_ad_verdict(ad, [f])

    assert verdict.status is VerdictStatus.BLOCKED
    assert "Blocked" in verdict.summary


def test_verdict_blocked_takes_precedence_over_fix_needed():
    ad = sample_ad()
    f_fix = sample_finding("ad-v-1:mechanical:utm_misspelled", message="Wrong UTM casing")
    f_block = sample_finding(
        "ad-v-1:page_offer:page_expired_offer:offer",
        check_type=CheckType.PAGE,
        message="Offer expired on 2026-09-30",
    )

    verdict = aggregate_ad_verdict(ad, [f_fix, f_block])

    assert verdict.status is VerdictStatus.BLOCKED
    assert f_fix.finding_id in verdict.finding_ids
    assert f_block.finding_id in verdict.finding_ids
    assert "Blocked" in verdict.summary


def test_verdict_needs_person_for_unknown_claim():
    ad = sample_ad()
    f = sample_finding(
        "ad-v-1:wording:unknown_claim",
        check_type=CheckType.WORDING,
        message="Claim cannot be verified from client register; flagged for human review",
    )

    assert classify_finding_severity(f) is VerdictStatus.NEEDS_PERSON
    verdict = aggregate_ad_verdict(ad, [f])

    assert verdict.status is VerdictStatus.NEEDS_PERSON
    assert "Needs human review" in verdict.summary


def test_verdict_needs_person_for_unverified_citation():
    ad = sample_ad()
    f = sample_finding(
        "ad-v-1:wording:unverified_citation:FAKE-1",
        check_type=CheckType.WORDING,
        message="Wording finding cited unverified/invented rule FAKE-1",
    )

    assert classify_finding_severity(f) is VerdictStatus.NEEDS_PERSON
    verdict = aggregate_ad_verdict(ad, [f])

    assert verdict.status is VerdictStatus.NEEDS_PERSON


def test_verdict_needs_person_for_spend_cap_exceeded():
    ad = sample_ad()
    f = sample_finding(
        "ad-v-1:wording:spend_cap_exceeded",
        check_type=CheckType.WORDING,
        message="Spend cap exceeded ($0.01 per row limit reached); flagged for human review",
    )

    assert classify_finding_severity(f) is VerdictStatus.NEEDS_PERSON
    verdict = aggregate_ad_verdict(ad, [f])

    assert verdict.status is VerdictStatus.NEEDS_PERSON
    assert "Spend cap exceeded" in verdict.summary


def test_verdict_needs_person_for_stale_limits():
    ad = sample_ad()
    f = sample_finding(
        "ad-v-1:mechanical:stale_platform_limits",
        check_type=CheckType.MECHANICAL,
        message="Platform limits older than 2024-01-01; confirm limits with human reviewer",
    )

    assert classify_finding_severity(f) is VerdictStatus.NEEDS_PERSON
    verdict = aggregate_ad_verdict(ad, [f])

    assert verdict.status is VerdictStatus.NEEDS_PERSON


def test_verdict_floor_raises_ready_to_needs_person():
    ad = sample_ad()
    # Ad has 0 findings, but guard flagged adversarial signal
    verdict = aggregate_ad_verdict(
        ad,
        findings=[],
        verdict_floor=VerdictStatus.NEEDS_PERSON,
    )

    assert verdict.status is VerdictStatus.NEEDS_PERSON
    assert verdict.finding_ids == []
    assert "guard" in verdict.summary.casefold() or "human review" in verdict.summary.casefold()


def test_verdict_step_cap_exceeded_triggers_needs_person():
    ad = sample_ad()
    f = sample_finding("ad-v-1:mechanical:utm_missing", message="Missing UTM")

    # step_count is 6, exceeding max_steps=5
    verdict = aggregate_ad_verdict(ad, [f], step_count=6, max_steps=5)

    assert verdict.status is VerdictStatus.NEEDS_PERSON
    assert "Step cap exceeded" in verdict.summary


def test_batch_verdict_aggregation_and_contract_validation():
    batch_id = "batch-test-101"

    ad_ready = sample_ad(ad_id="ad-1", batch_id=batch_id, row_number=1)
    ad_fix = sample_ad(ad_id="ad-2", batch_id=batch_id, row_number=2)
    ad_blocked = sample_ad(ad_id="ad-3", batch_id=batch_id, row_number=3)
    ad_person = sample_ad(ad_id="ad-4", batch_id=batch_id, row_number=4)

    ads = [ad_ready, ad_fix, ad_blocked, ad_person]

    f_fix = sample_finding("ad-2:mechanical:utm_missing", ad_id="ad-2", row_number=2, message="Missing UTM")
    f_blocked = sample_finding(
        "ad-3:page:expired_offer",
        ad_id="ad-3",
        row_number=3,
        check_type=CheckType.PAGE,
        message="Offer expired",
    )
    f_person = sample_finding(
        "ad-4:wording:unknown_claim",
        ad_id="ad-4",
        row_number=4,
        check_type=CheckType.WORDING,
        message="Claim cannot be verified from register",
    )

    findings = [f_fix, f_blocked, f_person]

    report = aggregate_batch_verdicts(batch_id, ads, findings)

    assert isinstance(report, BatchVerdictReport)
    assert report.batch_id == batch_id
    assert report.total_ads == 4
    assert report.ready_count == 1
    assert report.fix_needed_count == 1
    assert report.blocked_count == 1
    assert report.needs_person_count == 1

    # Verify underlying BatchContract passes Pydantic validation
    assert isinstance(report.batch_contract, BatchContract)
    assert report.batch_contract.batch_id == batch_id
    assert len(report.batch_contract.verdicts) == 4
    assert "1 ready, 1 fix needed, 1 blocked, 1 needs a person" in report.summary


def test_batch_verdict_empty_batch():
    report = aggregate_batch_verdicts("batch-empty", ads=[], findings=[])

    assert report.total_ads == 0
    assert report.ready_count == 0
    assert report.batch_contract.verdicts == []
    assert "0 ad(s) checked" in report.summary
