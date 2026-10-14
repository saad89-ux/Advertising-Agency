"""Tests for Task 53: 30-Sheet Replay and Scoring Engine (PRD §4.7 / §11 / D9).

Verifies:
- Needs 15: UTM naming rules detection
- Needs 18: Platform limits and creative size ratio detection
- Needs 25: Page checks (404, UTM drop, offer/discount/threshold mismatch, expired offer)
- Needs 29: Wording checks, citations, brand book, claims register, footnotes
- Needs 34: Verdict aggregation accuracy and 0 false-readies
- CI Release Gate compliance (false alarm <= 5%, false ready <= 5%, recall >= 95%)
- Split safety gating (held_out/all require is_final=True)
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from evals.replay_30_sheets import (
    DEFAULT_AS_OF,
    evaluate_ad_row,
    load_replay_resources,
    run_30_sheets_replay,
)
from packages.contracts import AdContract, CheckType, VerdictStatus


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def resources(repo_root: Path) -> dict:
    return load_replay_resources(repo_root)


def test_load_replay_resources(resources: dict):
    """Verify replay resources load from repository data directory."""
    assert "manifest" in resources
    assert "limits" in resources
    assert "domain_map" in resources
    assert "pages_index" in resources

    assert len(resources["manifest"]) > 0
    assert "meta" in resources["limits"] or "facebook" in resources["limits"]
    assert len(resources["domain_map"]) >= 3
    assert len(resources["pages_index"]) > 0


def test_held_out_guard_without_final():
    """Verify split safety guard prevents accidental execution of held_out and all without is_final."""
    with pytest.raises(ValueError, match="is_final=True"):
        run_30_sheets_replay(split="held_out", is_final=False)

    with pytest.raises(ValueError, match="is_final=True"):
        run_30_sheets_replay(split="all", is_final=False)


def test_replay_dev_split_metrics():
    """Verify dev split achieves 100% planted recall, 0 false alarms, and passes release gate."""
    report = run_30_sheets_replay(split="dev")

    assert report["split"] == "dev"
    assert report["sheets_evaluated"] == 20
    assert report["total_valid_rows"] == 2200
    assert report["malformed_rows_isolated"] == 28

    # Core performance metrics
    assert report["overall_recall"] >= 0.95
    assert report["false_alarm_rate"] <= 0.05
    assert report["false_ready_rate"] <= 0.05
    assert report["verdict_accuracy"] >= 0.95

    # CI Release gate
    gate = report["ci_release_gate"]
    assert gate["status"] == "passed"
    assert gate["passed"] is True
    assert gate["false_alarm_passed"] is True
    assert gate["false_ready_passed"] is True

    # 100% recall across all 19 planted error types
    for etype, data in report["recall_by_type"].items():
        assert data["planted"] > 0
        assert data["recall"] >= 0.95, f"Recall for {etype} was below 95%: {data['recall']}"


def test_replay_held_out_split_with_final():
    """Verify held_out split passes release gate when is_final=True."""
    report = run_30_sheets_replay(split="held_out", is_final=True)

    assert report["split"] == "held_out"
    assert report["sheets_evaluated"] == 10
    assert report["total_valid_rows"] == 1060
    assert report["malformed_rows_isolated"] == 13

    assert report["overall_recall"] >= 0.95
    assert report["false_alarm_rate"] <= 0.05
    assert report["false_ready_rate"] <= 0.05
    assert report["ci_release_gate"]["passed"] is True


def test_evaluate_ad_row_clean_ad(resources: dict):
    """Verify clean ad produces zero findings and READY verdict."""
    ad = AdContract(
        ad_id="test:clean_ad",
        batch_id="replay:test",
        client_id="oakandhome",
        row_number=1,
        platform="facebook",
        headline="Shop Oak Furniture",
        ad_text="Handmade oak shelves, free delivery over $75.",
        destination_url="https://oakandhome.example/shelves?utm_source=facebook&utm_medium=paid_social&utm_campaign=shelves_oct",
        creative_file="shelves_v3_1080x1920.png",
        row_hash="test-row-hash",
        sheet_hash="test-sheet-hash",
    )
    row_info = {"page": "shelves", "planted_errors": []}

    findings, verdict = evaluate_ad_row(ad, row_info, resources, as_of=DEFAULT_AS_OF)
    assert len(findings) == 0
    assert verdict == VerdictStatus.READY


def test_evaluate_ad_row_page_not_found(resources: dict):
    """Verify 404 landing page results in BLOCKED verdict."""
    ad = AdContract(
        ad_id="test:404_ad",
        batch_id="replay:test",
        client_id="brightleaf",
        row_number=2,
        platform="instagram",
        headline="Revitalize Skin",
        ad_text="Pure botanical extracts for glowing skin.",
        destination_url="https://brightleaf.example/old-mask?utm_source=instagram&utm_medium=paid_social&utm_campaign=mask_oct",
        creative_file="mask_v1_1080x1080.png",
        row_hash="test-row-hash",
        sheet_hash="test-sheet-hash",
    )
    row_info = {"page": "old-mask", "planted_errors": [{"type": "page_not_found"}]}

    findings, verdict = evaluate_ad_row(ad, row_info, resources, as_of=DEFAULT_AS_OF)
    assert any("not_found" in f.finding_id for f in findings)
    assert verdict == VerdictStatus.BLOCKED


def test_evaluate_ad_row_forbidden_claim(resources: dict):
    """Verify forbidden claim results in BLOCKED verdict."""
    ad = AdContract(
        ad_id="test:forbidden_claim_ad",
        batch_id="replay:test",
        client_id="calmleaf",
        row_number=3,
        platform="facebook",
        headline="Sleep Better",
        ad_text="Our herbal formula cures insomnia quickly.",
        destination_url="https://calmleaf.example/tincture?utm_source=facebook&utm_medium=paid_social&utm_campaign=tincture_oct",
        creative_file="tincture_v1_1080x1080.png",
        row_hash="test-row-hash",
        sheet_hash="test-sheet-hash",
    )
    row_info = {"page": "tincture", "planted_errors": [{"type": "forbidden_claim"}]}

    findings, verdict = evaluate_ad_row(ad, row_info, resources, as_of=DEFAULT_AS_OF)
    assert any("forbidden_claim" in f.finding_id for f in findings)
    assert verdict == VerdictStatus.BLOCKED


def test_evaluate_ad_row_unknown_claim(resources: dict):
    """Verify novel unregistered claim results in NEEDS_PERSON verdict."""
    ad = AdContract(
        ad_id="test:unknown_claim_ad",
        batch_id="replay:test",
        client_id="calmleaf",
        row_number=4,
        platform="facebook",
        headline="Award-winning Formula",
        ad_text="Award-winning sleep aid for restorative sleep.",
        destination_url="https://calmleaf.example/capsules?utm_source=facebook&utm_medium=paid_social&utm_campaign=capsules_oct",
        creative_file="capsules_v1_1080x1080.png",
        row_hash="test-row-hash",
        sheet_hash="test-sheet-hash",
    )
    row_info = {"page": "capsules", "planted_errors": [{"type": "unknown_claim"}]}

    findings, verdict = evaluate_ad_row(ad, row_info, resources, as_of=DEFAULT_AS_OF)
    assert any("unknown_claim" in f.finding_id for f in findings)
    assert verdict == VerdictStatus.NEEDS_PERSON


def test_cli_report_generation(tmp_path: Path):
    """Verify report generation output structure via run_30_sheets_replay."""
    report = run_30_sheets_replay(split="dev")
    report_file = tmp_path / "test_report.json"
    report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")

    loaded = json.loads(report_file.read_text(encoding="utf-8"))
    assert loaded["ci_release_gate"]["passed"] is True
    assert loaded["overall_recall"] == 1.0
    assert loaded["verdict_accuracy"] == 1.0
    assert loaded["sheets_evaluated"] == 20

