"""SMTP-only campaign alert delivery."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


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


class NotificationDispatcher:
    """Deliver campaign alerts through SMTP email only."""

    @classmethod
    def send_email(
        cls,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> dict[str, Any]:
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
                "error": "SMTP_HOST is not configured",
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
                "error": "NOTIFICATION_FROM_EMAIL is not configured",
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
            logger.exception("SMTP email dispatch failed to %s", target_email)
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
        platform: str | None = None,
    ) -> dict[str, Any]:
        """Send one SMTP alert and retain phone only as contact metadata."""
        timestamp = datetime.now(timezone.utc).isoformat()
        body_text = "\n".join(
            [
                f"LAUNCH GATE CAMPAIGN ALERT — {alert_type.upper()}",
                f"Client: {client_id}",
                f"Platform: {(platform or 'Meta').title()}",
                f"Ad ID: {ad_id or 'N/A'}",
                f"Timestamp: {timestamp}",
                "",
                message_body,
            ]
        )
        try:
            email_result = cls.send_email(
                to_email=recipient_email,
                subject=subject,
                body_text=body_text,
                body_html=_render_email_html(
                    client_id, alert_type, subject, message_body, ad_id, platform, timestamp
                ),
            )
            return {
                "email_status": email_result.get("status", "failed"),
                "timestamp": timestamp,
                "details": {
                    "client_id": client_id,
                    "ad_id": ad_id,
                    "platform": platform,
                    "alert_type": alert_type,
                    "recipient_email": recipient_email,
                    "recipient_phone": recipient_phone,
                    "email_provider": email_result.get("provider"),
                    "email_error": email_result.get("error"),
                },
            }
        except Exception as exc:
            logger.exception("Campaign alert dispatch failed")
            return {
                "email_status": "failed",
                "timestamp": timestamp,
                "details": {"client_id": client_id, "error": str(exc)},
            }
