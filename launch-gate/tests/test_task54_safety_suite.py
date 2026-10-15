"""Tests for Task 54: Cross-Cutting Safety Suite (PRD §3, §4.6, §4.8, Needs: 28, 39, 47).

Verifies critical safety, idempotency, failure-recovery, and tenant isolation guarantees:
1. 500 repeat clicks & retries: zero duplicates on platform, zero sends on unapproved or tampered ads.
2. Worker killed mid-send recoveries:
   - Crash after 'sending' transition before platform call: safe recovery with same idempotency key.
   - Crash after platform commit before DB commit: safe recovery via Idempotent-Replay header, 0 duplicates.
   - Crash with unrecovered worker: stuck-send sweep reconciles to dead_letter, records person alert,
     blocks automatic relaunch, and enables reviewer-driven retry.
3. Cross-client isolation probes:
   - Rule index queries strictly isolated per client (0 cross-client rule leaks).
   - API penetration probes across batches, ads, approvals, edits, dismissals, sends, alerts,
     and metrics return HTTP 403 Forbidden on unassigned client access.
   - 12 adversarial rows prompt injection containment: zero policy breaches, zero cross-client leaks,
     nothing approved or sent by text alone.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from evals.cross_cutting_safety_eval import (
    run_500_repeat_clicks_eval,
    run_adversarial_rows_containment,
    run_cross_client_probes_eval,
    run_cross_cutting_safety_suite,
    run_worker_killed_mid_send_eval,
)


def test_500_repeat_clicks_sequential_and_concurrent():
    """Verify zero duplicate sends under 500 repeat clicks and retries (PRD §3 / Needs: 39, 47)."""
    report = run_500_repeat_clicks_eval()

    assert report["passed"] is True
    # Sequential test
    seq = report["sequential_test"]
    assert seq["total_clicks"] == 500
    assert seq["idempotent_replays"] == 499
    assert seq["platform_ads_stored"] == 1
    assert seq["platform_duplicates"] == 0

    # Concurrent test
    conc = report["concurrent_test"]
    assert conc["total_threads"] == 500
    assert conc["platform_ads_stored"] == 1
    assert conc["platform_duplicates"] == 0

    # Safety gates
    assert report["unapproved_clicks_rejected"] == 500
    assert report["tampered_clicks_rejected"] == 500


def test_worker_killed_mid_send_recovery():
    """Verify state recovery when worker is killed at different points during send (Needs: 39, 47)."""
    report = run_worker_killed_mid_send_eval()

    assert report["passed"] is True
    # Sub-scenario 2a: Pre-network crash
    pre = report["pre_network_crash_recovery"]
    assert pre["recovered_status"] == "sent"
    assert pre["platform_duplicates"] == 0

    # Sub-scenario 2b: Post-commit crash
    post = report["post_commit_crash_recovery"]
    assert post["recovered_status"] == "sent"
    assert post["idempotent_replay"] is True
    assert post["platform_duplicates"] == 0

    # Sub-scenario 2c: Orphaned sending sweep
    sweep = report["orphaned_sending_sweep"]
    assert sweep["reconciled_count"] == 1
    assert sweep["reconciled_state"] == "dead_letter"
    assert sweep["alert_type"] == "stuck_sending"
    assert sweep["auto_relaunch_prevented"] is True


def test_cross_client_isolation_and_penetration_probes():
    """Verify zero cross-client rule leaks and 100% penetration probe containment (Needs: 28)."""
    report = run_cross_client_probes_eval()

    assert report["passed"] is True
    assert report["rule_overlap_count"] == 0
    assert report["api_probes_blocked"] == report["api_probes_total"]
    assert report["api_probes_total"] == 10

    # Verify each probe was properly rejected
    for probe in report["api_probe_details"]:
        assert probe["blocked"] is True, f"Probe {probe['probe']} was not blocked: {probe}"


def test_adversarial_rows_containment_and_zero_policy_breaches():
    """Verify containment across all 12 adversarial prompt injection rows (PRD §3 / Needs: 28)."""
    report = run_adversarial_rows_containment()

    assert report["total_rows"] == 12
    assert report["contained_count"] == 12
    assert "zero cross-client leaks" in report["policy"]

    for row in report["details"]:
        assert row["contained"] is True, f"Adversarial row #{row['row_number']} breached containment"


def test_cross_cutting_safety_suite_end_to_end(tmp_path: Path):
    """Verify complete safety suite evaluation and JSON audit report output."""
    report = run_cross_cutting_safety_suite(scenario="all")

    assert report["status"] == "passed"
    assert report["passed"] is True
    assert "repeat_clicks" in report["scenarios"]
    assert "worker_killed" in report["scenarios"]
    assert "cross_client" in report["scenarios"]

    # Verify report export
    out_file = tmp_path / "safety_report.json"
    out_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    loaded = json.loads(out_file.read_text(encoding="utf-8"))

    assert loaded["status"] == "passed"
    assert loaded["scenarios"]["repeat_clicks"]["sequential_test"]["platform_duplicates"] == 0
    assert loaded["scenarios"]["worker_killed"]["orphaned_sending_sweep"]["auto_relaunch_prevented"] is True
    assert loaded["scenarios"]["cross_client"]["adversarial_containment"]["contained_count"] == 12
