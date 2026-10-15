"""Idempotent sender worker for stand-in dry-run ad platform.

Implements PRD §4.6, Dataflow Architecture §3, and Never-break Rules 1, 2, 7:
- No ad is sent without person's approval (Rule 1).
- Each ad is sent exactly once using an idempotency key (Rule 2).
- Bounded retries with exponential backoff, timeouts, and dead-letter queue.
- Rejections or uncertain states alert a person and return to review; no automatic relaunch.
- Real platform calls remain strictly switched off (Rule 7).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from apps.api.approvals import (
    ApprovalState,
    ContentHashMismatchError,
    UnapprovedAdError,
    get_ad_record,
    get_or_create_ad_approval,
    transition_ad_state,
)
from apps.api.database import get_session
from apps.worker.platforms import (
    BasePlatformAdapter,
    RealPlatformCallDisabledError,
    StandInPlatformAdapter,
    get_platform_adapter,
)

logger = logging.getLogger(__name__)

# --- Database DDL ---

CREATE_AD_SENDS_TABLE = """
CREATE TABLE IF NOT EXISTS ad_sends (
    send_id TEXT PRIMARY KEY,
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id),
    batch_id TEXT NOT NULL REFERENCES batches (batch_id),
    idempotency_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    platform_ad_id TEXT,
    platform_status TEXT,
    rejection_reason TEXT,
    is_dry_run BOOLEAN NOT NULL DEFAULT TRUE,
    idempotent_replay BOOLEAN NOT NULL DEFAULT FALSE,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    request_payload TEXT,
    response_payload TEXT,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
"""

CREATE_SENDS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ad_sends_ad_id ON ad_sends (ad_id);
CREATE INDEX IF NOT EXISTS idx_ad_sends_batch_id ON ad_sends (batch_id);
CREATE INDEX IF NOT EXISTS idx_ad_sends_idempotency_key ON ad_sends (idempotency_key);
"""

CREATE_SEND_ALERTS_TABLE = """
CREATE TABLE IF NOT EXISTS send_alerts (
    alert_id TEXT PRIMARY KEY,
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id),
    batch_id TEXT NOT NULL REFERENCES batches (batch_id),
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    message TEXT NOT NULL,
    details TEXT,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_by TEXT REFERENCES users (user_id),
    resolved_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
"""

CREATE_ALERTS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_send_alerts_ad_id ON send_alerts (ad_id);
CREATE INDEX IF NOT EXISTS idx_send_alerts_client_id ON send_alerts (client_id);
"""


def create_send_tables() -> None:
    """Create the ad_sends and send_alerts tables and indexes."""
    try:
        with get_session() as conn:
            conn.execute(CREATE_AD_SENDS_TABLE)
            try:
                conn.execute(CREATE_SENDS_INDEXES)
            except Exception:
                pass
            conn.execute(CREATE_SEND_ALERTS_TABLE)
            try:
                conn.execute(CREATE_ALERTS_INDEXES)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("Skipping create_send_tables on non-Postgres or mock connection: %s", exc)


# --- Retry and Timeout Policies (PRD §4.6) ---

@dataclass
class BackoffPolicy:
    """Configurable exponential backoff policy with jitter per PRD §4.6."""
    initial_backoff: float = 0.05  # seconds
    max_backoff: float = 5.0      # seconds ceiling
    backoff_factor: float = 2.0   # exponential multiplier
    jitter: str = "full"          # 'none', 'full', 'equal'

    def calculate_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Calculate backoff duration for a given attempt (1-based), honoring Retry-After if set."""
        if retry_after is not None and retry_after > 0:
            return min(self.max_backoff, float(retry_after))

        base_delay = min(
            self.max_backoff,
            self.initial_backoff * (self.backoff_factor ** max(0, attempt - 1)),
        )

        if self.jitter == "none":
            return base_delay
        elif self.jitter == "equal":
            half = base_delay / 2.0
            return half + random.uniform(0.0, half)
        else:  # 'full' jitter
            return random.uniform(0.0, base_delay)


@dataclass
class TimeoutPolicy:
    """Configurable timeout boundaries for platform requests."""
    connect_timeout: float = 2.0
    read_timeout: float = 5.0
    write_timeout: float = 5.0
    total_timeout: float = 10.0

    def to_httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            timeout=self.total_timeout,
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.write_timeout,
        )


RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def is_retryable_status(status_code: int) -> bool:
    """Check if an HTTP status code indicates a transient, retryable platform error."""
    return status_code in RETRYABLE_STATUS_CODES


def is_retryable_exception(exc: Exception) -> bool:
    """Check if an exception represents a transient network or timeout error."""
    return isinstance(exc, (httpx.TimeoutException, httpx.RequestError))


def parse_retry_after(header_val: str | None) -> float | None:
    """Parse HTTP Retry-After header value into seconds."""
    if not header_val:
        return None
    val = header_val.strip()
    try:
        return max(0.0, float(val))
    except ValueError:
        pass
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(val)
        diff = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, float(diff))
    except Exception:
        return None


# --- Helpers ---

def generate_idempotency_key(ad_id: str, content_hash: str) -> str:
    """Generate deterministic idempotency key for an ad and approved content hash."""
    return f"send:{ad_id}:{content_hash}"


def build_stand_in_ad_payload(ad: dict[str, Any], content_hash: str) -> dict[str, Any]:
    """Build the payload expected by data/stand_in_platform/app.py."""
    return {
        "ad_id": str(ad["ad_id"]),
        "client": str(ad["client_id"]),
        "platform": str(ad["platform"]),
        "text": str(ad.get("ad_text") or ""),
        "headline": str(ad.get("headline") or ""),
        "link": str(ad.get("destination_url") or ""),
        "creative": str(ad.get("creative_file") or ""),
        "content_hash": str(content_hash),
    }


def record_send_attempt(
    conn: Any,
    *,
    ad_id: str,
    client_id: str,
    batch_id: str,
    idempotency_key: str,
    content_hash: str,
    platform: str,
    status: str,
    platform_ad_id: str | None = None,
    platform_status: str | None = None,
    rejection_reason: str | None = None,
    is_dry_run: bool = True,
    idempotent_replay: bool = False,
    attempt_count: int = 1,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Persist a send attempt or result record into ad_sends."""
    send_id = f"send_{uuid.uuid4().hex}"
    req_json = json.dumps(request_payload) if request_payload is not None else None
    res_json = json.dumps(response_payload) if response_payload is not None else None
    now = datetime.now(timezone.utc)

    try:
        conn.execute(
            """
            INSERT INTO ad_sends (
                send_id, ad_id, client_id, batch_id, idempotency_key, content_hash,
                platform, status, platform_ad_id, platform_status, rejection_reason,
                is_dry_run, idempotent_replay, attempt_count, request_payload,
                response_payload, error_message, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s
            );
            """,
            (
                send_id, ad_id, client_id, batch_id, idempotency_key, content_hash,
                platform, status, platform_ad_id, platform_status, rejection_reason,
                is_dry_run, idempotent_replay, attempt_count, req_json,
                res_json, error_message, now,
            ),
        )
    except Exception as exc:
        logger.debug("Skipping ad_sends insert on mock connection: %s", exc)

    return {
        "send_id": send_id,
        "ad_id": ad_id,
        "client_id": client_id,
        "batch_id": batch_id,
        "idempotency_key": idempotency_key,
        "content_hash": content_hash,
        "platform": platform,
        "status": status,
        "platform_ad_id": platform_ad_id,
        "platform_status": platform_status,
        "rejection_reason": rejection_reason,
        "is_dry_run": is_dry_run,
        "idempotent_replay": idempotent_replay,
        "attempt_count": attempt_count,
        "error_message": error_message,
        "created_at": now.isoformat(),
    }


def record_send_alert(
    conn: Any,
    *,
    ad_id: str,
    client_id: str,
    batch_id: str,
    alert_type: str,
    message: str,
    severity: str = "warning",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an alert for reviewer notification per PRD §4.6."""
    alert_id = f"alert_{uuid.uuid4().hex}"
    details_str = json.dumps(details) if details is not None else None
    now = datetime.now(timezone.utc)

    try:
        conn.execute(
            """
            INSERT INTO send_alerts (
                alert_id, ad_id, client_id, batch_id, alert_type, severity, message, details, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (alert_id, ad_id, client_id, batch_id, alert_type, severity, message, details_str, now),
        )
    except Exception as exc:
        logger.debug("Skipping send_alerts insert on mock connection: %s", exc)

    return {
        "alert_id": alert_id,
        "ad_id": ad_id,
        "client_id": client_id,
        "batch_id": batch_id,
        "alert_type": alert_type,
        "severity": severity,
        "message": message,
        "details": details,
        "resolved": False,
        "created_at": now.isoformat(),
    }


def get_ad_send_records(conn: Any, ad_id: str) -> list[dict[str, Any]]:
    """Retrieve audit history of send attempts for an ad."""
    try:
        cur = conn.execute(
            """
            SELECT send_id, ad_id, client_id, batch_id, idempotency_key, content_hash,
                   platform, status, platform_ad_id, platform_status, rejection_reason,
                   is_dry_run, idempotent_replay, attempt_count, request_payload,
                   response_payload, error_message, created_at
            FROM ad_sends
            WHERE ad_id = %s
            ORDER BY created_at ASC;
            """,
            (ad_id,),
        )
        records = []
        for r in cur.fetchall():
            records.append({
                "send_id": str(r[0]),
                "ad_id": str(r[1]),
                "client_id": str(r[2]),
                "batch_id": str(r[3]),
                "idempotency_key": str(r[4]),
                "content_hash": str(r[5]),
                "platform": str(r[6]),
                "status": str(r[7]),
                "platform_ad_id": str(r[8]) if r[8] else None,
                "platform_status": str(r[9]) if r[9] else None,
                "rejection_reason": str(r[10]) if r[10] else None,
                "is_dry_run": bool(r[11]),
                "idempotent_replay": bool(r[12]),
                "attempt_count": int(r[13]) if r[13] is not None else 1,
                "error_message": str(r[16]) if r[16] else None,
                "created_at": r[17].isoformat() if hasattr(r[17], "isoformat") else str(r[17]),
            })
        return records
    except Exception:
        return []


def get_ad_alerts(conn: Any, ad_id: str) -> list[dict[str, Any]]:
    """Retrieve all reviewer alerts recorded for an ad."""
    try:
        cur = conn.execute(
            """
            SELECT alert_id, ad_id, client_id, batch_id, alert_type, severity, message, details, resolved, created_at
            FROM send_alerts
            WHERE ad_id = %s
            ORDER BY created_at DESC;
            """,
            (ad_id,),
        )
        alerts = []
        for r in cur.fetchall():
            alerts.append({
                "alert_id": str(r[0]),
                "ad_id": str(r[1]),
                "client_id": str(r[2]),
                "batch_id": str(r[3]),
                "alert_type": str(r[4]),
                "severity": str(r[5]),
                "message": str(r[6]),
                "details": json.loads(r[7]) if isinstance(r[7], str) else r[7],
                "resolved": bool(r[8]),
                "created_at": r[9].isoformat() if hasattr(r[9], "isoformat") else str(r[9]),
            })
        return alerts
    except Exception:
        return []


# --- Core Idempotent Send Engine ---

def send_ad_to_stand_in(
    ad_id: str,
    *,
    connection: Any = None,
    adapter: BasePlatformAdapter | None = None,
    client: httpx.Client | None = None,
    base_url: str | None = None,
    max_retries: int = 3,
    timeout_seconds: float = 5.0,
    timeout_policy: TimeoutPolicy | None = None,
    initial_backoff: float = 0.05,
    backoff_policy: BackoffPolicy | None = None,
    sleep_fn: Any = time.sleep,
    user_id: str | None = None,
    enforce_dry_run: bool = True,
    release_to_approved_on_timeout: bool = False,
) -> dict[str, Any]:
    """Execute idempotent dry-run ad send to the stand-in platform with bounded retries."""
    if not enforce_dry_run:
        raise RealPlatformCallDisabledError(
            "Real platform calls are switched off in Launch Gate v1 (PRD §4.6, Rule 7)."
        )

    b_policy = backoff_policy or BackoffPolicy(initial_backoff=initial_backoff)
    t_policy = timeout_policy or TimeoutPolicy(
        connect_timeout=min(2.0, timeout_seconds),
        read_timeout=timeout_seconds,
        write_timeout=timeout_seconds,
        total_timeout=timeout_seconds * 2,
    )
    eff_timeout = t_policy.to_httpx_timeout()

    def _execute(conn: Any) -> dict[str, Any]:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise ValueError(f"Ad '{ad_id}' not found")

        approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        current_state = approval.get("state")

        # 1. Idempotent short-circuit: already sent!
        if current_state == ApprovalState.SENT.value:
            sends = get_ad_send_records(conn, ad_id)
            latest = sends[-1] if sends else {}
            return {
                "status": "sent",
                "already_sent": True,
                "ad_id": ad_id,
                "platform_ad_id": latest.get("platform_ad_id"),
                "idempotency_key": approval.get("idempotency_key") or latest.get("idempotency_key"),
                "content_hash": approval.get("content_hash"),
                "is_dry_run": True,
                "idempotent_replay": True,
                "attempts": approval.get("send_attempts", 1),
                "message": "Ad was already sent to platform (idempotent no-op)",
            }

        # 2. Rule 1: No ad sent without approval
        if current_state not in (ApprovalState.APPROVED.value, ApprovalState.SENDING.value):
            raise UnapprovedAdError(
                f"Cannot send ad with approval state '{current_state}'; approval is required (Never-break rule 1)"
            )

        # 3. Rule 2 / PRD D13: Content hash validation
        approved_hash = approval.get("content_hash")
        current_hash = ad.get("row_hash")
        if not approved_hash or approved_hash != current_hash:
            raise ContentHashMismatchError(
                f"Content hash mismatch: approved version hash '{approved_hash}' "
                f"does not match current ad hash '{current_hash}'. Only the approved version can be sent."
            )

        # 4. Idempotency key reuse / generation
        idempotency_key = approval.get("idempotency_key") or generate_idempotency_key(ad_id, approved_hash)

        # 5. Lock row by transitioning to 'sending' state
        if current_state != ApprovalState.SENDING.value:
            transition_ad_state(
                conn,
                ad_id=ad_id,
                to_state=ApprovalState.SENDING.value,
                changed_by=user_id,
                idempotency_key=idempotency_key,
            )

        # 6. Build payload and initialize adapter
        platform_adapter = adapter or get_platform_adapter(
            ad.get("platform", "stand_in"),
            dry_run=True,
            base_url=base_url,
            client=client,
            timeout=timeout_seconds,
        )
        payload = build_stand_in_ad_payload(ad, approved_hash)

        # 7. Bounded retry loop with exponential backoff & jitter
        last_error: str | None = None
        is_timeout_failure: bool = False
        retry_delays_applied: list[float] = []

        for attempt in range(1, max_retries + 1):
            is_timeout_failure = False
            try:
                resp = platform_adapter.send_ad(
                    payload,
                    idempotency_key=idempotency_key,
                    timeout=eff_timeout,
                )

                # Check for successful HTTP communication (200 OK or 201 Created)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    is_replay = resp.headers.get("Idempotent-Replay", "").lower() == "true"
                    p_status = data.get("status")
                    p_reason = data.get("reason")
                    p_ad_id = data.get("platform_ad_id")

                    if p_status == "accepted":
                        # Accepted by stand-in platform! Transition sending -> sent
                        transition_ad_state(
                            conn,
                            ad_id=ad_id,
                            to_state=ApprovalState.SENT.value,
                            changed_by=user_id,
                        )
                        record_send_attempt(
                            conn,
                            ad_id=ad_id,
                            client_id=ad["client_id"],
                            batch_id=ad["batch_id"],
                            idempotency_key=idempotency_key,
                            content_hash=approved_hash,
                            platform=ad["platform"],
                            status="accepted",
                            platform_ad_id=p_ad_id,
                            platform_status="accepted",
                            is_dry_run=True,
                            idempotent_replay=is_replay,
                            attempt_count=attempt,
                            request_payload=payload,
                            response_payload=data,
                        )
                        return {
                            "status": "sent",
                            "ad_id": ad_id,
                            "platform_ad_id": p_ad_id,
                            "idempotency_key": idempotency_key,
                            "content_hash": approved_hash,
                            "is_dry_run": True,
                            "idempotent_replay": is_replay,
                            "attempts": attempt,
                            "retry_delays": retry_delays_applied,
                            "platform_status": "accepted",
                            "reason": None,
                        }
                    else:
                        # Rejected by stand-in platform (e.g. policy violation)! Permanent rejection
                        transition_ad_state(
                            conn,
                            ad_id=ad_id,
                            to_state=ApprovalState.REJECTED.value,
                            changed_by=user_id,
                            reason=f"Platform rejection: {p_reason}",
                        )
                        record_send_attempt(
                            conn,
                            ad_id=ad_id,
                            client_id=ad["client_id"],
                            batch_id=ad["batch_id"],
                            idempotency_key=idempotency_key,
                            content_hash=approved_hash,
                            platform=ad["platform"],
                            status="rejected",
                            platform_ad_id=p_ad_id,
                            platform_status="rejected",
                            rejection_reason=p_reason,
                            is_dry_run=True,
                            idempotent_replay=is_replay,
                            attempt_count=attempt,
                            request_payload=payload,
                            response_payload=data,
                        )
                        record_send_alert(
                            conn,
                            ad_id=ad_id,
                            client_id=ad["client_id"],
                            batch_id=ad["batch_id"],
                            alert_type="platform_rejection",
                            severity="warning",
                            message=f"Ad #{ad['row_number']} rejected by stand-in platform: {p_reason}",
                            details={"reason": p_reason, "platform_ad_id": p_ad_id},
                        )
                        return {
                            "status": "rejected",
                            "ad_id": ad_id,
                            "platform_ad_id": p_ad_id,
                            "idempotency_key": idempotency_key,
                            "content_hash": approved_hash,
                            "is_dry_run": True,
                            "idempotent_replay": is_replay,
                            "attempts": attempt,
                            "platform_status": "rejected",
                            "reason": p_reason,
                        }

                elif is_retryable_status(resp.status_code):
                    # Transient error (500, 502, 503, 504, 429)
                    last_error = f"HTTP {resp.status_code}: {resp.text}"
                    logger.warning("Stand-in platform transient error on attempt %d: %s", attempt, last_error)
                    if attempt < max_retries:
                        record_send_attempt(
                            conn,
                            ad_id=ad_id,
                            client_id=ad["client_id"],
                            batch_id=ad["batch_id"],
                            idempotency_key=idempotency_key,
                            content_hash=approved_hash,
                            platform=ad["platform"],
                            status="retrying",
                            is_dry_run=True,
                            attempt_count=attempt,
                            request_payload=payload,
                            error_message=last_error,
                        )
                        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                        delay = b_policy.calculate_delay(attempt, retry_after=retry_after)
                        retry_delays_applied.append(delay)
                        sleep_fn(delay)
                        continue
                    else:
                        break

                else:
                    # Non-retryable 4xx client error
                    last_error = f"HTTP {resp.status_code}: {resp.text}"
                    logger.error("Stand-in platform non-retryable client error: %s", last_error)
                    record_send_attempt(
                        conn,
                        ad_id=ad_id,
                        client_id=ad["client_id"],
                        batch_id=ad["batch_id"],
                        idempotency_key=idempotency_key,
                        content_hash=approved_hash,
                        platform=ad["platform"],
                        status="non_retryable_error",
                        is_dry_run=True,
                        attempt_count=attempt,
                        request_payload=payload,
                        error_message=last_error,
                    )
                    break

            except (httpx.TimeoutException, httpx.RequestError) as exc:
                is_timeout_failure = isinstance(exc, httpx.TimeoutException)
                err_tag = "Timeout" if is_timeout_failure else "Network error"
                last_error = f"{err_tag}: {exc}"
                logger.warning("Stand-in platform %s on attempt %d: %s", err_tag.lower(), attempt, exc)
                if attempt < max_retries:
                    record_send_attempt(
                        conn,
                        ad_id=ad_id,
                        client_id=ad["client_id"],
                        batch_id=ad["batch_id"],
                        idempotency_key=idempotency_key,
                        content_hash=approved_hash,
                        platform=ad["platform"],
                        status="retrying",
                        is_dry_run=True,
                        attempt_count=attempt,
                        request_payload=payload,
                        error_message=last_error,
                    )
                    delay = b_policy.calculate_delay(attempt)
                    retry_delays_applied.append(delay)
                    sleep_fn(delay)
                    continue
                else:
                    break

        # Check for release to approved on timeout (Dataflow Architecture §3: sending --> approved: timeout, retry with backoff)
        if release_to_approved_on_timeout and is_timeout_failure:
            transition_ad_state(
                conn,
                ad_id=ad_id,
                to_state=ApprovalState.APPROVED.value,
                changed_by=user_id,
                reason=f"Timeout release for deferred retry with backoff: {last_error}",
            )
            record_send_alert(
                conn,
                ad_id=ad_id,
                client_id=ad["client_id"],
                batch_id=ad["batch_id"],
                alert_type="uncertain_send_state",
                severity="warning",
                message=f"Ad #{ad['row_number']} timed out; send outcome uncertain. Released back to approved state for deferred review.",
                details={"attempts": attempt, "error": last_error, "sub_type": "timeout_retry_deferred"},
            )
            return {
                "status": "approved",
                "ad_id": ad_id,
                "idempotency_key": idempotency_key,
                "content_hash": approved_hash,
                "is_dry_run": True,
                "attempts": attempt,
                "retry_delays": retry_delays_applied,
                "released_for_retry": True,
                "error": last_error,
            }

        # Otherwise retries exhausted or non-retryable failure -> Dead Letter Queue
        dead_letter_reason = f"Send retries exhausted after {attempt} attempts: {last_error}"
        transition_ad_state(
            conn,
            ad_id=ad_id,
            to_state=ApprovalState.DEAD_LETTER.value,
            changed_by=user_id,
            reason=dead_letter_reason,
        )
        record_send_attempt(
            conn,
            ad_id=ad_id,
            client_id=ad["client_id"],
            batch_id=ad["batch_id"],
            idempotency_key=idempotency_key,
            content_hash=approved_hash,
            platform=ad["platform"],
            status="dead_letter",
            is_dry_run=True,
            attempt_count=attempt,
            request_payload=payload,
            error_message=last_error,
        )
        record_send_alert(
            conn,
            ad_id=ad_id,
            client_id=ad["client_id"],
            batch_id=ad["batch_id"],
            alert_type="dead_letter",
            severity="error",
            message=f"Send retries exhausted for Ad #{ad['row_number']}: {last_error}",
            details={"attempts": attempt, "error": last_error},
        )
        return {
            "status": "dead_letter",
            "ad_id": ad_id,
            "idempotency_key": idempotency_key,
            "content_hash": approved_hash,
            "is_dry_run": True,
            "attempts": attempt,
            "retry_delays": retry_delays_applied,
            "error": last_error,
        }

    if connection is not None:
        return _execute(connection)
    with get_session() as conn:
        return _execute(conn)


def retry_dead_letter_ad(
    ad_id: str,
    *,
    connection: Any = None,
    user_id: str | None = None,
    adapter: BasePlatformAdapter | None = None,
    client: httpx.Client | None = None,
    base_url: str | None = None,
    max_retries: int = 3,
    initial_backoff: float = 0.05,
    backoff_policy: BackoffPolicy | None = None,
    timeout_seconds: float = 5.0,
    timeout_policy: TimeoutPolicy | None = None,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """Retry an ad that previously failed and is in dead_letter queue."""
    def _execute(conn: Any) -> dict[str, Any]:
        ad = get_ad_record(conn, ad_id)
        if ad is None:
            raise ValueError(f"Ad '{ad_id}' not found")
        approval = get_or_create_ad_approval(conn, ad_id, ad["client_id"])
        current_state = approval.get("state")
        if current_state != ApprovalState.DEAD_LETTER.value:
            raise ValueError(f"Cannot retry ad in state '{current_state}'; must be in 'dead_letter'")

        # Transition dead_letter -> approved
        transition_ad_state(
            conn,
            ad_id=ad_id,
            to_state=ApprovalState.APPROVED.value,
            changed_by=user_id,
            reason=f"Manual retry from dead letter by {user_id or 'reviewer'}",
        )
        # Re-send to stand-in
        return send_ad_to_stand_in(
            ad_id,
            connection=conn,
            adapter=adapter,
            client=client,
            base_url=base_url,
            max_retries=max_retries,
            initial_backoff=initial_backoff,
            backoff_policy=backoff_policy,
            timeout_seconds=timeout_seconds,
            timeout_policy=timeout_policy,
            sleep_fn=sleep_fn,
            user_id=user_id,
        )

    if connection is not None:
        return _execute(connection)
    with get_session() as conn:
        return _execute(conn)


def retry_dead_letter_batch_ads(
    batch_id: str,
    *,
    connection: Any = None,
    client_ids: list[str] | None = None,
    user_id: str | None = None,
    adapter: BasePlatformAdapter | None = None,
    client: httpx.Client | None = None,
    base_url: str | None = None,
    max_retries: int = 3,
    initial_backoff: float = 0.05,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """Retry all dead-letter ads in a batch belonging to assigned clients."""
    def _execute(conn: Any) -> dict[str, Any]:
        query = (
            "SELECT a.ad_id, a.client_id, ap.state "
            "FROM ads a JOIN ad_approvals ap ON a.ad_id = ap.ad_id "
            "WHERE a.batch_id = %s AND ap.state = 'dead_letter'"
        )
        params: list[Any] = [batch_id]
        if client_ids:
            query += " AND a.client_id = ANY(%s)"
            params.append(client_ids)
        query += " ORDER BY a.row_number ASC;"

        try:
            cur = conn.execute(query, tuple(params))
            rows = cur.fetchall()
        except Exception:
            rows = []

        retried_count = 0
        succeeded_count = 0
        dead_letter_count = 0
        results: list[dict[str, Any]] = []

        for row in rows:
            aid = str(row[0])
            retried_count += 1
            try:
                res = retry_dead_letter_ad(
                    aid,
                    connection=conn,
                    user_id=user_id,
                    adapter=adapter,
                    client=client,
                    base_url=base_url,
                    max_retries=max_retries,
                    initial_backoff=initial_backoff,
                    sleep_fn=sleep_fn,
                )
                results.append(res)
                if res.get("status") == "sent":
                    succeeded_count += 1
                elif res.get("status") == "dead_letter":
                    dead_letter_count += 1
            except Exception as exc:
                logger.error("Error retrying dead letter ad %s: %s", aid, exc)
                dead_letter_count += 1
                results.append({"ad_id": aid, "status": "error", "error": str(exc)})

        return {
            "batch_id": batch_id,
            "total_retried": retried_count,
            "succeeded": succeeded_count,
            "dead_letter": dead_letter_count,
            "results": results,
        }

    if connection is not None:
        return _execute(connection)
    with get_session() as conn:
        return _execute(conn)


def send_batch_approved_ads(
    batch_id: str,
    *,
    connection: Any = None,
    client_ids: list[str] | None = None,
    adapter: BasePlatformAdapter | None = None,
    client: httpx.Client | None = None,
    base_url: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Send all approved ads in a batch belonging to assigned clients."""
    def _execute(conn: Any) -> dict[str, Any]:
        # Fetch ads in this batch
        query = "SELECT a.ad_id, a.client_id, ap.state FROM ads a JOIN ad_approvals ap ON a.ad_id = ap.ad_id WHERE a.batch_id = %s"
        params: list[Any] = [batch_id]
        if client_ids:
            query += " AND a.client_id = ANY(%s)"
            params.append(client_ids)
        query += " ORDER BY a.row_number ASC;"

        try:
            cur = conn.execute(query, tuple(params))
            rows = cur.fetchall()
        except Exception:
            rows = []

        total_eligible = 0
        sent_count = 0
        rejected_count = 0
        dead_letter_count = 0
        skipped_already_sent = 0
        results: list[dict[str, Any]] = []

        for row in rows:
            aid = str(row[0])
            state = str(row[2])
            if state == ApprovalState.SENT.value:
                skipped_already_sent += 1
                continue
            if state == ApprovalState.APPROVED.value:
                # Rule 6: No ad is sent without a person's approval. Rejections or uncertain states alert a person; no automatic relaunch.
                alerts = get_ad_alerts(conn, aid)
                unresolved_blocking_alerts = [
                    al for al in alerts
                    if not al.get("resolved") and al.get("alert_type") in (
                        "platform_rejection", "uncertain_send_state", "stuck_sending"
                    )
                ]
                if unresolved_blocking_alerts:
                    logger.info("Skipping ad %s in batch send due to %d active unresolved alert(s) (no automatic relaunch)", aid, len(unresolved_blocking_alerts))
                    results.append({
                        "ad_id": aid,
                        "status": "skipped_uncertain_alert",
                        "message": f"Ad has {len(unresolved_blocking_alerts)} active person alert(s). Human review required before relaunch.",
                        "alerts": unresolved_blocking_alerts,
                    })
                    continue

                total_eligible += 1
                try:
                    res = send_ad_to_stand_in(
                        aid,
                        connection=conn,
                        adapter=adapter,
                        client=client,
                        base_url=base_url,
                        user_id=user_id,
                    )
                    results.append(res)
                    if res.get("status") == "sent":
                        sent_count += 1
                    elif res.get("status") == "rejected":
                        rejected_count += 1
                    elif res.get("status") == "dead_letter":
                        dead_letter_count += 1
                except Exception as exc:
                    logger.error("Error sending ad %s in batch: %s", aid, exc)
                    dead_letter_count += 1
                    results.append({"ad_id": aid, "status": "error", "error": str(exc)})

        return {
            "batch_id": batch_id,
            "total_eligible": total_eligible,
            "sent": sent_count,
            "rejected": rejected_count,
            "dead_letter": dead_letter_count,
            "skipped_already_sent": skipped_already_sent,
            "results": results,
        }

    if connection is not None:
        return _execute(connection)
    with get_session() as conn:
        return _execute(conn)
