"""Tests for SMTP-only campaign alert delivery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apps.api.notifications import NotificationDispatcher


def test_email_requires_smtp_configuration(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("NOTIFICATION_FROM_EMAIL", raising=False)

    result = NotificationDispatcher.send_email(
        "buyer@example.com", "Alert", "Body"
    )

    assert result["status"] == "unconfigured_credentials"
    assert result["provider"] is None


def test_email_rejects_invalid_recipient(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NOTIFICATION_FROM_EMAIL", "alerts@example.com")

    result = NotificationDispatcher.send_email("invalid", "Alert", "Body")

    assert result["status"] == "failed"
    assert "recipient" in result["error"].lower()


def test_email_sends_through_smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-password")
    monkeypatch.setenv("SMTP_USE_TLS", "true")
    monkeypatch.setenv("NOTIFICATION_FROM_EMAIL", "alerts@example.com")
    server = MagicMock()
    server.__enter__.return_value = server

    with patch("apps.api.notifications.smtplib.SMTP", return_value=server):
        result = NotificationDispatcher.send_email(
            "buyer@example.com", "Alert", "Body", "<p>Body</p>"
        )

    assert result == {"status": "sent", "provider": "smtp"}
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("smtp-user", "smtp-password")
    server.sendmail.assert_called_once()


def test_alert_dispatch_has_email_status_only(monkeypatch):
    from contextlib import nullcontext
    from apps.api import notifications

    monkeypatch.setattr("apps.api.database.get_session", lambda: nullcontext(object()))
    monkeypatch.setattr(notifications, "record_notification_delivery", lambda *a, **k: {
        "delivery_id": "delivery-test", "status": "pending", "is_duplicate": False,
    })
    with patch.object(NotificationDispatcher, "send_email") as send_email:
        result = NotificationDispatcher.dispatch_campaign_alert(
            client_id="client",
            recipient_email="buyer@example.com",
            recipient_phone="+15551234567",
            alert_type="launch_blocked",
            subject="Launch blocked",
            message_body="Connector unavailable",
        )

    assert result["email_status"] == "queued"
    assert result["status"] == "pending"
    assert result["delivery_id"] == "delivery-test"
    send_email.assert_not_called()
    assert "sms_status" not in result
    assert result["details"]["recipient_phone"] == "+15551234567"


def test_email_diagnostics_require_explicit_smtp_sink(monkeypatch):
    from apps.api.diagnostics import get_ai_and_tracing_status

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-password")
    monkeypatch.setenv("NOTIFICATION_FROM_EMAIL", "alerts@example.com")
    monkeypatch.setenv("NOTIFICATION_DELIVERY_SINK", "")
    status = get_ai_and_tracing_status()["email_alerts"]
    assert status == {"is_configured": False, "sink": "unset", "status": "sink_not_selected"}

    monkeypatch.setenv("NOTIFICATION_DELIVERY_SINK", "smtp")
    status = get_ai_and_tracing_status()["email_alerts"]
    assert status == {"is_configured": True, "sink": "smtp", "status": "ready"}
