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
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NOTIFICATION_FROM_EMAIL", "alerts@example.com")

    with patch.object(
        NotificationDispatcher,
        "send_email",
        return_value={"status": "sent", "provider": "smtp"},
    ):
        result = NotificationDispatcher.dispatch_campaign_alert(
            client_id="client",
            recipient_email="buyer@example.com",
            recipient_phone="+15551234567",
            alert_type="launch_blocked",
            subject="Launch blocked",
            message_body="Connector unavailable",
        )

    assert result["email_status"] == "sent"
    assert "sms_status" not in result
    assert result["details"]["recipient_phone"] == "+15551234567"
