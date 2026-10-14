r"""Durable alert notification delivery foundation.

Architecture & Lifecycle:
- Each delivery attempt is backed by a durable record in `notification_deliveries`.
- Explicit lifecycle states:
    pending -> processing -> delivered
                         \-> retryable_failed -> processing (on next_retry_at)
                         \-> permanently_failed (when attempt_count >= max_attempts)
- Prevents duplicate delivery records via stable deduplication keys and unique constraints.
- In-memory development delivery sink for local development and deterministic automated tests.
- Fail-closed security: production mode strictly disallows the development sink and
  requires valid live provider credentials.
- Multi-tenant client scoping: every delivery record is associated with a client_id.
- Safe logging: message bodies, access tokens, and credentials are never emitted to logs.
- At-least-once delivery limitation note: If an external provider receives and sends a message
  but drops the acknowledgment connection before Launch Gate receives confirmation, Launch Gate
  will classify the attempt as retryable and re-send. Providers that do not support idempotent
  message keys may deliver duplicate messages in this network partition scenario.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import logging
import os
import random
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional
import uuid

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# --- Lifecycle States ---

class DeliveryStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    RETRYABLE_FAILED = "retryable_failed"
    PERMANENTLY_FAILED = "permanently_failed"


TERMINAL_STATES = frozenset({DeliveryStatus.DELIVERED.value, DeliveryStatus.PERMANENTLY_FAILED.value})


# --- Database Schema ---

CREATE_NOTIFICATION_DELIVERIES_TABLE = """
CREATE TABLE IF NOT EXISTS notification_deliveries (
    delivery_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'email',
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    status TEXT NOT NULL DEFAULT 'pending',
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    message_body TEXT NOT NULL,
    ad_id TEXT,
    batch_id TEXT,
    dedup_key TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error_category TEXT,
    error_message TEXT,
    provider TEXT,
    provider_message_id TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    lock_token TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_attempt_at TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    CHECK (status IN ('pending', 'processing', 'delivered', 'retryable_failed', 'permanently_failed')),
    CHECK (attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts)
);
"""

CREATE_NOTIFICATION_ATTEMPTS_TABLE = """
CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES notification_deliveries(delivery_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    lock_token TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    error_category TEXT,
    error_message TEXT,
    provider TEXT,
    provider_message_id TEXT,
    UNIQUE (delivery_id, attempt_number),
    CHECK (attempt_number > 0),
    CHECK (status IN ('processing', 'delivered', 'retryable_failed', 'permanently_failed'))
);
"""

CREATE_NOTIFICATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_client ON notification_deliveries (client_id);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status ON notification_deliveries (status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_notification_attempts_delivery ON notification_delivery_attempts (delivery_id, attempt_number);
CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_deliveries_tenant_dedup ON notification_deliveries (client_id, dedup_key, channel);
"""


def create_notification_tables(conn: Any = None) -> None:
    """Create the notification_deliveries table and indexes."""
    from apps.api.database import get_session

    def _exec(target_conn: Any):
        target_conn.execute(CREATE_NOTIFICATION_DELIVERIES_TABLE)
        target_conn.execute(CREATE_NOTIFICATION_ATTEMPTS_TABLE)
        try:
            target_conn.execute(CREATE_NOTIFICATION_INDEXES)
        except Exception as exc:
            logger.debug("Skipping indexes on mock or restricted connection: %s", exc)

    if conn is not None:
        _exec(conn)
    else:
        with get_session() as session:
            _exec(session)


# --- Error Sanitization & Privacy Helpers ---

def mask_recipient(recipient: str) -> str:
    """Mask email or phone for safe logging without exposing PII."""
    if not recipient:
        return "[empty]"
    if "@" in recipient:
        parts = recipient.split("@", 1)
        user, domain = parts[0], parts[1]
        if len(user) <= 2:
            masked_user = user[0] + "*"
        else:
            masked_user = user[0] + "***" + user[-1]
        return f"{masked_user}@{domain}"
    if len(recipient) > 4:
        return recipient[:2] + "****" + recipient[-2:]
    return "****"


def sanitize_error(exc: Exception | str) -> tuple[str, str]:
    """Classify an error into a safe category and sanitized summary without credentials."""
    msg = str(exc)
    low = msg.lower()

    if "unconfigured" in low or "smtp_host" in low or "notification_from_email" in low or "forbidden" in low or "must explicitly select" in low:
        return "unconfigured_credentials", "Required delivery provider credentials not configured"
    if "recipient" in low or "invalid address" in low or "syntax" in low:
        return "invalid_recipient", "Recipient address rejected by format or policy check"
    if "timeout" in low or "timed out" in low:
        return "connection_timeout", "Connection to delivery provider timed out"
    if "refused" in low or "connection reset" in low or "network unreachable" in low:
        return "connection_error", "Network or connection error reaching delivery provider"
    if "simulated temporary" in low:
        return "temporary_provider_error", "Simulated transient provider failure"
    if "simulated permanent" in low:
        return "permanent_provider_error", "Simulated permanent provider rejection"
    if "rate limit" in low or "429" in low:
        return "rate_limited", "Delivery provider rate limit encountered"
    if "authentication" in low or "login" in low or "535" in low:
        return "provider_auth_error", "Authentication error communicating with provider"

    # Default sanitized classification
    return "provider_error", "Delivery provider returned an error"


def is_retryable_error_category(category: str) -> bool:
    """Determine if an error category qualifies for automatic retry."""
    return category in {
        "connection_timeout",
        "connection_error",
        "temporary_provider_error",
        "rate_limited",
        "provider_error",
    }


# --- Exponential Backoff Policy ---

@dataclass
class DeliveryBackoffPolicy:
    """Exponential backoff policy with jitter for retrying delivery attempts."""
    initial_backoff: float = 2.0
    max_backoff: float = 60.0
    backoff_factor: float = 2.0
    jitter: str = "full"

    def calculate_delay(self, attempt: int) -> float:
        """Calculate retry delay in seconds for attempt count (1-based)."""
        base = min(
            self.max_backoff,
            self.initial_backoff * (self.backoff_factor ** max(0, attempt - 1)),
        )
        if self.jitter == "none":
            return base
        if self.jitter == "equal":
            half = base / 2.0
            return half + random.uniform(0.0, half)
        return random.uniform(0.1, base)


DEFAULT_BACKOFF_POLICY = DeliveryBackoffPolicy()


def configured_max_attempts() -> int:
    try:
        value = int(os.environ.get("NOTIFICATION_MAX_ATTEMPTS", "3"))
    except ValueError as exc:
        raise RuntimeError("NOTIFICATION_MAX_ATTEMPTS must be a positive integer") from exc
    if value < 1:
        raise RuntimeError("NOTIFICATION_MAX_ATTEMPTS must be a positive integer")
    return value


DEFAULT_NOTIFICATION_RETENTION_DAYS = 30


def configured_retention_days() -> int:
    try:
        value = int(os.environ.get("NOTIFICATION_RETENTION_DAYS", str(DEFAULT_NOTIFICATION_RETENTION_DAYS)))
    except ValueError as exc:
        raise RuntimeError("NOTIFICATION_RETENTION_DAYS must be a positive integer") from exc
    if value < 1:
        raise RuntimeError("NOTIFICATION_RETENTION_DAYS must be a positive integer")
    return value


def configured_backoff_policy() -> DeliveryBackoffPolicy:
    try:
        initial = float(os.environ.get("NOTIFICATION_RETRY_BACKOFF_SECONDS", "2.0"))
    except ValueError as exc:
        raise RuntimeError("NOTIFICATION_RETRY_BACKOFF_SECONDS must be positive") from exc
    if initial <= 0:
        raise RuntimeError("NOTIFICATION_RETRY_BACKOFF_SECONDS must be positive")
    return DeliveryBackoffPolicy(initial_backoff=initial)


# --- Delivery Sinks ---

class BaseDeliverySink:
    """Abstract base class for alert delivery sinks."""

    def deliver(
        self,
        *,
        recipient: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        delivery_id: str | None = None,
        channel: str = "email",
    ) -> dict[str, Any]:
        raise NotImplementedError


class DevelopmentDeliverySink(BaseDeliverySink):
    """Development/testing sink that safely records deliveries in memory without external side effects."""

    def __init__(self) -> None:
        self.deliveries: list[dict[str, Any]] = []
        self._forced_outcome: Optional[str] = None  # None, "delivered", "retryable_failed", "permanently_failed"
        self._forced_error: Optional[str] = None
        self.simulate_ack_loss: bool = False

    def clear(self) -> None:
        self.deliveries.clear()
        self._forced_outcome = None
        self._forced_error = None
        self.simulate_ack_loss = False

    def set_forced_outcome(self, outcome: str | None, error_message: str | None = None) -> None:
        """Force subsequent deliveries to deterministically succeed or fail."""
        self._forced_outcome = outcome
        self._forced_error = error_message

    def deliver(
        self,
        *,
        recipient: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        delivery_id: str | None = None,
        channel: str = "email",
    ) -> dict[str, Any]:
        # Validate recipient
        target = (recipient or "").strip()
        if not target or ("@" not in target and channel == "email"):
            return {
                "success": False,
                "status": "permanently_failed",
                "error_category": "invalid_recipient",
                "error_message": "Invalid or missing recipient address",
                "provider": "dev_sink",
                "provider_message_id": None,
                "simulated": True,
            }

        # Check simulated outcome
        if self._forced_outcome == "retryable_failed":
            return {
                "success": False,
                "status": "retryable_failed",
                "error_category": "temporary_provider_error",
                "error_message": self._forced_error or "Simulated temporary network timeout",
                "provider": "dev_sink",
                "provider_message_id": None,
                "simulated": True,
            }
        if self._forced_outcome == "permanently_failed":
            return {
                "success": False,
                "status": "permanently_failed",
                "error_category": "permanent_provider_error",
                "error_message": self._forced_error or "Simulated permanent recipient rejection",
                "provider": "dev_sink",
                "provider_message_id": None,
            }

        provider_msg_id = f"dev_{uuid.uuid4().hex[:12]}"

        # Record simulated delivery (masked recipient and subject metadata only, no full body PII)
        record = {
            "delivery_id": delivery_id,
            "channel": channel,
            "recipient_masked": mask_recipient(target),
            "subject_sha256": hashlib.sha256(subject.encode("utf-8")).hexdigest(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider_message_id": provider_msg_id,
            "simulated": True,
        }
        self.deliveries.append(record)

        if self.simulate_ack_loss:
            # Message accepted by sink, but caller will experience network disconnection
            return {
                "success": False,
                "status": "retryable_failed",
                "error_category": "connection_timeout",
                "error_message": "Provider accepted message but acknowledgment connection was lost",
                "provider": "dev_sink",
                "provider_message_id": provider_msg_id,
                "simulated": True,
            }

        return {
            "success": True,
            "status": "delivered",
            "provider": "dev_sink",
            "provider_message_id": provider_msg_id,
            "error_category": None,
            "error_message": None,
            "simulated": True,
        }


DEV_SINK = DevelopmentDeliverySink()


class SmtpDeliverySink(BaseDeliverySink):
    """Live SMTP email delivery sink delegating to NotificationDispatcher.send_email."""

    def deliver(
        self,
        *,
        recipient: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        delivery_id: str | None = None,
        channel: str = "email",
    ) -> dict[str, Any]:
        res = NotificationDispatcher.send_email(
            to_email=recipient,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
        if res.get("status") == "sent":
            return {
                "success": True,
                "status": "delivered",
                "provider": res.get("provider") or "smtp",
                "provider_message_id": f"smtp_{uuid.uuid4().hex[:12]}",
                "error_category": None,
                "error_message": None,
            }
        err = res.get("error") or "SMTP dispatch failed"
        cat, safe_msg = sanitize_error(err)
        if res.get("status") == "unconfigured_credentials":
            cat = "unconfigured_credentials"
        retryable = is_retryable_error_category(cat)
        return {
            "success": False,
            "status": "retryable_failed" if retryable else "permanently_failed",
            "error_category": cat,
            "error_message": safe_msg,
            "provider": res.get("provider"),
            "provider_message_id": None,
        }


SMTP_SINK = SmtpDeliverySink()


def get_delivery_sink(sink_name: str | None = None) -> BaseDeliverySink:
    """Resolve the delivery sink with strict production safety enforcement.

    The development sink must be selected explicitly via NOTIFICATION_DELIVERY_SINK=dev.
    Live SMTP must be explicitly selected; an unset or unknown provider fails closed.
    """
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    chosen_sink = (sink_name or os.environ.get("NOTIFICATION_DELIVERY_SINK", "")).strip().lower()

    if chosen_sink in {"dev", "test", "simulated", "mock"}:
        if app_env == "production":
            raise RuntimeError(
                "Development delivery sink is strictly forbidden in production mode. "
                "Configure a live delivery provider or update APP_ENV."
            )
        return DEV_SINK

    if chosen_sink == "smtp":
        return SMTP_SINK
    raise RuntimeError("NOTIFICATION_DELIVERY_SINK must explicitly select dev or smtp")


# --- HTML Template ---

def _render_email_html(
    client_id: str,
    alert_type: str,
    subject: str,
    message_body: str,
    ad_id: str | None = None,
    platform: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Render an escaped, readable alert email."""
    from html import escape

    return f"""<!DOCTYPE html>
<html><body>
<h2>{escape(subject)}</h2>
<p><strong>Alert:</strong> {escape(alert_type.replace("_", " ").title())}</p>
<p><strong>Client:</strong> {escape(client_id)}</p>
<p><strong>Platform:</strong> {escape((platform or "Meta").title())}</p>
<p><strong>Ad:</strong> {escape(ad_id or "N/A")}</p>
<p><strong>Time:</strong> {escape(timestamp or datetime.now(timezone.utc).isoformat())}</p>
<pre>{escape(message_body)}</pre>
</body></html>"""


# --- Core Durable Delivery Functions ---

def record_notification_delivery(
    conn: Any,
    *,
    client_id: str,
    recipient: str,
    subject: str,
    message_body: str,
    event_type: str,
    channel: str = "email",
    severity: str = "warning",
    ad_id: str | None = None,
    batch_id: str | None = None,
    dedup_key: str | None = None,
    max_attempts: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a durable notification record in 'pending' status before delivery begins.

    Enforces deduplication: if a notification with the same (dedup_key, channel) exists,
    returns the existing record without creating a duplicate.
    """
    create_notification_tables(conn)
    max_attempts = max_attempts if max_attempts is not None else configured_max_attempts()
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    # 1. Deduplication check
    if dedup_key:
        try:
            cur = conn.execute(
                """
                SELECT delivery_id, client_id, channel, event_type, severity, status,
                       recipient, subject, ad_id, batch_id, dedup_key, attempt_count,
                       max_attempts, error_category, provider, provider_message_id, created_at
                FROM notification_deliveries
                WHERE client_id = %s AND dedup_key = %s AND channel = %s
                LIMIT 1;
                """,
                (client_id, dedup_key, channel),
            )
            row = cur.fetchone()
            if row:
                return {
                    "delivery_id": str(row[0]),
                    "client_id": str(row[1]),
                    "channel": str(row[2]),
                    "event_type": str(row[3]),
                    "severity": str(row[4]),
                    "status": str(row[5]),
                    "recipient": str(row[6]),
                    "subject": str(row[7]),
                    "ad_id": str(row[8]) if row[8] else None,
                    "batch_id": str(row[9]) if row[9] else None,
                    "dedup_key": str(row[10]),
                    "attempt_count": int(row[11]),
                    "max_attempts": int(row[12]),
                    "error_category": str(row[13]) if row[13] else None,
                    "provider": str(row[14]) if row[14] else None,
                    "provider_message_id": str(row[15]) if row[15] else None,
                    "created_at": row[16].isoformat() if hasattr(row[16], "isoformat") else str(row[16]),
                    "is_duplicate": True,
                }
        except Exception:
            raise RuntimeError("Notification persistence failed") from None

    # 2. Insert new record in pending state
    delivery_id = f"deliv_{uuid.uuid4().hex}"
    meta_json = json.dumps(metadata) if metadata else "{}"
    now = datetime.now(timezone.utc)

    conflict_clause = "ON CONFLICT (client_id, dedup_key, channel) DO NOTHING" if dedup_key else ""
    try:
        inserted = conn.execute(
            f"""
            INSERT INTO notification_deliveries (
                delivery_id, client_id, channel, event_type, severity, status,
                recipient, subject, message_body, ad_id, batch_id, dedup_key,
                attempt_count, max_attempts, metadata, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                0, %s, %s, %s
            )
            {conflict_clause}
            RETURNING delivery_id;
            """,
            (
                delivery_id, client_id, channel, event_type, severity, DeliveryStatus.PENDING.value,
                recipient, subject, message_body, ad_id, batch_id, dedup_key,
                max_attempts, meta_json, now,
            ),
        )
        inserted_row = inserted.fetchone()
        if inserted_row is None and dedup_key:
            existing = conn.execute(
                """SELECT delivery_id, client_id, channel, event_type, severity, status,
                          recipient, subject, ad_id, batch_id, dedup_key, attempt_count,
                          max_attempts, error_category, provider, provider_message_id, created_at
                   FROM notification_deliveries WHERE client_id = %s AND dedup_key = %s AND channel = %s""",
                (client_id, dedup_key, channel),
            ).fetchone()
            if existing:
                return {
                    "delivery_id": str(existing[0]), "client_id": str(existing[1]),
                    "channel": str(existing[2]), "event_type": str(existing[3]),
                    "severity": str(existing[4]), "status": str(existing[5]),
                    "recipient": str(existing[6]), "subject": str(existing[7]),
                    "ad_id": str(existing[8]) if existing[8] else None,
                    "batch_id": str(existing[9]) if existing[9] else None,
                    "dedup_key": str(existing[10]), "attempt_count": int(existing[11]),
                    "max_attempts": int(existing[12]),
                    "error_category": str(existing[13]) if existing[13] else None,
                    "provider": str(existing[14]) if existing[14] else None,
                    "provider_message_id": str(existing[15]) if existing[15] else None,
                    "created_at": existing[16].isoformat() if hasattr(existing[16], "isoformat") else str(existing[16]),
                    "is_duplicate": True,
                }
    except Exception:
        raise RuntimeError("Notification persistence failed") from None

    return {
        "delivery_id": delivery_id,
        "client_id": client_id,
        "channel": channel,
        "event_type": event_type,
        "severity": severity,
        "status": DeliveryStatus.PENDING.value,
        "recipient": recipient,
        "subject": subject,
        "ad_id": ad_id,
        "batch_id": batch_id,
        "dedup_key": dedup_key,
        "attempt_count": 0,
        "max_attempts": max_attempts,
        "created_at": now.isoformat(),
        "is_duplicate": False,
    }


def claim_pending_deliveries(
    conn: Any,
    *,
    batch_size: int = 10,
    lock_token: str | None = None,
) -> list[dict[str, Any]]:
    """Atomically claim pending or retry-ready deliveries using FOR UPDATE SKIP LOCKED."""
    token = lock_token or f"worker_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    sql = """
    SELECT delivery_id, client_id, channel, event_type, severity, recipient,
           subject, message_body, ad_id, batch_id, dedup_key, attempt_count,
           max_attempts, metadata
    FROM notification_deliveries
    WHERE (
        status = 'pending'
        OR (status = 'retryable_failed' AND (next_retry_at IS NULL OR next_retry_at <= %s))
    )
    ORDER BY created_at ASC
    LIMIT %s
    FOR UPDATE SKIP LOCKED;
    """

    claimed = []
    try:
        cur = conn.execute(sql, (now, batch_size))
        rows = cur.fetchall()
        for r in rows:
            did = str(r[0])
            new_attempts = int(r[11]) + 1
            conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'processing',
                    lock_token = %s,
                    last_attempt_at = %s,
                    attempt_count = %s
                WHERE delivery_id = %s;
                """,
                (token, now, new_attempts, did),
            )
            conn.execute(
                """INSERT INTO notification_delivery_attempts
                   (attempt_id, delivery_id, attempt_number, status, lock_token, started_at)
                   VALUES (%s, %s, %s, 'processing', %s, %s)""",
                (f"att_{uuid.uuid4().hex}", did, new_attempts, token, now),
            )
            claimed.append({
                "delivery_id": did,
                "client_id": str(r[1]),
                "channel": str(r[2]),
                "event_type": str(r[3]),
                "severity": str(r[4]),
                "recipient": str(r[5]),
                "subject": (str(r[6])[:32] + "...") if r[6] and len(str(r[6])) > 32 else (str(r[6]) or ""),
            "subject_sanitized": (str(r[6])[:32] + "...") if r[6] and len(str(r[6])) > 32 else (str(r[6]) or ""),
                "message_body": str(r[7]),
                "ad_id": str(r[8]) if r[8] else None,
                "batch_id": str(r[9]) if r[9] else None,
                "dedup_key": str(r[10]) if r[10] else None,
                "attempt_count": new_attempts,
                "max_attempts": int(r[12]),
                "metadata": r[13] if isinstance(r[13], dict) else json.loads(r[13]) if r[13] else {},
                "lock_token": token,
            })
    except Exception:
        raise RuntimeError("Notification work claim failed") from None

    return claimed


def process_delivery(
    conn: Any,
    delivery_id: str,
    *,
    sink: BaseDeliverySink | None = None,
    backoff_policy: DeliveryBackoffPolicy | None = None,
    lock_token: str | None = None,
) -> dict[str, Any]:
    """Execute a single claimed delivery through its sink and record state transition."""
    policy = backoff_policy or configured_backoff_policy()
    now = datetime.now(timezone.utc)

    # 1. Fetch current record
    try:
        cur = conn.execute(
            """
            SELECT delivery_id, client_id, channel, event_type, recipient, subject,
                   message_body, ad_id, batch_id, status, attempt_count, max_attempts
            FROM notification_deliveries
            WHERE delivery_id = %s;
            """,
            (delivery_id,),
        )
        row = cur.fetchone()
    except Exception:
        raise RuntimeError("Notification record lookup failed") from None

    if not row:
        return {"delivery_id": delivery_id, "status": "not_found", "success": False}

    cur_status = str(row[9])
    attempt_count = int(row[10])
    max_attempts = int(row[11])
    claim_token = lock_token or f"worker_{uuid.uuid4().hex[:16]}"

    # 2. Never overwrite terminal states
    if cur_status in TERMINAL_STATES:
        return {
            "delivery_id": delivery_id,
            "status": cur_status,
            "success": cur_status == DeliveryStatus.DELIVERED.value,
            "terminal_protected": True,
        }

    if cur_status == DeliveryStatus.PROCESSING.value and lock_token is None:
        return {"delivery_id": delivery_id, "status": cur_status, "success": False, "claim_required": True}
    if cur_status == DeliveryStatus.PROCESSING.value:
        token_cur = conn.execute(
            "SELECT 1 FROM notification_deliveries WHERE delivery_id = %s AND status = 'processing' AND lock_token = %s",
            (delivery_id, lock_token),
        )
        if token_cur.fetchone() is None:
            return {"delivery_id": delivery_id, "status": cur_status, "success": False, "claim_lost": True}

    # 3. If pending or retryable, transition to processing first
    if cur_status in (DeliveryStatus.PENDING.value, DeliveryStatus.RETRYABLE_FAILED.value):
        attempt_count += 1
        try:
            claimed = conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'processing',
                lock_token = %s,
                    last_attempt_at = %s,
                    attempt_count = %s
            WHERE delivery_id = %s AND status = %s
            RETURNING delivery_id;
                """,
                (claim_token, now, attempt_count, delivery_id, cur_status),
            )
            if claimed.fetchone() is None:
                return {"delivery_id": delivery_id, "status": cur_status, "success": False, "claim_lost": True}
            conn.execute(
                """INSERT INTO notification_delivery_attempts
                   (attempt_id, delivery_id, attempt_number, status, lock_token, started_at)
                   VALUES (%s, %s, %s, 'processing', %s, %s)""",
                (f"att_{uuid.uuid4().hex}", delivery_id, attempt_count, claim_token, now),
            )
        except Exception:
            raise RuntimeError("Notification claim failed") from None

    # 4. Invoke sink
    recipient = str(row[4])
    subject = str(row[5])
    message_body = str(row[6])
    channel = str(row[2])

    try:
        delivery_sink = sink or get_delivery_sink()
        result = delivery_sink.deliver(
            recipient=recipient,
            subject=subject,
            body_text=message_body,
            body_html=_render_email_html(
                client_id=str(row[1]),
                alert_type=str(row[3]),
                subject=subject,
                message_body=message_body,
                ad_id=str(row[7]) if row[7] else None,
            ),
            delivery_id=delivery_id,
            channel=channel,
        )
    except Exception as exc:
        error_cat, error_msg = sanitize_error(exc)
        result = {"success": False, "error_category": error_cat, "error_message": error_msg, "provider": None}

    # 5. Determine next state
    if result.get("success"):
        new_status = DeliveryStatus.DELIVERED.value
        delivered_at = now
        next_retry_at = None
        error_cat = None
        error_msg = None
    else:
        delivered_at = None
        raw_category = result.get("error_category") or "unknown_error"
        error_cat, error_msg = sanitize_error(result.get("error_message") or "Delivery failed")
        if raw_category in {
            "invalid_recipient", "unconfigured_credentials", "connection_timeout", "connection_error",
            "temporary_provider_error", "permanent_provider_error", "rate_limited", "provider_auth_error",
            "provider_error", "worker_timeout", "worker_timeout_exhausted",
        }:
            error_cat = raw_category

        if attempt_count < max_attempts and is_retryable_error_category(error_cat):
            new_status = DeliveryStatus.RETRYABLE_FAILED.value
            delay = policy.calculate_delay(attempt_count)
            next_retry_at = now + timedelta(seconds=delay)
        else:
            new_status = DeliveryStatus.PERMANENTLY_FAILED.value
            next_retry_at = None

    # 6. Persist status transition
    try:
        updated = conn.execute(
            """
            UPDATE notification_deliveries
            SET status = %s,
                error_category = %s,
                error_message = %s,
                provider = %s,
                provider_message_id = %s,
                delivered_at = %s,
                next_retry_at = %s,
                attempt_count = %s,
                lock_token = NULL
            WHERE delivery_id = %s AND status = 'processing' AND lock_token = %s
            RETURNING delivery_id;
            """,
            (
                new_status,
                error_cat,
                error_msg,
                result.get("provider"),
                result.get("provider_message_id"),
                delivered_at,
                next_retry_at,
                attempt_count,
                delivery_id,
                claim_token,
            ),
        )
        if updated.fetchone() is None:
            return {"delivery_id": delivery_id, "status": "processing", "success": False, "claim_lost": True}
        conn.execute(
            """UPDATE notification_delivery_attempts
               SET status = %s, finished_at = %s, error_category = %s, error_message = %s,
                   provider = %s, provider_message_id = %s
               WHERE delivery_id = %s AND attempt_number = %s AND lock_token = %s AND status = 'processing'""",
            (new_status, now, error_cat, error_msg, result.get("provider"), result.get("provider_message_id"),
             delivery_id, attempt_count, claim_token),
        )
    except Exception:
        raise RuntimeError("Notification status update failed") from None

    return {
        "delivery_id": delivery_id,
        "status": new_status,
        "success": new_status == DeliveryStatus.DELIVERED.value,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "error_category": error_cat,
        "provider": result.get("provider"),
        "provider_message_id": result.get("provider_message_id"),
        "simulated": bool(result.get("simulated") or result.get("provider") == "dev_sink"),
        "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
    }


def recover_stale_processing(
    conn: Any,
    *,
    stale_timeout_seconds: float = 300.0,
) -> list[str]:
    """Detect delivery records stuck in 'processing' past the timeout and transition them safely."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_timeout_seconds)
    recovered_ids = []

    sql = """
    SELECT delivery_id, attempt_count, max_attempts
    FROM notification_deliveries
    WHERE status = 'processing' AND last_attempt_at <= %s
    FOR UPDATE SKIP LOCKED;
    """

    try:
        cur = conn.execute(sql, (cutoff,))
        rows = cur.fetchall()
        for r in rows:
            did = str(r[0])
            attempts = int(r[1])
            max_att = int(r[2])

            if attempts < max_att:
                # Recover back to retryable
                conn.execute(
                    """
                    UPDATE notification_deliveries
                    SET status = 'retryable_failed',
                        error_category = 'worker_timeout',
                        error_message = 'Worker interrupted or stalled while processing delivery',
                        lock_token = NULL,
                        next_retry_at = NOW()
                    WHERE delivery_id = %s;
                    """,
                    (did,),
                )
                conn.execute(
                    """UPDATE notification_delivery_attempts SET status = 'retryable_failed',
                       finished_at = NOW(), error_category = 'worker_timeout',
                       error_message = 'Worker interrupted or stalled while processing delivery'
                       WHERE delivery_id = %s AND attempt_number = %s AND status = 'processing'""",
                    (did, attempts),
                )
            else:
                # Retries exhausted
                conn.execute(
                    """
                    UPDATE notification_deliveries
                    SET status = 'permanently_failed',
                        error_category = 'worker_timeout_exhausted',
                        error_message = 'Worker stalled and maximum delivery attempts exhausted',
                        lock_token = NULL
                    WHERE delivery_id = %s;
                    """,
                    (did,),
                )
                conn.execute(
                    """UPDATE notification_delivery_attempts SET status = 'permanently_failed',
                       finished_at = NOW(), error_category = 'worker_timeout_exhausted',
                       error_message = 'Worker stalled and maximum delivery attempts exhausted'
                       WHERE delivery_id = %s AND attempt_number = %s AND status = 'processing'""",
                    (did, attempts),
                )
            recovered_ids.append(did)
    except Exception:
        raise RuntimeError("Stale notification recovery failed") from None

    return recovered_ids


def process_pending_notifications(
    *,
    batch_size: int = 10,
    sink: BaseDeliverySink | None = None,
) -> list[dict[str, Any]]:
    """Worker entry point: claim and process a batch of pending/due deliveries."""
    from apps.api.database import get_session

    # Commit claims before contacting a sink. If the worker exits mid-attempt, stale
    # processing rows can be recovered and retried by a later worker pass.
    with get_session() as session:
        recover_stale_processing(session)
        claimed = claim_pending_deliveries(session, batch_size=batch_size)

    results = []
    for item in claimed:
        with get_session() as session:
            results.append(process_delivery(session, item["delivery_id"], sink=sink, lock_token=item["lock_token"]))
    return results


# --- Retention & Data Lifecycle Management ---

def preview_expired_notifications(
    conn: Any,
    *,
    retention_days: int | None = None,
    cutoff: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Preview eligible old terminal notification records without modifying the database.

    Safety constraints:
    - Only records in terminal states ('delivered', 'permanently_failed') are eligible.
    - Pending, processing, and retryable records are strictly excluded.
    - Sensitive message bodies and unmasked recipient addresses are never exposed.
    """
    days = retention_days if retention_days is not None else configured_retention_days()
    cutoff_time = cutoff if cutoff is not None else datetime.now(timezone.utc) - timedelta(days=days)

    sql = """
    SELECT delivery_id, client_id, channel, event_type, status,
           recipient, subject, created_at, delivered_at, last_attempt_at
    FROM notification_deliveries
    WHERE status IN ('delivered', 'permanently_failed')
      AND COALESCE(delivered_at, last_attempt_at, created_at) <= %s
    ORDER BY created_at ASC
    LIMIT %s;
    """
    cur = conn.execute(sql, (cutoff_time, limit))
    rows = cur.fetchall()
    previews = []
    for r in rows:
        previews.append({
            "delivery_id": str(r[0]),
            "client_id": str(r[1]),
            "channel": str(r[2]),
            "event_type": str(r[3]),
            "status": str(r[4]),
            "recipient_masked": mask_recipient(str(r[5])),
            "subject": (str(r[6])[:32] + "...") if r[6] and len(str(r[6])) > 32 else (str(r[6]) or ""),
            "subject_sanitized": (str(r[6])[:32] + "...") if r[6] and len(str(r[6])) > 32 else (str(r[6]) or ""),
            "created_at": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]),
            "delivered_at": r[8].isoformat() if r[8] and hasattr(r[8], "isoformat") else (str(r[8]) if r[8] else None),
            "last_attempt_at": r[9].isoformat() if r[9] and hasattr(r[9], "isoformat") else (str(r[9]) if r[9] else None),
        })
    return previews


def purge_expired_notifications(
    conn: Any,
    *,
    retention_days: int | None = None,
    cutoff: datetime | None = None,
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Purge old terminal notification records in bounded batches.

    Safety constraints:
    - Only records in terminal states ('delivered', 'permanently_failed') are eligible.
    - Active ('pending', 'processing', 'retryable_failed') records are NEVER purged.
    - Associated 'notification_delivery_attempts' records are cleaned up via ON DELETE CASCADE.
    - If dry_run=True, returns the count and IDs of eligible records without deleting.
    - Bounded by batch_size to prevent large table locks.
    - Sensitive message bodies and unmasked recipients are never emitted to logs.
    """
    days = retention_days if retention_days is not None else configured_retention_days()
    cutoff_time = cutoff if cutoff is not None else datetime.now(timezone.utc) - timedelta(days=days)

    select_sql = """
    SELECT delivery_id
    FROM notification_deliveries
    WHERE status IN ('delivered', 'permanently_failed')
      AND COALESCE(delivered_at, last_attempt_at, created_at) <= %s
    ORDER BY created_at ASC
    LIMIT %s
    FOR UPDATE SKIP LOCKED;
    """
    cur = conn.execute(select_sql, (cutoff_time, batch_size))
    candidate_ids = [str(r[0]) for r in cur.fetchall()]

    if dry_run or not candidate_ids:
        return {
            "dry_run": dry_run,
            "eligible_count": len(candidate_ids),
            "purged_count": 0,
            "purged_delivery_ids": candidate_ids if dry_run else [],
            "cutoff": cutoff_time.isoformat(),
            "retention_days": days,
        }

    delete_sql = """
    DELETE FROM notification_deliveries
    WHERE delivery_id = ANY(%s)
      AND status IN ('delivered', 'permanently_failed')
    RETURNING delivery_id;
    """
    deleted_cur = conn.execute(delete_sql, (candidate_ids,))
    deleted_ids = [str(r[0]) for r in deleted_cur.fetchall()]

    logger.info("Purged %d expired notification deliveries older than %s", len(deleted_ids), cutoff_time.isoformat())

    return {
        "dry_run": False,
        "eligible_count": len(candidate_ids),
        "purged_count": len(deleted_ids),
        "purged_delivery_ids": deleted_ids,
        "cutoff": cutoff_time.isoformat(),
        "retention_days": days,
    }


# --- Public Dispatcher Interface (Preserves Existing Contracts) ---

class NotificationDispatcher:
    """Deliver campaign alerts through durable records and configurable sinks."""

    @classmethod
    def send_email(
        cls,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> dict[str, Any]:
        """Direct SMTP email dispatch helper (preserved for backward compatibility)."""
        target_email = (to_email or "").strip()
        if not target_email or "@" not in target_email:
            return {
                "status": "failed",
                "provider": None,
                "error": "Invalid or missing recipient email address",
            }

        smtp_host = os.environ.get("SMTP_HOST", "").strip()
        if not smtp_host:
            return {
                "status": "unconfigured_credentials",
                "provider": None,
                "error": "SMTP_HOST is not configured in environment",
            }

        try:
            smtp_port = int(os.environ.get("SMTP_PORT", "587").strip())
        except ValueError:
            return {
                "status": "failed",
                "provider": "smtp",
                "error": "SMTP_PORT must be an integer",
            }

        from_email = (
            os.environ.get("NOTIFICATION_FROM_EMAIL", "").strip()
            or os.environ.get("SMTP_FROM_EMAIL", "").strip()
        )
        if not from_email or "@" not in from_email:
            return {
                "status": "unconfigured_credentials",
                "provider": "smtp",
                "error": "NOTIFICATION_FROM_EMAIL is not configured in environment",
            }

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = target_email
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))

        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.ehlo()
                if os.environ.get("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes"}:
                    server.starttls()
                    server.ehlo()
                smtp_user = os.environ.get("SMTP_USER", "").strip()
                smtp_password = os.environ.get("SMTP_PASSWORD", "")
                if smtp_user or smtp_password:
                    if not smtp_user or not smtp_password:
                        return {
                            "status": "unconfigured_credentials",
                            "provider": "smtp",
                            "error": "SMTP_USER and SMTP_PASSWORD must be configured together",
                        }
                    server.login(smtp_user, smtp_password)
                server.sendmail(from_email, [target_email], msg.as_string())
            return {"status": "sent", "provider": "smtp"}
        except Exception as exc:
            cat, safe_msg = sanitize_error(exc)
            logger.warning("SMTP email dispatch failed to %s: %s (%s)", mask_recipient(target_email), cat, safe_msg)
            return {"status": "failed", "provider": "smtp", "error": str(exc)}

    @classmethod
    def dispatch_campaign_alert(
        cls,
        client_id: str,
        recipient_email: str,
        recipient_phone: str,
        alert_type: str,
        subject: str,
        message_body: str,
        ad_id: str | None = None,
        batch_id: str | None = None,
        platform: str | None = None,
        launch_attempt_id: str | None = None,
        event_id: str | None = None,
        dedup_key: str | None = None,
        conn: Any | None = None,
        sink: BaseDeliverySink | None = None,
    ) -> dict[str, Any]:
        """Persist a durable notification record and queue the alert for worker delivery.

        Maintains full backward compatibility with the existing return dictionary shape.
        """
        from apps.api.database import get_session

        timestamp = datetime.now(timezone.utc).isoformat()
        recipient_hash = hashlib.sha256(recipient_email.encode("utf-8")).hexdigest()[:16]

        if dedup_key:
            stable_dedup = dedup_key
        else:
            # Build structured target identifier
            target_parts = []
            if batch_id:
                target_parts.append(f"batch:{batch_id}")
            if ad_id:
                target_parts.append(f"ad:{ad_id}")
            if platform:
                target_parts.append(f"platform:{platform}")
            if launch_attempt_id:
                target_parts.append(f"attempt:{launch_attempt_id}")

            if target_parts:
                target_scope = ":".join(target_parts)
            elif event_id:
                target_scope = f"event:{event_id}"
            else:
                # Content hash when no batch, ad, or event identifier exists.
                # Retries of the exact same message will match, while distinct events with
                # different subjects/messages will not collide into a shared key.
                content_sig = hashlib.sha256(f"{subject}\n{message_body}".encode("utf-8")).hexdigest()[:16]
                target_scope = f"msg:{content_sig}"

            stable_dedup = f"{client_id}:{alert_type}:{target_scope}:{recipient_hash}"

        def _handle(active_conn: Any) -> dict[str, Any]:
            # 1. Persist durable record (with dedup check)
            rec = record_notification_delivery(
                active_conn,
                client_id=client_id,
                recipient=recipient_email,
                subject=subject,
                message_body=message_body,
                event_type=alert_type,
                channel="email",
                severity="error" if "block" in alert_type or "rejection" in alert_type else "warning",
                ad_id=ad_id,
                batch_id=batch_id,
                dedup_key=stable_dedup,
                metadata={
                    "platform": platform,
                    "batch_id": batch_id,
                    "launch_attempt_id": launch_attempt_id,
                    "event_id": event_id,
                },
            )

            delivery_id = rec["delivery_id"]

            # If duplicate was already delivered or in flight, return existing status idempotently
            if rec.get("is_duplicate"):
                existing_status = rec.get("status")
                email_status = "queued" if existing_status not in TERMINAL_STATES else existing_status
                return {
                    "email_status": email_status,
                    "timestamp": timestamp,
                    "delivery_id": delivery_id,
                    "status": existing_status,
                    "duplicate_suppressed": True,
                    "details": {
                        "client_id": client_id,
                        "batch_id": batch_id,
                        "ad_id": ad_id,
                        "platform": platform,
                        "alert_type": alert_type,
                        "recipient_email": recipient_email,
                        "recipient_phone": recipient_phone,
                        "email_provider": rec.get("provider"),
                        "email_error": None,
                    },
                }

            return {
                "email_status": "queued",
                "timestamp": timestamp,
                "delivery_id": delivery_id,
                "status": DeliveryStatus.PENDING.value,
                "attempt_count": 0,
                "details": {
                    "client_id": client_id,
                    "batch_id": batch_id,
                    "ad_id": ad_id,
                    "platform": platform,
                    "alert_type": alert_type,
                    "recipient_email": recipient_email,
                    "recipient_phone": recipient_phone,
                    "email_provider": None,
                    "email_error": None,
                },
            }

        try:
            if conn is not None:
                return _handle(conn)
            with get_session() as session:
                return _handle(session)
        except Exception:
            logger.warning("Notification could not be durably queued")
            return {
                "email_status": "failed",
                "status": "persistence_failed",
                "timestamp": timestamp,
                "error": "Notification could not be durably queued",
            }


# --- Retention Maintenance Router ---

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from apps.api.auth import CurrentUser, get_current_user, require_role

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/delivery-status")
def get_notification_delivery_status(
    client_id: str | None = None,
    batch_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Return delivery outcomes for the user's clients without exposing recipients or message bodies."""
    from apps.api.access import require_client_access
    from apps.api.database import get_session

    if client_id:
        require_client_access(user, client_id)
    clients = [client_id] if client_id else list(user.assigned_client_ids)
    with get_session() as conn:
        create_notification_tables(conn)
        counts = conn.execute(
            """
            SELECT status, COUNT(*)
            FROM notification_deliveries
            WHERE client_id = ANY(%s) AND (%s::text IS NULL OR batch_id = %s)
            GROUP BY status;
            """,
            (clients, batch_id, batch_id),
        ).fetchall()
        recent = conn.execute(
            """
            SELECT delivery_id, client_id, batch_id, event_type, subject, status,
                   error_category, provider, attempt_count, max_attempts, created_at, delivered_at
            FROM notification_deliveries
            WHERE client_id = ANY(%s) AND (%s::text IS NULL OR batch_id = %s)
            ORDER BY created_at DESC
            LIMIT %s;
            """,
            (clients, batch_id, batch_id, limit),
        ).fetchall()

    by_status = {str(status): int(count) for status, count in counts}
    for status_name in ("pending", "processing", "delivered", "retryable_failed", "permanently_failed"):
        by_status.setdefault(status_name, 0)
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "recent": [
            {
                "delivery_id": str(row[0]),
                "client_id": str(row[1]),
                "batch_id": str(row[2]) if row[2] else None,
                "event_type": str(row[3]),
                "subject": str(row[4]),
                "status": str(row[5]),
                "error_category": str(row[6]) if row[6] else None,
                "provider": str(row[7]) if row[7] else None,
                "attempt_count": int(row[8]),
                "max_attempts": int(row[9]),
                "created_at": row[10].isoformat() if hasattr(row[10], "isoformat") else str(row[10]),
                "delivered_at": row[11].isoformat() if hasattr(row[11], "isoformat") else str(row[11]) if row[11] else None,
            }
            for row in recent
        ],
    }


class RetentionMaintenanceRequest(BaseModel):
    client_id: str | None = None
    retention_days: int | None = Field(default=None, ge=1)
    batch_size: int = Field(default=500, ge=1, le=5000)
    dry_run: bool = True


@router.post("/retention", status_code=status.HTTP_200_OK)
def manage_notification_retention(
    req: RetentionMaintenanceRequest,
    user: CurrentUser = Depends(require_role("buyer", "account_manager")),
) -> dict[str, Any]:
    """Execute or preview bounded retention purge of terminal notification records.

    Safety:
    - Restricted to buyer/account_manager roles.
    - Multi-tenant client scoping enforced when client_id specified.
    - Never deletes pending, processing, or retryable notifications.
    - Transaction safe with FOR UPDATE SKIP LOCKED.
    """
    from apps.api.access import require_client_access
    from apps.api.database import get_session

    if req.client_id:
        require_client_access(user, req.client_id)

    with get_session() as conn:
        result = purge_expired_notifications(
            conn,
            retention_days=req.retention_days,
            batch_size=req.batch_size,
            dry_run=req.dry_run,
        )
        if not req.dry_run and hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
            conn.commit()

    logger.info(
        "Retention maintenance executed: dry_run=%s, eligible_count=%s, purged_count=%s by user=%s",
        req.dry_run,
        result.get("eligible_count"),
        result.get("purged_count"),
        user.user_id,
    )
    return result
