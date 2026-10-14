"""Tests for citation verification of wording check findings."""

from __future__ import annotations

from pathlib import Path
import pytest

from apps.worker.citation_verification import (
    CitationStatus,
    CitationVerificationReport,
    get_all_known_rule_ids,
    get_known_client_rule_ids,
    infer_client_from_citation,
    verify_ad_findings,
    verify_citation_entry,
    verify_finding_citation,
    verify_findings_citations,
)
from apps.worker.retrieval import RetrievedRule
from packages.contracts import AdContract, CheckType, FindingContract
from packages.guards.output_guard import validate_model_output

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_ROOT = REPO_ROOT / "data" / "clients"


def sample_ad(**overrides) -> AdContract:
    values = {
        "ad_id": "ad-cit-1",
        "batch_id": "batch-1",
        "client_id": "brightleaf",
        "row_number": 1,
        "platform": "Facebook",
        "ad_text": "Serum promotion",
        "headline": "Shop now",
        "destination_url": "https://shop.brightleaf.example/serum",
        "creative_file": None,
        "row_hash": "row-hash",
        "sheet_hash": "sheet-hash",
    }
    values.update(overrides)
    return AdContract(**values)


def sample_wording_finding(rule_id: str | None = "BL-C-01", **overrides) -> FindingContract:
    values = {
        "finding_id": "finding-1",
        "ad_id": "ad-cit-1",
        "row_number": 1,
        "check_type": CheckType.WORDING,
        "message": "Unsubstantiated claim about hydration.",
        "field": "ad_text",
        "proposed_fix": "Use approved language.",
        "rule_id": rule_id,
    }
    values.update(overrides)
    return FindingContract(**values)


def sample_mechanical_finding(**overrides) -> FindingContract:
    values = {
        "finding_id": "finding-mech-1",
        "ad_id": "ad-cit-1",
        "row_number": 1,
        "check_type": CheckType.MECHANICAL,
        "message": "Missing UTM source parameter.",
        "field": "destination_url",
        "proposed_fix": "Add utm_source=facebook",
        "rule_id": "utm.missing_source",
    }
    values.update(overrides)
    return FindingContract(**values)


# --- Citation Verification Unit Tests ---


def test_verified_citation_when_present_in_retrieved_rules():
    retrieved = [
        RetrievedRule("brightleaf", "BL-BB-1.1", "brand_book.md", "Voice", 0.05),
        RetrievedRule("brightleaf", "BL-C-01", "claims_register.md", "Hydration", 0.12),
    ]

    res = verify_citation_entry("BL-C-01", retrieved, client_id="brightleaf")
    assert res.is_valid is True
    assert res.status == CitationStatus.VERIFIED
    assert res.rule_id == "BL-C-01"
    assert res.retrieved_rule == retrieved[1]


def test_invented_citation_rejected():
    retrieved = [
        RetrievedRule("brightleaf", "BL-C-01", "claims_register.md", "Hydration", 0.1),
    ]

    # Made up ID
    res1 = verify_citation_entry("MADE-UP-999", retrieved, client_id="brightleaf")
    assert res1.is_valid is False
    assert res1.status == CitationStatus.INVENTED
    assert "invented" in res1.reason.lower()

    # Client-prefixed but non-existent rule
    res2 = verify_citation_entry("BL-C-999", retrieved, client_id="brightleaf")
    assert res2.is_valid is False
    assert res2.status == CitationStatus.INVENTED


def test_cross_client_citation_rejected():
    retrieved = [
        RetrievedRule("brightleaf", "BL-C-01", "claims_register.md", "Hydration", 0.1),
    ]

    # Citing Calmleaf claim CL-C-01 for a Brightleaf ad
    res = verify_citation_entry("CL-C-01", retrieved, client_id="brightleaf")
    assert res.is_valid is False
    assert res.status == CitationStatus.CROSS_CLIENT
    assert "belongs to client 'calmleaf'" in res.reason


def test_unretrieved_existing_rule_rejected():
    """Rule exists for client in brand book, but was not retrieved for this ad check."""
    # Only BL-C-01 was retrieved
    retrieved = [
        RetrievedRule("brightleaf", "BL-C-01", "claims_register.md", "Hydration", 0.1),
    ]

    # BL-BB-1.1 exists in Brightleaf's brand book, but was not retrieved
    res = verify_citation_entry("BL-BB-1.1", retrieved, client_id="brightleaf")
    assert res.is_valid is False
    assert res.status == CitationStatus.NOT_RETRIEVED
    assert "was not among retrieved rules" in res.reason


def test_missing_citation_for_wording_finding_rejected():
    finding = sample_wording_finding(rule_id=None)
    res = verify_finding_citation(finding, ["BL-C-01"], client_id="brightleaf")
    assert res.is_valid is False
    assert res.status == CitationStatus.MISSING

    # Direct empty citation checks
    empty_res = verify_citation_entry("", ["BL-C-01"], client_id="brightleaf")
    assert empty_res.is_valid is False
    assert empty_res.status == CitationStatus.MISSING

    ws_res = verify_citation_entry("   ", ["BL-C-01"], client_id="brightleaf")
    assert ws_res.is_valid is False
    assert ws_res.status == CitationStatus.MISSING


def test_non_wording_findings_are_not_applicable_and_pass():
    finding = sample_mechanical_finding()
    res = verify_finding_citation(finding, ["BL-C-01"], client_id="brightleaf")
    assert res.is_valid is True
    assert res.status == CitationStatus.NOT_APPLICABLE


def test_verify_findings_citations_batch_reporting():
    retrieved = [
        RetrievedRule("brightleaf", "BL-C-01", "claims_register.md", "Hydration", 0.1),
    ]

    f_valid = sample_wording_finding("BL-C-01", finding_id="f1")
    f_invented = sample_wording_finding("INVENTED-99", finding_id="f2")
    f_cross = sample_wording_finding("OH-C-01", finding_id="f3")
    f_mech = sample_mechanical_finding(finding_id="f4")

    report = verify_findings_citations(
        [f_valid, f_invented, f_cross, f_mech],
        retrieved,
        client_id="brightleaf",
    )

    assert report.is_valid is False
    assert report.verified_count == 1
    assert report.failed_count == 2
    assert report.needs_person is True
    assert [f.finding_id for f in report.accepted_findings] == ["f1", "f4"]
    assert [f.finding_id for f in report.rejected_findings] == ["f2", "f3"]
    assert any("invented" in r for r in report.rejection_reasons)
    assert any("cross_client" in r for r in report.rejection_reasons)


def test_verify_ad_findings_uses_ad_client_id():
    ad = sample_ad(client_id="oakandhome")
    retrieved = [
        RetrievedRule("oakandhome", "OH-BB-1.1", "brand_book.md", "Oak Voice", 0.1),
    ]

    f_valid = sample_wording_finding("OH-BB-1.1", ad_id=ad.ad_id)
    report = verify_ad_findings(ad, [f_valid], retrieved)

    assert report.is_valid is True
    assert report.verified_count == 1
    assert report.failed_count == 0
    assert report.needs_person is False


def test_output_guard_integration_with_retrieved_rule_ids():
    ad = sample_ad(client_id="brightleaf")
    rule_index = {
        "brightleaf": {"brand_book": ["BL-BB-1.1"], "register": ["BL-C-01", "BL-C-02"]},
        "calmleaf": {"register": ["CL-C-01"]},
    }
    client_texts = {"brightleaf": "", "calmleaf": ""}

    # Model output cites BL-C-02, but retrieved rules ONLY contain BL-C-01
    model_raw = {
        "findings": [
            {"message": "Valid citation in retrieved.", "check_type": "wording", "rule_id": "BL-C-01"},
            {"message": "Unretrieved citation.", "check_type": "wording", "rule_id": "BL-C-02"},
            {"message": "Invented citation.", "check_type": "wording", "rule_id": "BL-C-999"},
        ]
    }

    result = validate_model_output(
        model_raw,
        ad=ad,
        rule_index=rule_index,
        client_texts=client_texts,
        retrieved_rule_ids=["BL-C-01"],
    )

    assert len(result.findings) == 1
    assert result.findings[0].rule_id == "BL-C-01"
    assert result.needs_person is True
    assert result.rejected_reasons == ["unknown_or_missing_rule_id", "unknown_or_missing_rule_id"]


def test_all_pilot_clients_indexed_entries_are_validated_correctly():
    """Verify that every indexed entry in rule_index.json is recognized and mapped."""
    all_rules = get_all_known_rule_ids(rules_root=RULES_ROOT)
    assert len(all_rules) == 29

    for entry_id, owner_client in all_rules.items():
        # Infers correct client from prefix
        assert infer_client_from_citation(entry_id) == owner_client
        # Known IDs for this client include it
        assert entry_id in get_known_client_rule_ids(owner_client, rules_root=RULES_ROOT)

        # Verifies successfully when in retrieved rules
        res = verify_citation_entry(entry_id, [entry_id], client_id=owner_client)
        assert res.is_valid is True
        assert res.status == CitationStatus.VERIFIED

        # Cross-client test: verify it is flagged if checked against another client
        other_client = "oakandhome" if owner_client != "oakandhome" else "brightleaf"
        cross_res = verify_citation_entry(entry_id, [entry_id], client_id=other_client)
        assert cross_res.is_valid is False
        assert cross_res.status == CitationStatus.CROSS_CLIENT
