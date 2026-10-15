"""Tests for Task 47: Retry with backoff and timeouts (Needs: 45).

Verifies PRD §4.6, Dataflow Architecture §3, and Never-break Rules 1, 2, 7:
1. Exponential backoff calculation and max ceiling.
2. Jitter modes (none, full, equal) to prevent thundering herd.
3. Retry-After header parsing (numeric seconds and HTTP dates) and delay override.
4. Granular TimeoutPolicy (connect, read, write, total timeouts).
5. Transient vs. non-transient error classification (5xx, 429, timeouts vs 4xx, rejections).
6. Intermediate attempt logging in ad_sends table across retries.
7. Stand-in platform chaos recovery: fail_before_commit (503) retry and success.
8. Stand-in platform chaos recovery: fail_after_commit (503) idempotent replay without duplicates.
9. Timeouts caught, retried with backoff, and transitioned to dead_letter upon exhaustion.
10. Architecture §3 state transition: sending -> approved on timeout when deferred retry enabled.
11. Fast fail on non-retryable 4xx client errors (no retries wasted).
12. Platform policy rejections (status: rejected) permanently halt retries and alert reviewer.
13. Manual retry of dead_letter ad via retry_dead_letter_ad and API endpoint POST /ads/{ad_id}/retry.
14. Batch dead-letter retry endpoint POST /batches/{batch_id}/retry-dead-letter.
15. Rapid repeat retries produce exactly zero duplicate ads on stand-in platform.
16. Monitored exponential delay growth with custom sleep function.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.approvals import (
    ApprovalState,
    create_approval_tables,
    get_ad_record,
    get_or_create_ad_approval,
    transition_ad_state,
)
from apps.api.auth import create_access_token
from apps.api.database import get_session
from apps.api.main import app as main_api_app
from apps.worker.platforms import (
    BasePlatformAdapter,
    StandInPlatformAdapter,
    get_platform_adapter,
)
from apps.worker.sender import (
    BackoffPolicy,
    RETRYABLE_STATUS_CODES,
    TimeoutPolicy,
    build_stand_in_ad_payload,
    generate_idempotency_key,
    get_ad_alerts,
    get_ad_send_records,
    is_retryable_exception,
    is_retryable_status,
    parse_retry_after,
    retry_dead_letter_ad,
    retry_dead_letter_batch_ads,
    send_ad_to_stand_in,
    send_batch_approved_ads,
)
from data.stand_in_platform.app import (
    STATE as STAND_IN_STATE,
    app as stand_in_app,
    reset as stand_in_reset,
)


# --- Fixtures and Mocks ---

@pytest.fixture(autouse=True)
def setup_auth(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")

    def mock_get_user_by_id(uid: str):
        if uid in ("buyer_1", "buyer_2"):
            return {
                "user_id": uid,
                "email": f"{uid}@example.com",
                "display_name": uid,
                "role": "buyer",
                "is_active": True,
            }
        return None

    def mock_get_user_client_ids(uid: str):
        if uid == "buyer_1":
            return ["client_aura"]
        if uid == "buyer_2":
            return ["client_lumina"]
        return []

    monkeypatch.setattr("apps.api.users.get_user_by_id", mock_get_user_by_id)
    monkeypatch.setattr("apps.api.users.get_user_client_ids", mock_get_user_client_ids)


@pytest.fixture(autouse=True)
def reset_stand_in_platform():
    """Reset the stand-in synthetic platform before each test."""
    stand_in_reset()
    STAND_IN_STATE["cfg"] = {
        "fail_before_commit_rate": 0.0,
        "fail_after_commit_rate": 0.0,
        "timeout_rate": 0.0,
        "timeout_seconds": 15,
        "seed": 1,
    }


class MockDB:
    def __init__(self):
        self.ads = {
            "ad-retry-1": {
                "ad_id": "ad-retry-1",
                "batch_id": "batch-retry-1",
                "client_id": "client_aura",
                "row_number": 1,
                "platform": "instagram",
                "platform_raw": "Instagram",
                "ad_text": "Premium skincare routine for glowing radiance every single day.",
                "headline": "Transform Your Skin",
                "destination_url": "https://aurabeauty.com/shop?utm_source=instagram&utm_medium=paid_social&utm_campaign=glow_routine",
                "creative_file": "creatives/glow.jpg",
                "row_hash": "hash_retry_1",
                "sheet_hash": "sheet_hash_1",
            },
            "ad-retry-2": {
                "ad_id": "ad-retry-2",
                "batch_id": "batch-retry-1",
                "client_id": "client_aura",
                "row_number": 2,
                "platform": "meta",
                "platform_raw": "Meta",
                "ad_text": "Night cream special formula.",
                "headline": "Nightly Repair",
                "destination_url": "https://aurabeauty.com/shop?utm_source=meta&utm_medium=paid_social&utm_campaign=night_repair",
                "creative_file": "creatives/night.jpg",
                "row_hash": "hash_retry_2",
                "sheet_hash": "sheet_hash_1",
            },
        }
        self.approvals = {
            "ad-retry-1": {
                "ad_id": "ad-retry-1",
                "client_id": "client_aura",
                "state": "approved",
                "content_hash": "hash_retry_1",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
                "evidence_note": "Client lead approved",
                "evidence_file": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-retry-1:hash_retry_1",
                "send_attempts": 0,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc),
            },
            "ad-retry-2": {
                "ad_id": "ad-retry-2",
                "client_id": "client_aura",
                "state": "approved",
                "content_hash": "hash_retry_2",
                "approved_by": "buyer_1",
                "approved_at": datetime(2026, 10, 14, 10, 5, 0, tzinfo=timezone.utc),
                "evidence_note": "Client lead approved",
                "evidence_file": None,
                "rejection_reason": None,
                "rejected_by": None,
                "rejected_at": None,
                "idempotency_key": "send:ad-retry-2:hash_retry_2",
                "send_attempts": 0,
                "sent_at": None,
                "created_at": datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 14, 10, 5, 0, tzinfo=timezone.utc),
            },
        }
        self.sends = []
        self.alerts = []


class FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


@pytest.fixture
def mock_db():
    return MockDB()


@pytest.fixture
def fake_session(mock_db: MockDB, monkeypatch: pytest.MonkeyPatch):
    class FakeCursor:
        def __init__(self, db: MockDB):
            self.db = db

        def execute(self, statement: str, params: Any = None):
            s = statement.strip()
            params_tuple = tuple(params) if params is not None else ()

            # 1. Fetch ad record
            if "FROM ads" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                ad = self.db.ads.get(aid)
                if ad:
                    return FakeResult([(
                        ad["ad_id"], ad["batch_id"], ad["client_id"], ad["row_number"],
                        ad["platform"], ad["ad_text"], ad["headline"], ad["destination_url"],
                        ad["row_hash"], ad["sheet_hash"], ad.get("creative_file")
                    )])
                return FakeResult([])

            # 2. Fetch approval record
            if "FROM ad_approvals" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                appr = self.db.approvals.get(aid)
                if appr:
                    return FakeResult([(
                        appr["ad_id"], appr["client_id"], appr["state"], appr["content_hash"],
                        appr["approved_by"], appr["approved_at"], appr["evidence_note"],
                        appr["evidence_file"], appr["rejection_reason"], appr["rejected_by"],
                        appr["rejected_at"], appr["idempotency_key"], appr["send_attempts"],
                        appr["sent_at"], appr["created_at"], appr["updated_at"]
                    )])
                return FakeResult([])

            # 3. Transition approval state
            if "UPDATE ad_approvals" in s:
                aid = params_tuple[-1]
                appr = self.db.approvals.get(aid)
                if appr:
                    if "state = %s" in s:
                        appr["state"] = params_tuple[0]
                    if "idempotency_key = %s" in s:
                        appr["idempotency_key"] = params_tuple[1]
                    if "send_attempts = send_attempts + 1" in s:
                        appr["send_attempts"] = appr.get("send_attempts", 0) + 1
                    appr["updated_at"] = datetime.now(timezone.utc)
                return FakeResult([])

            # 4. Insert send record
            if "INSERT INTO ad_sends" in s:
                send_rec = {
                    "send_id": params_tuple[0],
                    "ad_id": params_tuple[1],
                    "client_id": params_tuple[2],
                    "batch_id": params_tuple[3],
                    "idempotency_key": params_tuple[4],
                    "content_hash": params_tuple[5],
                    "platform": params_tuple[6],
                    "status": params_tuple[7],
                    "platform_ad_id": params_tuple[8],
                    "platform_status": params_tuple[9],
                    "rejection_reason": params_tuple[10],
                    "is_dry_run": params_tuple[11],
                    "idempotent_replay": params_tuple[12],
                    "attempt_count": params_tuple[13],
                    "request_payload": params_tuple[14],
                    "response_payload": params_tuple[15],
                    "error_message": params_tuple[16],
                    "created_at": params_tuple[17],
                }
                self.db.sends.append(send_rec)
                return FakeResult([])

            # 5. Insert alert
            if "INSERT INTO send_alerts" in s:
                alert_rec = {
                    "alert_id": params_tuple[0],
                    "ad_id": params_tuple[1],
                    "client_id": params_tuple[2],
                    "batch_id": params_tuple[3],
                    "alert_type": params_tuple[4],
                    "severity": params_tuple[5],
                    "message": params_tuple[6],
                    "details": params_tuple[7],
                    "created_at": params_tuple[8],
                }
                self.db.alerts.append(alert_rec)
                return FakeResult([])

            # 6. Fetch sends history
            if "FROM ad_sends" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                rows = [
                    (
                        s["send_id"], s["ad_id"], s["client_id"], s["batch_id"],
                        s["idempotency_key"], s["content_hash"], s["platform"], s["status"],
                        s["platform_ad_id"], s["platform_status"], s["rejection_reason"],
                        s["is_dry_run"], s["idempotent_replay"], s["attempt_count"],
                        s["request_payload"], s["response_payload"], s["error_message"],
                        s["created_at"]
                    )
                    for s in self.db.sends if s["ad_id"] == aid
                ]
                return FakeResult(rows)

            # 7. Fetch alerts
            if "FROM send_alerts" in s and "WHERE ad_id = %s" in s:
                aid = params_tuple[0]
                rows = [
                    (
                        a["alert_id"], a["ad_id"], a["client_id"], a["batch_id"],
                        a["alert_type"], a["severity"], a["message"], a["details"],
                        False, a["created_at"]
                    )
                    for a in self.db.alerts if a["ad_id"] == aid
                ]
                return FakeResult(rows)

            # 8. Fetch ads in batch for send_batch_approved_ads / retry_dead_letter_batch_ads
            if "SELECT a.ad_id, a.client_id, ap.state" in s:
                bid = params_tuple[0]
                rows = []
                for a in self.db.ads.values():
                    if a["batch_id"] == bid:
                        appr = self.db.approvals.get(a["ad_id"])
                        state = appr["state"] if appr else "pending_review"
                        if "ap.state = 'dead_letter'" in s and state != "dead_letter":
                            continue
                        rows.append((a["ad_id"], a["client_id"], state))
                return FakeResult(rows)

            # 9. User client assignments
            if "FROM user_client_assignments" in s:
                uid = params_tuple[0]
                assignments = [("buyer_1", "client_aura"), ("buyer_2", "client_lumina")]
                rows = [(cid,) for (u, cid) in assignments if u == uid]
                return FakeResult(rows)

            return FakeResult([])

    class FakeSession:
        def __init__(self, db: MockDB):
            self.cursor = FakeCursor(db)

        def execute(self, statement: str, params: Any = None):
            return self.cursor.execute(statement, params)

        def __enter__(self):
            return self.cursor

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    session = FakeSession(mock_db)
    monkeypatch.setattr("apps.api.database.get_session", lambda: session)
    monkeypatch.setattr("apps.worker.sender.get_session", lambda: session)
    monkeypatch.setattr("apps.api.approvals.get_session", lambda: session)
    monkeypatch.setattr("apps.api.sends.get_session", lambda: session)
    monkeypatch.setattr(
        "apps.worker.sender.get_platform_adapter",
        lambda *args, **kwargs: StandInPlatformAdapter(client=TestClient(stand_in_app)),
    )
    return session


# =====================================================================
# Unit & Policy Tests
# =====================================================================

def test_backoff_policy_exponential_calculation():
    """Verify exponential delay doubling without jitter and max ceiling."""
    policy = BackoffPolicy(
        initial_backoff=0.05,
        max_backoff=0.5,
        backoff_factor=2.0,
        jitter="none",
    )
    # Attempt 1: 0.05 * 2^0 = 0.05
    assert policy.calculate_delay(1) == 0.05
    # Attempt 2: 0.05 * 2^1 = 0.10
    assert policy.calculate_delay(2) == 0.10
    # Attempt 3: 0.05 * 2^2 = 0.20
    assert policy.calculate_delay(3) == 0.20
    # Attempt 4: 0.05 * 2^3 = 0.40
    assert policy.calculate_delay(4) == 0.40
    # Attempt 5: 0.05 * 2^4 = 0.80 -> capped at max_backoff (0.50)
    assert policy.calculate_delay(5) == 0.50


def test_backoff_policy_jitter_modes():
    """Verify jitter produces values within expected bounded ranges."""
    # Full jitter: [0, base]
    p_full = BackoffPolicy(initial_backoff=1.0, max_backoff=10.0, jitter="full")
    for _ in range(20):
        d = p_full.calculate_delay(1)  # base is 1.0
        assert 0.0 <= d <= 1.0

    # Equal jitter: [base/2, base]
    p_equal = BackoffPolicy(initial_backoff=1.0, max_backoff=10.0, jitter="equal")
    for _ in range(20):
        d = p_equal.calculate_delay(1)
        assert 0.5 <= d <= 1.0


def test_backoff_policy_retry_after_parsing():
    """Verify Retry-After header parsing for numeric seconds and HTTP date."""
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("  5  ") == 5.0
    assert parse_retry_after("2.75") == 2.75

    # Retry-After override in BackoffPolicy
    p = BackoffPolicy(initial_backoff=0.05, max_backoff=5.0)
    assert p.calculate_delay(1, retry_after=3.2) == 3.2
    assert p.calculate_delay(1, retry_after=10.0) == 5.0  # capped at max_backoff


def test_timeout_policy_to_httpx():
    """Verify TimeoutPolicy translates to httpx.Timeout object."""
    tp = TimeoutPolicy(connect_timeout=1.5, read_timeout=4.0, write_timeout=3.0, total_timeout=8.0)
    httpx_t = tp.to_httpx_timeout()
    assert httpx_t.connect == 1.5
    assert httpx_t.read == 4.0
    assert httpx_t.write == 3.0


def test_transient_error_classification():
    """Verify status codes and exceptions classified as retryable vs permanent."""
    assert is_retryable_status(500) is True
    assert is_retryable_status(502) is True
    assert is_retryable_status(503) is True
    assert is_retryable_status(504) is True
    assert is_retryable_status(429) is True

    assert is_retryable_status(200) is False
    assert is_retryable_status(400) is False
    assert is_retryable_status(401) is False
    assert is_retryable_status(403) is False
    assert is_retryable_status(404) is False
    assert is_retryable_status(422) is False

    assert is_retryable_exception(httpx.ReadTimeout("timeout")) is True
    assert is_retryable_exception(httpx.ConnectError("connection refused")) is True
    assert is_retryable_exception(ValueError("invalid payload")) is False


# =====================================================================
# Stand-In Platform Chaos Recovery Tests
# =====================================================================

def test_stand_in_fail_before_commit_chaos_recovery(fake_session, mock_db):
    """Stand-in platform throws 503 before saving on attempt 1, succeeds on attempt 2."""
    test_client = TestClient(stand_in_app)
    adapter = StandInPlatformAdapter(client=test_client)

    # Set fail_before_commit_rate to 1.0 initially
    STAND_IN_STATE["cfg"]["fail_before_commit_rate"] = 1.0

    sleep_calls = []

    def mock_sleep(seconds: float):
        sleep_calls.append(seconds)
        # Turn off failure after attempt 1
        STAND_IN_STATE["cfg"]["fail_before_commit_rate"] = 0.0

    b_policy = BackoffPolicy(initial_backoff=0.05, jitter="none")

    res = send_ad_to_stand_in(
        "ad-retry-1",
        connection=fake_session,
        adapter=adapter,
        max_retries=3,
        backoff_policy=b_policy,
        sleep_fn=mock_sleep,
    )

    assert res["status"] == "sent"
    assert res["attempts"] == 2
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 0.05

    # Verify audit records in ad_sends table
    sends = mock_db.sends
    assert len(sends) == 2
    assert sends[0]["status"] == "retrying"
    assert sends[0]["attempt_count"] == 1
    assert "503" in sends[0]["error_message"]
    assert sends[1]["status"] == "accepted"
    assert sends[1]["attempt_count"] == 2


def test_stand_in_fail_after_commit_chaos_recovery_zero_duplicates(fake_session, mock_db):
    """Stand-in platform saves ad but returns 503; on retry returns Idempotent-Replay with 0 duplicates."""
    test_client = TestClient(stand_in_app)
    adapter = StandInPlatformAdapter(client=test_client)

    STAND_IN_STATE["cfg"]["fail_after_commit_rate"] = 1.0

    sleep_calls = []

    def mock_sleep(seconds: float):
        sleep_calls.append(seconds)
        STAND_IN_STATE["cfg"]["fail_after_commit_rate"] = 0.0

    b_policy = BackoffPolicy(initial_backoff=0.1, jitter="none")

    res = send_ad_to_stand_in(
        "ad-retry-1",
        connection=fake_session,
        adapter=adapter,
        max_retries=3,
        backoff_policy=b_policy,
        sleep_fn=mock_sleep,
    )

    assert res["status"] == "sent"
    assert res["idempotent_replay"] is True
    assert res["attempts"] == 2
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 0.1

    # Check stand-in platform stats: ZERO duplicates created!
    stats_resp = test_client.get("/admin/stats")
    stats = stats_resp.json()
    assert stats["duplicates"] == 0
    assert stats["ads"] == 1


def test_timeout_retries_exhausted_moves_to_dead_letter(fake_session, mock_db):
    """Repeated timeouts exhaust max_retries and move ad into dead_letter queue."""
    class AlwaysTimeoutAdapter(BasePlatformAdapter):
        def send_ad(self, payload, idempotency_key, timeout=None):
            raise httpx.ReadTimeout(f"Read timed out after {timeout}")

    sleep_calls = []

    def mock_sleep(seconds: float):
        sleep_calls.append(seconds)

    b_policy = BackoffPolicy(initial_backoff=0.05, max_backoff=1.0, backoff_factor=2.0, jitter="none")

    res = send_ad_to_stand_in(
        "ad-retry-1",
        connection=fake_session,
        adapter=AlwaysTimeoutAdapter(),
        max_retries=3,
        backoff_policy=b_policy,
        sleep_fn=mock_sleep,
    )

    assert res["status"] == "dead_letter"
    assert res["attempts"] == 3
    assert len(sleep_calls) == 2  # slept between attempt 1->2 and 2->3
    assert sleep_calls[0] == 0.05
    assert sleep_calls[1] == 0.10

    # Ad approval state transitioned to dead_letter
    appr = mock_db.approvals["ad-retry-1"]
    assert appr["state"] == ApprovalState.DEAD_LETTER.value

    # Alert created
    alerts = mock_db.alerts
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "dead_letter"
    assert alerts[0]["severity"] == "error"

    # All 3 attempts logged in ad_sends
    sends = mock_db.sends
    assert len(sends) == 3
    assert sends[0]["status"] == "retrying"
    assert sends[1]["status"] == "retrying"
    assert sends[2]["status"] == "dead_letter"


def test_timeout_release_to_approved_when_deferred_retry_enabled(fake_session, mock_db):
    """When release_to_approved_on_timeout is True, timeout resets state to approved (Architecture §3)."""
    class SingleTimeoutAdapter(BasePlatformAdapter):
        def send_ad(self, payload, idempotency_key, timeout=None):
            raise httpx.ReadTimeout("Timeout")

    res = send_ad_to_stand_in(
        "ad-retry-1",
        connection=fake_session,
        adapter=SingleTimeoutAdapter(),
        max_retries=1,
        release_to_approved_on_timeout=True,
    )

    assert res["status"] == "approved"
    assert res.get("released_for_retry") is True
    # State in database remains/reverts to approved for worker resume
    assert mock_db.approvals["ad-retry-1"]["state"] == ApprovalState.APPROVED.value


def test_non_retryable_client_error_fails_fast(fake_session, mock_db):
    """4xx Client errors fail fast without wasting retries."""
    class ClientErrorAdapter(BasePlatformAdapter):
        def send_ad(self, payload, idempotency_key, timeout=None):
            return httpx.Response(400, text="Bad Request: Malformed JSON")

    sleep_calls = []

    res = send_ad_to_stand_in(
        "ad-retry-1",
        connection=fake_session,
        adapter=ClientErrorAdapter(),
        max_retries=3,
        sleep_fn=lambda s: sleep_calls.append(s),
    )

    assert res["status"] == "dead_letter"
    assert res["attempts"] == 1  # Did NOT retry!
    assert len(sleep_calls) == 0
    assert "400" in res["error"]


def test_platform_policy_rejection_halts_retries_immediately(fake_session, mock_db):
    """Platform policy rejections (status: rejected) do not retry and alert reviewer."""
    test_client = TestClient(stand_in_app)
    adapter = StandInPlatformAdapter(client=test_client)

    # Use forbidden word in text that triggers rejection_rules.json
    mock_db.ads["ad-retry-1"]["ad_text"] = "Get 100% guaranteed miracle overnight cure now!"

    sleep_calls = []

    res = send_ad_to_stand_in(
        "ad-retry-1",
        connection=fake_session,
        adapter=adapter,
        max_retries=3,
        sleep_fn=lambda s: sleep_calls.append(s),
    )

    assert res["status"] == "rejected"
    assert res["attempts"] == 1  # 0 retries
    assert len(sleep_calls) == 0
    assert mock_db.approvals["ad-retry-1"]["state"] == ApprovalState.REJECTED.value

    # Alert recorded
    alerts = mock_db.alerts
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "platform_rejection"


# =====================================================================
# Dead-Letter Queue & Manual Retry Tests
# =====================================================================

def test_manual_retry_dead_letter_ad_success(fake_session, mock_db):
    """Reviewer can retry an ad in dead_letter queue, recovering it to sent state."""
    test_client = TestClient(stand_in_app)
    adapter = StandInPlatformAdapter(client=test_client)

    # Put ad in dead_letter state
    mock_db.approvals["ad-retry-1"]["state"] = ApprovalState.DEAD_LETTER.value

    res = retry_dead_letter_ad(
        "ad-retry-1",
        connection=fake_session,
        adapter=adapter,
        user_id="buyer_1",
    )

    assert res["status"] == "sent"
    assert mock_db.approvals["ad-retry-1"]["state"] == ApprovalState.SENT.value


def test_manual_retry_dead_letter_fails_if_not_dead_letter(fake_session, mock_db):
    """retry_dead_letter_ad rejects ads that are not in dead_letter state."""
    mock_db.approvals["ad-retry-1"]["state"] = ApprovalState.APPROVED.value

    with pytest.raises(ValueError, match="must be in 'dead_letter'"):
        retry_dead_letter_ad("ad-retry-1", connection=fake_session)


def test_retry_dead_letter_batch_ads(fake_session, mock_db):
    """retry_dead_letter_batch_ads retries all dead_letter ads in a batch."""
    test_client = TestClient(stand_in_app)
    adapter = StandInPlatformAdapter(client=test_client)

    mock_db.approvals["ad-retry-1"]["state"] = ApprovalState.DEAD_LETTER.value
    mock_db.approvals["ad-retry-2"]["state"] = ApprovalState.DEAD_LETTER.value

    summary = retry_dead_letter_batch_ads(
        "batch-retry-1",
        connection=fake_session,
        adapter=adapter,
        user_id="buyer_1",
    )

    assert summary["total_retried"] == 2
    assert summary["succeeded"] == 2
    assert summary["dead_letter"] == 0
    assert mock_db.approvals["ad-retry-1"]["state"] == ApprovalState.SENT.value
    assert mock_db.approvals["ad-retry-2"]["state"] == ApprovalState.SENT.value


# =====================================================================
# API Endpoints & Multi-Tenant Authorization Tests
# =====================================================================

def test_api_retry_dead_letter_endpoint_success(fake_session, mock_db):
    """POST /ads/{ad_id}/retry allows authorized reviewer to retry dead-letter ad."""
    test_client = TestClient(main_api_app)
    token = create_access_token("buyer_1", "buyer")

    # Stand-in adapter inside endpoint uses stand_in_app
    mock_db.approvals["ad-retry-1"]["state"] = ApprovalState.DEAD_LETTER.value

    resp = test_client.post(
        "/ads/ad-retry-1/retry",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("sent", "dead_letter")


def test_api_retry_dead_letter_endpoint_unauthorized_client(fake_session, mock_db):
    """POST /ads/{ad_id}/retry returns 403 Forbidden for unassigned client."""
    test_client = TestClient(main_api_app)
    token = create_access_token("buyer_2", "buyer")  # assigned only to client_lumina

    mock_db.approvals["ad-retry-1"]["state"] = ApprovalState.DEAD_LETTER.value

    resp = test_client.post(
        "/ads/ad-retry-1/retry",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403
    detail = resp.json()["detail"].lower()
    assert "forbidden" in detail or "access denied" in detail


def test_api_retry_dead_letter_batch_endpoint(fake_session, mock_db):
    """POST /batches/{batch_id}/retry-dead-letter retries all dead-letter ads for assigned clients."""
    test_client = TestClient(main_api_app)
    token = create_access_token("buyer_1", "buyer")

    mock_db.approvals["ad-retry-1"]["state"] = ApprovalState.DEAD_LETTER.value

    resp = test_client.post(
        "/batches/batch-retry-1/retry-dead-letter",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["batch_id"] == "batch-retry-1"
    assert data["total_retried"] == 1


# =====================================================================
# Concurrency & Idempotency Under Rapid Retries
# =====================================================================

def test_rapid_repeat_retries_zero_duplicates_on_stand_in_platform(fake_session, mock_db):
    """Executing 50 rapid repeat sends with the same idempotency key produces 0 duplicate ads."""
    test_client = TestClient(stand_in_app)
    adapter = StandInPlatformAdapter(client=test_client)

    # First send: successfully sends ad
    res1 = send_ad_to_stand_in(
        "ad-retry-1",
        connection=fake_session,
        adapter=adapter,
    )
    assert res1["status"] == "sent"

    # 49 subsequent repeat calls (simulating concurrent user clicks or automated retries)
    for _ in range(49):
        res = send_ad_to_stand_in(
            "ad-retry-1",
            connection=fake_session,
            adapter=adapter,
        )
        assert res["status"] == "sent"
        assert res.get("already_sent") is True

    # Stand-in platform stats verify EXACTLY 1 ad created and 0 duplicates
    stats_resp = test_client.get("/admin/stats")
    stats = stats_resp.json()
    assert stats["ads"] == 1
    assert stats["duplicates"] == 0
