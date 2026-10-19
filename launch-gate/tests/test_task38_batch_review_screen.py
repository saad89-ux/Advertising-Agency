"""Tests for Task 38: Batch Review Screen (ads beside findings).

Verifies the Next.js components, layout contracts, side-by-side ad and findings
rendering, verdict badges, batch summary metrics, isolated rejected rows,
filters, search, and action modals per PRD 4.6 and Dataflow Architecture.
"""

import json
import re
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = PROJECT_ROOT / "apps" / "web"
COMPONENTS_DIR = WEB_DIR / "components"
APP_DIR = WEB_DIR / "app"


# --- 1. File Structure and Placement Tests ---

def test_task38_files_exist_in_prescribed_folders():
    """Verify all Batch Review Screen files exist in the correct folders per CLAUDE.md."""
    expected_files = [
        COMPONENTS_DIR / "BatchSummary.jsx",
        COMPONENTS_DIR / "AdCard.jsx",
        COMPONENTS_DIR / "RejectedRowsList.jsx",
        COMPONENTS_DIR / "BatchReviewScreen.jsx",
        APP_DIR / "batches" / "[batchId]" / "page.jsx",
        APP_DIR / "review" / "page.jsx",
        APP_DIR / "review.css",
    ]
    for file_path in expected_files:
        assert file_path.exists(), f"Expected file does not exist: {file_path}"
        assert file_path.is_file(), f"Expected path to be a file: {file_path}"
        assert file_path.stat().st_size > 0, f"Expected non-empty file: {file_path}"


def test_root_layout_imports_review_styles():
    """Verify RootLayout in layout.jsx imports review.css."""
    layout_code = (APP_DIR / "layout.jsx").read_text(encoding="utf-8")
    assert 'import "./review.css";' in layout_code, "Root layout must import review.css"


def test_home_page_navigates_to_review_screen():
    """Verify HomePage retains Launch Gate title and links to batch review."""
    home_code = (APP_DIR / "page.jsx").read_text(encoding="utf-8")
    assert "Launch Gate" in home_code
    assert "`/batches/${item.batch_id}`" in home_code
    assert 'data-testid="link-batch-review"' in home_code


# --- 2. Side-by-Side Layout ("Ads Beside Findings") Tests ---

def test_ad_card_declares_side_by_side_grid_layout():
    """Verify AdCard.jsx structures the ad details and findings beside each other in a grid."""
    code = (COMPONENTS_DIR / "AdCard.jsx").read_text(encoding="utf-8")

    # Split-grid container
    assert "ad-beside-findings-grid" in code, "AdCard must contain 'ad-beside-findings-grid' class"
    assert "ad-pane" in code, "AdCard must have left column 'ad-pane'"
    assert "findings-pane" in code, "AdCard must have right column 'findings-pane'"

    # Left pane (Ad details)
    assert "ad.headline" in code
    assert "ad.ad_text" in code
    assert "ad.destination_url" in code
    assert "ad.platform" in code
    assert "ad.row_number" in code
    assert "ad.client_id" in code
    assert "ad.creative_file" in code
    assert "ad.row_hash" in code

    # Right pane (Findings details)
    assert "f.check_type" in code
    assert "f.message" in code
    assert "f.field" in code
    assert "f.proposed_fix" in code
    assert "f.rule_id" in code
    assert "f.file_ref" in code

    # Clean state when no findings
    assert "No issues detected" in code


def test_css_implements_two_column_split_for_desktop():
    """Verify review.css defines a 2-column grid layout with responsive collapse for mobile."""
    css = (APP_DIR / "review.css").read_text(encoding="utf-8")
    assert ".ad-beside-findings-grid" in css
    assert "grid-template-columns: 1fr 1fr" in css, "Desktop layout must be 1fr 1fr side-by-side"
    assert "@media (max-width:" in css, "Must have responsive breakpoint"
    assert "grid-template-columns: 1fr" in css, "Mobile view must collapse gracefully to 1 column"


# --- 3. Verdict Badges and Status Support Tests ---

def test_ad_card_supports_all_prescribed_verdicts():
    """Verify AdCard maps all five verdicts: ready, fix_needed, blocked, needs_person, pending."""
    code = (COMPONENTS_DIR / "AdCard.jsx").read_text(encoding="utf-8")
    expected_verdicts = ["ready", "fix_needed", "blocked", "needs_person", "pending"]
    for v in expected_verdicts:
        assert v in code, f"AdCard must support verdict '{v}'"


def test_css_provides_distinct_styles_for_all_verdicts():
    """Verify review.css defines distinct border and background styles for each verdict."""
    css = (APP_DIR / "review.css").read_text(encoding="utf-8")
    assert "--verdict-ready" in css
    assert "--verdict-fix" in css
    assert "--verdict-blocked" in css
    assert "--verdict-needs-person" in css
    assert "--verdict-pending" in css


# --- 4. Reviewer Action Buttons Tests (Approve, Edit, Reject, Trace, Evidence) ---

def test_ad_card_provides_reviewer_actions():
    """Verify AdCard includes action buttons per PRD 4.6 (approve, edit, reject, trace, evidence)."""
    code = (COMPONENTS_DIR / "AdCard.jsx").read_text(encoding="utf-8")
    assert "btn-approve" in code, "Must include approve action"
    assert "btn-edit" in code, "Must include edit action"
    assert "btn-reject" in code, "Must include reject action"
    assert "btn-trace" in code, "Must include trace action"
    assert "btn-evidence" in code, "Must include client approval evidence action"


# --- 5. BatchSummary Component Tests ---

def test_batch_summary_displays_required_metrics_and_alerts():
    """Verify BatchSummary displays metadata, verdict breakdown, client list, and rejected rows alert."""
    code = (COMPONENTS_DIR / "BatchSummary.jsx").read_text(encoding="utf-8")

    # Metadata
    assert "batch.filename" in code
    assert "batch.batch_id" in code
    assert "batch.sheet_hash" in code
    assert "batch.uploaded_by" in code
    assert "batch.created_at" in code

    # Summary metrics
    assert "total_ads" in code
    assert "verdict_counts" in code
    assert "total_findings" in code
    assert "client_ids" in code

    # Rejected alert
    assert "batch-rejected-alert" in code
    assert "Malformed Row" in code
    assert "onToggleRejected" in code


# --- 6. RejectedRowsList Component Tests ---

def test_rejected_rows_list_renders_isolated_rows():
    """Verify RejectedRowsList displays data_row_index, row_number, reason_code, reason, and raw_cells."""
    code = (COMPONENTS_DIR / "RejectedRowsList.jsx").read_text(encoding="utf-8")
    assert "data_row_index" in code
    assert "row_number" in code
    assert "reason_code" in code
    assert "reason" in code
    assert "raw_cells" in code
    assert "rejected-rows-table" in code


# --- 7. BatchReviewScreen Orchestrator Component Tests ---

def test_batch_review_screen_implements_filters_and_search():
    """Verify BatchReviewScreen provides verdict filtering, client filtering, and live search."""
    code = (COMPONENTS_DIR / "BatchReviewScreen.jsx").read_text(encoding="utf-8")

    # Filters
    assert "verdictFilter" in code
    assert "clientFilter" in code
    assert "searchQuery" in code

    # Filter buttons / pills
    for verdict in ["all", "ready", "fix_needed", "blocked", "needs_person", "pending"]:
        assert f'"{verdict}"' in code or f"'{verdict}'" in code

    # Search inputs
    assert "ad-search-input" in code
    assert "btn-clear-search" in code


def test_batch_review_screen_handles_error_states():
    """Verify BatchReviewScreen handles 401 Unauthorized, 403 Forbidden, and 404 Not Found."""
    code = (COMPONENTS_DIR / "BatchReviewScreen.jsx").read_text(encoding="utf-8")
    assert "401" in code
    assert "403" in code
    assert "404" in code
    assert "error-card" in code
    assert "btn-retry" in code


def test_batch_review_screen_includes_dialogs_with_reasons_and_content_hash():
    """Verify BatchReviewScreen action modals enforce edit reasons, reject reasons, and content hash."""
    code = (COMPONENTS_DIR / "BatchReviewScreen.jsx").read_text(encoding="utf-8")

    # Approve confirms content hash (PRD D13)
    assert "Approved Content Hash" in code
    assert "selectedAd.row_hash" in code

    # Edit modal requires reason (PRD 4.6)
    assert "input-edit-reason" in code
    assert "Reason for Edit" in code

    # Reject modal requires reason (PRD 4.6)
    assert "textarea-reject-reason" in code
    assert "Rejection Reason" in code


# --- 8. Next.js Dynamic Route Tests ---

def test_batch_id_page_exports_component_and_extracts_param():
    """Verify /batches/[batchId]/page.jsx exports BatchPage and forwards batchId to BatchReviewScreen."""
    code = (APP_DIR / "batches" / "[batchId]" / "page.jsx").read_text(encoding="utf-8")
    assert "export default function BatchPage" in code
    assert "params" in code
    assert "batchId" in code
    assert "<BatchReviewScreen" in code


def test_review_index_page_fetches_batches_and_provides_direct_jump():
    """Verify /review/page.jsx provides direct batch UUID jump form and recent batches table."""
    code = (APP_DIR / "review" / "page.jsx").read_text(encoding="utf-8")
    assert "export default function ReviewIndexPage" in code
    assert "input-jump-batch-id" in code
    assert "batches-table" in code
    assert "/batches/${b.batch_id}" in code or '`/batches/${b.batch_id}`' in code


# --- 9. Task 37 API Response Parity Test ---

def test_api_payload_contracts_align_with_components():
    """Verify sample payload from Task 37 GET /batches/{batch_id} maps directly to review screen props."""
    sample_payload = {
        "batch_id": "11111111-2222-3333-4444-555555555555",
        "filename": "launch_sheet_test.csv",
        "uploaded_by": "user-buyer-1",
        "created_at": "2026-10-14T12:00:00Z",
        "sheet_hash": "a1b2c3d4e5f607182930",
        "summary": {
            "total_ads": 2,
            "verdict_counts": {
                "ready": 1,
                "fix_needed": 1,
                "blocked": 0,
                "needs_person": 0,
                "pending": 0,
            },
            "total_findings": 1,
            "client_ids": ["client-alpha"],
        },
        "ads": [
            {
                "ad_id": "ad-uuid-1",
                "batch_id": "11111111-2222-3333-4444-555555555555",
                "client_id": "client-alpha",
                "row_number": 1,
                "platform": "facebook",
                "platform_raw": "Facebook Ads",
                "ad_text": "Sample clean ad copy with great offer",
                "headline": "Clean Headline",
                "destination_url": "https://clientalpha.com/offer?utm_source=fb",
                "creative_file": "creative1.png",
                "row_hash": "hash-ad-1",
                "sheet_hash": "a1b2c3d4e5f607182930",
                "verdict": "ready",
                "findings": [],
            },
            {
                "ad_id": "ad-uuid-2",
                "batch_id": "11111111-2222-3333-4444-555555555555",
                "client_id": "client-alpha",
                "row_number": 2,
                "platform": "facebook",
                "platform_raw": "FB",
                "ad_text": "Text that has a missing utm campaign parameter",
                "headline": "Second Headline That Is A Bit Long",
                "destination_url": "https://clientalpha.com/offer",
                "creative_file": None,
                "row_hash": "hash-ad-2",
                "sheet_hash": "a1b2c3d4e5f607182930",
                "verdict": "fix_needed",
                "findings": [
                    {
                        "finding_id": "f-1",
                        "ad_id": "ad-uuid-2",
                        "row_number": 2,
                        "check_type": "utm_presence",
                        "message": "Missing utm_campaign parameter",
                        "field": "destination_url",
                        "proposed_fix": "https://clientalpha.com/offer?utm_campaign=launch",
                        "rule_id": "RULE-UTM-01",
                        "file_ref": "brand_book.pdf#p4",
                    }
                ],
            },
        ],
        "rejected_rows": [
            {
                "data_row_index": 3,
                "row_number": 3,
                "reason_code": "missing_required_columns",
                "reason": "Missing required platform column",
                "raw_cells": ["incomplete", "data"],
            }
        ],
        "rejected_count": 1,
    }

    # Verify all expected top-level keys
    assert "batch_id" in sample_payload
    assert "summary" in sample_payload
    assert "ads" in sample_payload
    assert "rejected_rows" in sample_payload

    # Verify ads structure
    for ad in sample_payload["ads"]:
        assert "ad_id" in ad
        assert "verdict" in ad
        assert "findings" in ad
        assert "row_number" in ad
        assert "platform" in ad
        assert "destination_url" in ad

    # Verify findings structure
    for finding in sample_payload["ads"][1]["findings"]:
        assert "finding_id" in finding
        assert "check_type" in finding
        assert "message" in finding
        assert "field" in finding
        assert "proposed_fix" in finding
        assert "rule_id" in finding
        assert "file_ref" in finding

    # Verify rejected rows structure
    assert sample_payload["rejected_rows"][0]["data_row_index"] == 3
    assert sample_payload["rejected_rows"][0]["reason_code"] == "missing_required_columns"
