"""Unit and integration tests for Metrics Screen (Task 52, PRD §4.7, Needs: 50).

Verifies:
1. File structure and placement of Next.js components, route pages, and CSS.
2. Component contracts in MetricsScreen.jsx:
   - Problems by type (mechanical, page, wording), field breakdown, top rules.
   - Ads blocked and verdict counts (ready, fix_needed, blocked, needs_person, pending).
   - Reviewer time, decisions (approved, rejected), and rejection reasons.
   - False alarms (dismissed count, false alarm rate, top dismissed rules, cleaned ads).
   - Financial cost ledger, total spend, avg cost per ad, worker steps, step cap detection.
   - Simulated rejection rates (without-gate %, with-gate %, rejections avoided, protection effectiveness %).
   - Platform send metrics, attempts, retry rate, dead-letter count and rate.
   - CI Release Gate status banner evaluating 5% false alarm and 5% dead-letter thresholds.
3. SSE Live Status Stream Integration (Needs: 50):
   - EventSource connection to /batches/{batch_id}/events with ?token= support.
   - Handling of connected, batch_status, ad_status, and complete events.
   - Live status indicator pill and automatic real-time metrics re-fetching.
4. Navigation, filtering, and export capabilities:
   - Global vs Batch view modes and client filter dropdown.
   - JSON export functionality.
   - Seamless navigation between Batch Review screen and Metrics screen.
5. End-to-end API payload contract validation with FastAPI metrics endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pytest
from fastapi.testclient import TestClient

from apps.api import auth, main
from apps.api.auth import create_access_token
from apps.api.database import get_session
from apps.api.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = PROJECT_ROOT / "apps" / "web"
COMPONENTS_DIR = WEB_DIR / "components"
APP_DIR = WEB_DIR / "app"


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.api import auth, main
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")


# ==============================================================================
# 1. File Structure and Placement Tests
# ==============================================================================

def test_task52_files_exist_in_prescribed_folders():
    """Verify all Metrics Screen files exist in their prescribed locations."""
    expected_files = [
        COMPONENTS_DIR / "MetricsScreen.jsx",
        APP_DIR / "metrics" / "page.jsx",
        APP_DIR / "batches" / "[batchId]" / "metrics" / "page.jsx",
        APP_DIR / "review.css",
    ]
    for file_path in expected_files:
        assert file_path.exists(), f"Expected file does not exist: {file_path}"
        assert file_path.is_file(), f"Expected path to be a file: {file_path}"
        assert file_path.stat().st_size > 0, f"Expected non-empty file: {file_path}"


def test_home_and_review_pages_link_to_metrics():
    """Verify HomePage and ReviewIndexPage include navigation links to /metrics."""
    home_code = (APP_DIR / "page.jsx").read_text(encoding="utf-8")
    assert "/metrics" in home_code, "HomePage must link to /metrics"
    assert "link-home-to-metrics" in home_code

    review_code = (APP_DIR / "review" / "page.jsx").read_text(encoding="utf-8")
    assert "/metrics" in review_code, "ReviewIndexPage must link to /metrics"
    assert "link-review-to-metrics" in review_code


def test_batch_summary_links_to_batch_metrics():
    """Verify BatchSummary includes direct navigation link to /batches/{batchId}/metrics."""
    summary_code = (COMPONENTS_DIR / "BatchSummary.jsx").read_text(encoding="utf-8")
    assert "/metrics" in summary_code, "BatchSummary must link to batch metrics"
    assert "link-batch-metrics" in summary_code


# ==============================================================================
# 2. MetricsScreen.jsx Component Contract Tests (PRD §4.7, Needs 19, 36, 43, 47)
# ==============================================================================

def test_metrics_screen_renders_ci_release_gate_evaluations():
    """Verify MetricsScreen renders the CI release gate banner and threshold checks."""
    code = (COMPONENTS_DIR / "MetricsScreen.jsx").read_text(encoding="utf-8")

    # CI Release gate card and badge
    assert "ci-release-gate-card" in code
    assert "ci-gate-status" in code
    assert "ci-gate-badge" in code
    assert "ci-gate-checks" in code

    # Threshold checks for 5% false alarm ceiling and 5% dead-letter ceiling
    assert "False Alarm Rate <= 5%" in code
    assert "Dead-Letter Rate <= 5%" in code
    assert "gate-passed" in code
    assert "gate-failed" in code


def test_metrics_screen_renders_simulated_rejection_rates():
    """Verify MetricsScreen renders simulated platform rejection rates and protection effectiveness."""
    code = (COMPONENTS_DIR / "MetricsScreen.jsx").read_text(encoding="utf-8")

    assert "section-simulated-rejections" in code
    assert "rate-without-gate" in code
    assert "rate-with-gate" in code
    assert "rejectionsAvoidedCount" in code or "rejections_avoided_count" in code
    assert "protectionEffectiveness" in code or "protection_effectiveness_percent" in code
    assert "kpi-protection-effectiveness" in code


def test_metrics_screen_renders_problems_by_type_and_field():
    """Verify MetricsScreen renders check type breakdown, field breakdown, and top rules (Needs 19)."""
    code = (COMPONENTS_DIR / "MetricsScreen.jsx").read_text(encoding="utf-8")

    # Check type breakdown
    assert "Mechanical Checks" in code
    assert "Page Checks" in code
    assert "Wording & Policy Checks" in code

    # Field breakdown & top rules
    assert "problems-by-field-row" in code
    assert "table-top-rules" in code
    assert "avgFindingsPerAd" in code or "avg_findings_per_ad" in code


def test_metrics_screen_renders_reviewer_decisions_and_false_alarms():
    """Verify MetricsScreen renders reviewer approvals, rejections, false alarms, and cleaned ads (Needs 43)."""
    code = (COMPONENTS_DIR / "MetricsScreen.jsx").read_text(encoding="utf-8")

    # Reviewer actions & KPIs
    assert "reviewer-approved-count" in code
    assert "reviewer-rejected-count" in code
    assert "reviewer-dismissed-count" in code
    assert "reviewer-cleaned-count" in code

    # Top dismissed rules (false alarms) and rejection reasons
    assert "table-top-dismissed-rules" in code
    assert "list-rejection-reasons" in code
    assert "kpi-false-alarm-rate" in code


def test_metrics_screen_renders_cost_ledger_and_step_caps():
    """Verify MetricsScreen displays financial spend and step cap alerts (Needs 36)."""
    code = (COMPONENTS_DIR / "MetricsScreen.jsx").read_text(encoding="utf-8")

    assert "cost-total-val" in code
    assert "cost-avg-ad-val" in code
    assert "cost-total-steps" in code
    assert "cost-step-capped-count" in code
    assert "0.25" in code  # Cost benchmark check per D16 / PRD §4.7 (under $0.25 per 100 ads)


def test_metrics_screen_renders_platform_sends_and_dead_letter():
    """Verify MetricsScreen renders platform delivery, send attempts, retries, and dead letter (Needs 47)."""
    code = (COMPONENTS_DIR / "MetricsScreen.jsx").read_text(encoding="utf-8")

    assert "sends-total" in code
    assert "sends-accepted" in code
    assert "sends-rejected" in code
    assert "sends-attempts" in code
    assert "sends-dead-letter-count" in code


# ==============================================================================
# 3. Server-Sent Events (SSE) Live Status Stream Integration Tests (Needs: 50)
# ==============================================================================

def test_metrics_screen_subscribes_to_sse_events():
    """Verify MetricsScreen connects to the SSE live status stream and listens for event types."""
    code = (COMPONENTS_DIR / "MetricsScreen.jsx").read_text(encoding="utf-8")

    # EventSource initialization
    assert "new EventSource" in code, "MetricsScreen must initialize EventSource"
    assert "/events" in code, "EventSource URL must target /events endpoint"
    assert "?token=" in code, "EventSource URL must support ?token= query param for browser auth"

    # Event listeners
    assert 'addEventListener("connected"' in code
    assert 'addEventListener("batch_status"' in code
    assert 'addEventListener("ad_status"' in code
    assert 'addEventListener("complete"' in code

    # Live status pill indicator
    assert "metrics-live-status-pill" in code
    assert "Live Stream Active" in code

    # Stream cleanup
    assert "es.close()" in code, "EventSource must be cleanly closed on component unmount"


# ==============================================================================
# 4. Export, Filtering & Route Tests
# ==============================================================================

def test_metrics_screen_supports_json_export_and_mode_switching():
    """Verify MetricsScreen includes export JSON button and Global vs Batch toggle."""
    code = (COMPONENTS_DIR / "MetricsScreen.jsx").read_text(encoding="utf-8")

    assert "btn-metrics-export" in code
    assert "btn-mode-global" in code
    assert "btn-mode-batch" in code
    assert "select-client-filter" in code
    assert "btn-metrics-refresh" in code


def test_review_css_contains_all_metrics_classes():
    """Verify review.css defines necessary styles for the Metrics Screen."""
    css = (APP_DIR / "review.css").read_text(encoding="utf-8")

    required_classes = [
        ".metrics-screen-container",
        ".ci-release-gate-card",
        ".gate-passed",
        ".gate-failed",
        ".primary-kpi-grid",
        ".rates-comparison-grid",
        ".rate-box",
        ".stat-bars-list",
        ".reviewer-kpi-subgrid",
        ".mini-data-table",
        ".btn-view-metrics",
    ]
    for cls in required_classes:
        assert cls in css, f"review.css must define {cls}"


# ==============================================================================
# 5. FastAPI End-to-End Contract Integration Test
# ==============================================================================

def setup_users(monkeypatch: pytest.MonkeyPatch) -> None:
    users_db = {
        "buyer-1": {"user_id": "buyer-1", "email": "buyer1@agency.test", "display_name": "Buyer One", "role": "buyer", "is_active": True},
    }
    user_clients = {
        "buyer-1": ["c1", "c2"],
    }
    monkeypatch.setattr(auth.users, "get_user_by_id", lambda uid: users_db.get(uid))
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda uid: user_clients.get(uid, []))


def test_metrics_api_contract_matches_frontend_expectations(monkeypatch: pytest.MonkeyPatch):
    """Verify the FastAPI backend returns exact schema keys expected by MetricsScreen."""
    setup_users(monkeypatch)
    client = TestClient(app)
    token = create_access_token(user_id="buyer-1", role="buyer")

    # Test global metrics endpoint
    res = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, f"Expected 200 OK, got {res.status_code}: {res.text}"
    global_data = res.json()

    # Core global contract keys
    expected_global_keys = [
        "clients",
        "total_batches",
        "total_ads",
        "total_cost_usd",
        "avg_cost_per_batch_usd",
        "avg_cost_per_ad_usd",
        "overall_blocked_rate",
        "overall_false_alarm_rate",
        "problems_by_type",
        "dead_letter",
        "ci_release_gate",
        "batches",
    ]
    for k in expected_global_keys:
        assert k in global_data, f"Global metrics response missing expected key: {k}"

    # CI Release gate contract
    ci_gate = global_data["ci_release_gate"]
    assert "status" in ci_gate
    assert "passed" in ci_gate
    assert "checks" in ci_gate
    assert isinstance(ci_gate["checks"], list)
    assert len(ci_gate["checks"]) >= 2
    for check in ci_gate["checks"]:
        assert "name" in check
        assert "target" in check
        assert "actual" in check
        assert "passed" in check

    # Problems by type breakdown
    prob = global_data["problems_by_type"]
    assert "mechanical" in prob
    assert "page" in prob
    assert "wording" in prob

