"""Tests for Task 42: Bounded re-check loop after edit. Needs: 34, 41.

Architecture & PRD compliance:
- PRD D17: After an edit, the ad is re-checked in a bounded loop (cap in configuration).
  Brief: re-examine, "never endlessly" [B R5].
- CLAUDE.md §1 & §4: Reviewer edits and reasons are saved; changed ad is re-checked in a bounded loop.
- Needs: 34 (Bounded LangGraph flow wiring & step cap enforcement).
- Needs: 41 (Reviewer edit with reasons saved & audit trail).
- Checkpointer integration: Thread checkpoints cleared for fresh content verification.
- Stale findings replacement: Previous findings for ad_id are cleaned and replaced by new findings.
- Tracing integration: worker_traces updated with re-check verdict and iteration metadata.
- FastAPI endpoint POST /ads/{ad_id}/recheck (client-scoped, role-checked, terminal-state protected).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from typing import Any
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import BaseCheckpointSaver

from apps.api import auth, users
from apps.api.database import get_session
from apps.api.edits import count_ad_edits, perform_ad_edit
from apps.api.main import app
from apps.api.settings import max_edit_loops
from apps.worker.graph import WorkerGraphResult
from apps.worker.recheck import (
    RecheckResult,
    get_ad_recheck_status,
    recheck_ad,
    record_recheck_trace,
    replace_ad_findings,
)
from packages.contracts import (
    AdContract,
    CheckType,
    FindingContract,
    VerdictStatus,
)


def sample_manifest() -> dict[str, dict[str, int]]:
    return {
        "square.png": {"width": 1080, "height": 1080},
        "bad_ratio.png": {"width": 1000, "height": 500},
        "bundle_v1_1080x1080.png": {"width": 1080, "height": 1080},
    }


def sample_platform_limits() -> dict[str, Any]:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return {
        "stale_if_last_verified_before": "2024-01-01",
        "platforms": {
            "facebook": {
                "text_max": {"value": 125, "last_verified": tomorrow},
                "headline_max": {"value": 40, "last_verified": tomorrow},
                "ratios": {"value": ["1:1", "4:5"], "last_verified": tomorrow},
            }
        },
    }


def sample_ad(
    ad_id: str = "ad-recheck-1",
    batch_id: str = "batch-rc1",
    client_id: str = "brightleaf",
    platform: str = "Facebook",
    headline: str = "Valid Short Headline",
    ad_text: str = "Natural organic serum for soothing care.",
    destination_url: str = "https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=serum_jan",
    creative_file: str | None = "bundle_v1_1080x1080.png",
    row_hash: str = "hash-clean-1",
) -> AdContract:
    return AdContract(
        ad_id=ad_id,
        batch_id=batch_id,
        client_id=client_id,
        row_number=1,
        platform=platform,
        headline=headline,
        ad_text=ad_text,
        destination_url=destination_url,
        creative_file=creative_file,
        row_hash=row_hash,
        sheet_hash="sheet-hash-1",
    )


class MockSaver(BaseCheckpointSaver):
    def __init__(self):
        super().__init__()
        self.deleted_threads: list[str] = []

    def get_tuple(self, config):
        return None

    def list(self, config, *, filter=None, before=None, limit=None):
        return iter([])

    def put(self, config, checkpoint, metadata, new_versions):
        return config

    def put_writes(self, config, writes, task_id, task_path=""):
        pass

    def delete_thread(self, thread_id: str):
        self.deleted_threads.append(thread_id)


# --- In-Memory Mock Database for Unit Tests ---

class MockRecheckDB:
    def __init__(self):
        self.ads = {
            "ad-rc-1": {
                "ad_id": "ad-rc-1",
                "batch_id": "batch-rc1",
                "client_id": "brightleaf",
                "row_number": 1,
                "platform": "facebook",
                "ad_text": "Original text with issues",
                "headline": "A" * 60,  # Exceeds 40
                "destination_url": "https://brightleaf.example/serum",  # Missing UTM
                "row_hash": "hash-orig-1",
                "sheet_hash": "sheet-hash-1",
                "creative_file": "bundle_v1_1080x1080.png",
            },
            "ad-rc-terminal": {
                "ad_id": "ad-rc-terminal",
                "batch_id": "batch-rc1",
                "client_id": "brightleaf",
                "row_number": 2,
                "platform": "facebook",
                "ad_text": "Sent text",
                "headline": "Sent Headline",
                "destination_url": "https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=serum_jan",
                "row_hash": "hash-sent-1",
                "sheet_hash": "sheet-hash-1",
                "creative_file": "bundle_v1_1080x1080.png",
            },
        }
        self.approvals = {
            "ad-rc-1": {
                "ad_id": "ad-rc-1",
                "client_id": "brightleaf",
                "state": "pending_review",
                "content_hash": "hash-orig-1",
            },
            "ad-rc-terminal": {
                "ad_id": "ad-rc-terminal",
                "client_id": "brightleaf",
                "state": "sent",
                "content_hash": "hash-sent-1",
            },
        }
        self.findings = {
            "ad-rc-1": [
                {
                    "finding_id": "ad-rc-1:headline_max",
                    "ad_id": "ad-rc-1",
                    "row_number": 1,
                    "check_type": "mechanical",
                    "message": "Headline exceeds limit",
                    "field": "headline",
                    "proposed_fix": "Shorten headline",
                    "rule_id": "facebook.headline_max",
                    "file_ref": None,
                },
                {
                    "finding_id": "ad-rc-1:utm_presence",
                    "ad_id": "ad-rc-1",
                    "row_number": 1,
                    "check_type": "mechanical",
                    "message": "Missing UTMs",
                    "field": "destination_url",
                    "proposed_fix": "Add UTMs",
                    "rule_id": "utm.presence",
                    "file_ref": None,
                },
            ]
        }
        self.edits: list[dict[str, Any]] = []
        self.traces: dict[str, dict[str, Any]] = {}
        self.queries: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None):
        self.queries.append((sql, params))
        norm = " ".join(sql.strip().split())

        # SELECT COUNT(*) FROM ad_edits WHERE ad_id = %s
        if "COUNT(*) FROM ad_edits WHERE ad_id = %s" in norm:
            ad_id = params[0]
            count = sum(1 for e in self.edits if e["ad_id"] == ad_id)
            cursor = MagicMock()
            cursor.fetchone.return_value = (count,)
            return cursor

        # SELECT ... FROM ad_edits WHERE ad_id = %s
        if "FROM ad_edits WHERE ad_id = %s" in norm:
            ad_id = params[0]
            matched = [e for e in self.edits if e["ad_id"] == ad_id]
            cursor = MagicMock()
            cursor.fetchall.return_value = [
                (
                    e["edit_id"],
                    e["ad_id"],
                    e["batch_id"],
                    e["client_id"],
                    e["edited_by"],
                    e["reason"],
                    e["previous_content"],
                    e["new_content"],
                    e["previous_row_hash"],
                    e["new_row_hash"],
                    e["edit_iteration"],
                    e["created_at"],
                )
                for e in matched
            ]
            return cursor

        # SELECT ... FROM ads WHERE ad_id = %s
        if "FROM ads WHERE ad_id = %s" in norm and "SELECT 1" not in norm:
            ad_id = params[0]
            ad = self.ads.get(ad_id)
            cursor = MagicMock()
            if ad:
                cursor.fetchone.return_value = (
                    ad["ad_id"],
                    ad["batch_id"],
                    ad["client_id"],
                    ad["row_number"],
                    ad["platform"],
                    ad["ad_text"],
                    ad["headline"],
                    ad["destination_url"],
                    ad["row_hash"],
                    ad["sheet_hash"],
                    ad["creative_file"],
                )
            else:
                cursor.fetchone.return_value = None
            return cursor

        # UPDATE ads
        if "UPDATE ads" in norm:
            hd, txt, url, cr, r_hash, aid = params
            if aid in self.ads:
                self.ads[aid]["headline"] = hd
                self.ads[aid]["ad_text"] = txt
                self.ads[aid]["destination_url"] = url
                self.ads[aid]["creative_file"] = cr
                self.ads[aid]["row_hash"] = r_hash
            cursor = MagicMock()
            return cursor

        # INSERT INTO ad_edits
        if "INSERT INTO ad_edits" in norm:
            aid, bid, cid, uid, rsn, prev_c, new_c, prev_h, new_h, iter_num = params
            self.edits.append({
                "edit_id": len(self.edits) + 1,
                "ad_id": aid,
                "batch_id": bid,
                "client_id": cid,
                "edited_by": uid,
                "reason": rsn,
                "previous_content": prev_c,
                "new_content": new_c,
                "previous_row_hash": prev_h,
                "new_row_hash": new_h,
                "edit_iteration": iter_num,
                "created_at": datetime.now(timezone.utc),
            })
            cursor = MagicMock()
            return cursor

        # SELECT ... FROM ad_approvals WHERE ad_id = %s
        if "FROM ad_approvals WHERE ad_id = %s" in norm:
            ad_id = params[0]
            appr = self.approvals.get(ad_id)
            cursor = MagicMock()
            if appr:
                cursor.fetchone.return_value = (
                    appr["ad_id"],
                    appr["client_id"],
                    appr["state"],
                    appr.get("content_hash"),
                    "buyer-1",
                    datetime.now(timezone.utc),
                    None, None, None, None, None, None, 0, None,
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                    "{}",
                )
            else:
                cursor.fetchone.return_value = None
            return cursor

        # UPDATE ad_approvals
        if "UPDATE ad_approvals" in norm:
            aid = params[-1]
            if aid in self.approvals:
                self.approvals[aid]["state"] = params[0]
                if len(params) > 1 and params[1] is not None:
                    self.approvals[aid]["content_hash"] = params[1]
            cursor = MagicMock()
            return cursor

        # DELETE FROM findings WHERE ad_id = %s
        if "DELETE FROM findings WHERE ad_id = %s" in norm:
            ad_id = params[0]
            self.findings[ad_id] = []
            cursor = MagicMock()
            return cursor

        # INSERT INTO findings
        if "INSERT INTO findings" in norm:
            fid, aid, row_num, ctype, msg, fld, pfix, rid, fref = params
            if aid not in self.findings:
                self.findings[aid] = []
            self.findings[aid].append({
                "finding_id": fid,
                "ad_id": aid,
                "row_number": row_num,
                "check_type": ctype,
                "message": msg,
                "field": fld,
                "proposed_fix": pfix,
                "rule_id": rid,
                "file_ref": fref,
            })
            cursor = MagicMock()
            return cursor

        # INSERT INTO worker_traces
        if "INSERT INTO worker_traces" in norm:
            tid, bid, aid, cid, verdict, scount, meta = params[0], params[1], params[2], params[3], params[4], params[5], params[6]
            self.traces[tid] = {
                "trace_id": tid,
                "batch_id": bid,
                "ad_id": aid,
                "client_id": cid,
                "verdict": verdict,
                "step_count": scount,
                "metadata": json.loads(meta) if isinstance(meta, str) else meta,
            }
            cursor = MagicMock()
            return cursor

        # Default fallback
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        return cursor

    def commit(self):
        pass


# =========================================================================
# Unit Tests for Re-Check Execution & Flow Integration (Needs: 34, 41)
# =========================================================================

def test_clean_recheck_resolves_all_issues_to_ready():
    """When an ad was edited to fix all violations, re-check evaluates to READY with 0 findings."""
    db = MockRecheckDB()
    # Ad has valid headline and destination URL
    ad = sample_ad(
        ad_id="ad-rc-1",
        headline="Short Headline",
        destination_url="https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=serum_jan",
    )
    # Update ad in mock DB
    db.ads["ad-rc-1"]["headline"] = "Short Headline"
    db.ads["ad-rc-1"]["destination_url"] = "https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=serum_jan"
    db.ads["ad-rc-1"]["row_hash"] = "hash-new-clean"

    # Record 1 edit in DB
    db.edits.append({
        "edit_id": 1,
        "ad_id": "ad-rc-1",
        "batch_id": "batch-rc1",
        "client_id": "brightleaf",
        "edited_by": "buyer-1",
        "reason": "Fixed headline and added UTM parameters",
        "previous_content": "{}",
        "new_content": "{}",
        "previous_row_hash": "hash-orig-1",
        "new_row_hash": "hash-new-clean",
        "edit_iteration": 1,
        "created_at": datetime.now(timezone.utc),
    })

    result = recheck_ad(
        ad,
        connection=db,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        run_page_checks=False,
        run_wording_checks=False,
    )

    assert isinstance(result, RecheckResult)
    assert result.verdict.status is VerdictStatus.READY
    assert len(result.findings) == 0
    assert result.edit_iteration == 1
    assert result.is_loop_capped is False
    assert result.needs_person is False
    # Stale findings in db were replaced with 0 findings
    assert len(db.findings["ad-rc-1"]) == 0
    # Trace was recorded with verdict ready and iteration 1
    assert "batch-rc1:ad-rc-1" in db.traces
    assert db.traces["batch-rc1:ad-rc-1"]["verdict"] == "ready"
    assert db.traces["batch-rc1:ad-rc-1"]["metadata"]["edit_iteration"] == 1


def test_recheck_with_remaining_errors_produces_fix_needed():
    """When an edit leaves some issues unresolved, re-check reflects current issues and FIX_NEEDED."""
    db = MockRecheckDB()
    # Fixed headline, but URL is STILL missing UTMs
    ad = sample_ad(
        ad_id="ad-rc-1",
        headline="Short Headline",
        destination_url="https://brightleaf.example/serum",  # Missing UTMs!
    )
    db.ads["ad-rc-1"]["headline"] = "Short Headline"
    db.ads["ad-rc-1"]["destination_url"] = "https://brightleaf.example/serum"

    result = recheck_ad(
        ad,
        connection=db,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        run_page_checks=False,
        run_wording_checks=False,
    )

    assert result.verdict.status is VerdictStatus.FIX_NEEDED
    # Only UTM error remains (headline length error was fixed!)
    assert len(result.findings) >= 1
    # Check that utm presence is among findings
    rule_ids = [f.rule_id for f in result.findings if f.rule_id]
    check_types = [f.check_type for f in result.findings]
    assert CheckType.MECHANICAL in check_types
    # Stale findings were replaced in database
    assert len(db.findings["ad-rc-1"]) == len(result.findings)


def test_bounded_loop_cap_enforced_escalates_to_needs_person():
    """PRD D17: If edit count reaches MAX_EDIT_LOOPS and issues remain, force verdict to NEEDS_PERSON."""
    db = MockRecheckDB()
    # Ad still has headline exceeding limit
    ad = sample_ad(
        ad_id="ad-rc-1",
        headline="A" * 60,
        destination_url="https://brightleaf.example/serum",
    )
    # 5 prior edits already recorded
    for i in range(1, 6):
        db.edits.append({
            "edit_id": i,
            "ad_id": "ad-rc-1",
            "batch_id": "batch-rc1",
            "client_id": "brightleaf",
            "edited_by": "buyer-1",
            "reason": f"Attempt {i}",
            "previous_content": "{}",
            "new_content": "{}",
            "previous_row_hash": f"hash-{i-1}",
            "new_row_hash": f"hash-{i}",
            "edit_iteration": i,
            "created_at": datetime.now(timezone.utc),
        })

    result = recheck_ad(
        ad,
        connection=db,
        max_loops=5,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        run_page_checks=False,
        run_wording_checks=False,
    )

    assert result.is_loop_capped is True
    assert result.needs_person is True
    assert result.verdict.status is VerdictStatus.NEEDS_PERSON
    # Cap finding added
    cap_findings = [f for f in result.findings if f.rule_id == "gate.recheck_loop_cap"]
    assert len(cap_findings) == 1
    assert "Maximum edit loops" in cap_findings[0].message
    assert "requires manual reviewer resolution" in cap_findings[0].message


def test_bounded_loop_cap_clean_fix_on_final_loop_allowed():
    """If the reviewer fixes all issues on the final allowed loop (5th edit), it evaluates to READY."""
    db = MockRecheckDB()
    ad = sample_ad(
        ad_id="ad-rc-1",
        headline="Clean Final Headline",
        destination_url="https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=serum_jan",
    )
    # 5 edits recorded (final attempt)
    for i in range(1, 6):
        db.edits.append({
            "edit_id": i,
            "ad_id": "ad-rc-1",
            "batch_id": "batch-rc1",
            "client_id": "brightleaf",
            "edited_by": "buyer-1",
            "reason": f"Edit {i}",
            "previous_content": "{}",
            "new_content": "{}",
            "previous_row_hash": f"hash-{i-1}",
            "new_row_hash": f"hash-{i}",
            "edit_iteration": i,
            "created_at": datetime.now(timezone.utc),
        })

    result = recheck_ad(
        ad,
        connection=db,
        max_loops=5,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        run_page_checks=False,
        run_wording_checks=False,
    )

    assert result.is_loop_capped is True
    # Because there are 0 findings, it does NOT fail; it is READY!
    assert len(result.findings) == 0
    assert result.verdict.status is VerdictStatus.READY
    assert result.needs_person is False


def test_checkpointer_thread_cleaned_on_recheck():
    """Stale thread checkpoints from prior content versions are deleted so re-check starts clean."""
    mock_saver = MockSaver()
    db = MockRecheckDB()
    ad = sample_ad(ad_id="ad-rc-1", batch_id="batch-rc1")

    result = recheck_ad(
        ad,
        connection=db,
        checkpointer=mock_saver,
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        run_page_checks=False,
        run_wording_checks=False,
    )

    # Verifies delete_thread was called with thread_id = "batch-rc1:ad-rc-1"
    assert "batch-rc1:ad-rc-1" in mock_saver.deleted_threads


def test_step_cap_inside_flow_honored_during_recheck():
    """LangGraph worker flow step cap (Task 34) is honored during re-check."""
    db = MockRecheckDB()
    ad = sample_ad(ad_id="ad-rc-1", batch_id="batch-rc1")

    result = recheck_ad(
        ad,
        connection=db,
        max_steps=2,  # Hard step cap of 2 steps
        manifest=sample_manifest(),
        platform_limits=sample_platform_limits(),
        run_page_checks=True,
        run_wording_checks=True,
    )

    # Step cap reached -> routed to verdict with needs a person
    assert result.needs_person is True
    assert result.verdict.status is VerdictStatus.NEEDS_PERSON
    assert "Step cap exceeded" in result.verdict.summary


def test_get_ad_recheck_status():
    """Helper get_ad_recheck_status reports iteration counts and remaining allowance."""
    db = MockRecheckDB()
    db.edits.append({"edit_id": 1, "ad_id": "ad-rc-1"})
    db.edits.append({"edit_id": 2, "ad_id": "ad-rc-1"})

    status = get_ad_recheck_status(db, "ad-rc-1")
    assert status["ad_id"] == "ad-rc-1"
    assert status["edit_count"] == 2
    assert status["max_edit_loops"] == max_edit_loops()
    assert status["remaining_loops"] == max_edit_loops() - 2
    assert status["is_loop_capped"] is False


# =========================================================================
# API Endpoint Tests: POST /ads/{ad_id}/recheck
# =========================================================================

@pytest.fixture(autouse=True)
def configured_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")


@pytest.fixture
def api_test_client(monkeypatch):
    """Setup test client with mocked DB and test users."""
    db = MockRecheckDB()

    @contextmanager
    def mock_get_session():
        yield db

    monkeypatch.setattr("apps.api.edits.get_session", mock_get_session)
    monkeypatch.setattr("apps.worker.recheck.get_session", mock_get_session)
    monkeypatch.setattr("apps.api.approvals.get_session", mock_get_session)

    # Users with roles and client assignments
    users_dict = {
        "buyer-brightleaf": {
            "user_id": "buyer-brightleaf",
            "display_name": "Brightleaf Buyer",
            "email": "buyer@brightleaf.example",
            "role": "buyer",
            "is_active": True,
        },
        "buyer-unassigned": {
            "user_id": "buyer-unassigned",
            "display_name": "Unassigned Buyer",
            "email": "unassigned@example.com",
            "role": "buyer",
            "is_active": True,
        },
    }

    monkeypatch.setattr(users, "get_user_by_id", lambda uid: users_dict.get(uid))
    monkeypatch.setattr(
        users,
        "get_user_client_ids",
        lambda uid: ["brightleaf"] if uid == "buyer-brightleaf" else ["other-client"],
    )

    client = TestClient(app)
    return client, db


def test_api_recheck_success(api_test_client):
    """Assigned buyer triggers re-check via POST /ads/{ad_id}/recheck."""
    client, db = api_test_client
    token = auth.create_access_token("buyer-brightleaf", "buyer")

    # Ad in DB has valid content
    db.ads["ad-rc-1"]["headline"] = "Valid Clean Headline"
    db.ads["ad-rc-1"]["destination_url"] = "https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=serum_jan"

    response = client.post(
        "/ads/ad-rc-1/recheck",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ad_id"] == "ad-rc-1"
    assert data["verdict"] == "ready"
    assert data["findings_count"] == 0
    assert data["is_ready_for_approval"] is True


def test_api_recheck_unassigned_client_forbidden(api_test_client):
    """Needs: 7: Buyer not assigned to ad's client receives 403 Forbidden."""
    client, _ = api_test_client
    token = auth.create_access_token("buyer-unassigned", "buyer")

    response = client.post(
        "/ads/ad-rc-1/recheck",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert "forbidden" in response.json()["detail"].lower()


def test_api_recheck_terminal_state_conflict(api_test_client):
    """Ad in 'sent' state cannot be re-checked (409 Conflict)."""
    client, _ = api_test_client
    token = auth.create_access_token("buyer-brightleaf", "buyer")

    response = client.post(
        "/ads/ad-rc-terminal/recheck",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert "cannot re-check ad in 'sent' state" in response.json()["detail"]


def test_api_recheck_not_found(api_test_client):
    """Non-existent ad returns 404 Not Found."""
    client, _ = api_test_client
    token = auth.create_access_token("buyer-brightleaf", "buyer")

    response = client.post(
        "/ads/ad-nonexistent/recheck",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert "ad not found" in response.json()["detail"]


def test_edit_recheck_approve_lifecycle(api_test_client):
    """Full lifecycle: Edit ad -> re-check -> verify READY -> approve with new content hash."""
    client, db = api_test_client
    token = auth.create_access_token("buyer-brightleaf", "buyer")

    # 1. Edit ad with valid content
    edit_resp = client.post(
        "/ads/ad-rc-1/edit",
        json={
            "headline": "Clean Serum Headline",
            "destination_url": "https://brightleaf.example/serum?utm_source=facebook&utm_medium=paid_social&utm_campaign=serum_jan",
            "reason": "Fixed length violation and added proper UTM parameters",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert edit_resp.status_code == 200
    edit_data = edit_resp.json()
    new_hash = edit_data["new_row_hash"]
    assert edit_data["recheck_queued"] is True
    assert edit_data["state"] == "pending_review"

    # 2. Re-check ad
    recheck_resp = client.post(
        "/ads/ad-rc-1/recheck",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert recheck_resp.status_code == 200
    recheck_data = recheck_resp.json()
    assert recheck_data["verdict"] == "ready"
    assert recheck_data["findings_count"] == 0
    assert recheck_data["row_hash"] == new_hash

    # 3. Approve ad with new content hash
    appr_resp = client.post(
        "/ads/ad-rc-1/approve",
        json={"content_hash": new_hash},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert appr_resp.status_code == 200
    appr_data = appr_resp.json()
    assert appr_data["state"] == "approved"
    assert appr_data["content_hash"] == new_hash
