"""Comprehensive tests for PIISanitizationMiddleware.

Verifies:
1. High-risk PII sanitization in JSON bodies (credit cards, SSNs, secret keys).
2. Query parameter scanning and sanitization.
3. Whitelisting of '/auth/login' to ensure authentication credentials remain intact.
4. Whitelisting of employee contact fields ('employee_email', 'employee_phone', 'email', 'phone')
   on campaign-flow endpoints ('/campaign-flow/launch-batch', '/campaign-flow/hitl-remediate').
5. Safe bypass of streaming multipart uploads ('/uploads' and evidence files) to prevent stream corruption.
6. Recording of detection telemetry on 'request.state.pii_detections'.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient

from apps.api import auth, main
from apps.api.main import app
from apps.api.middleware_pii import (
    PIIDetectionItem,
    PIISanitizationMiddleware,
    sanitize_high_risk_text,
    sanitize_json_payload,
    sanitize_query_string,
)


@pytest.fixture(autouse=True)
def configure_auth_and_schema(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-key-32-characters-long")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")


# ---------------------------------------------------------------------------
# 1. Unit tests: sanitize_high_risk_text
# ---------------------------------------------------------------------------

def test_sanitize_credit_cards():
    detections: list[PIIDetectionItem] = []
    text = "Visa: 4111-2222-3333-4444, Space: 4111 2222 3333 4444, Raw: 4111222233334444, Amex: 3782-822463-10005"
    sanitized = sanitize_high_risk_text(text, "test_field", detections)

    assert "[REDACTED_PAYMENT_CARD]" in sanitized
    assert "4111" not in sanitized
    assert "3782" not in sanitized
    assert len(detections) == 4
    assert all(d.entity_type == "CREDIT_CARD" for d in detections)
    assert all(d.masked_value == "[REDACTED_PAYMENT_CARD]" for d in detections)


def test_sanitize_ssns():
    detections: list[PIIDetectionItem] = []
    text = "Client SSN is 123-45-6789 and secondary is 987-65-4321."
    sanitized = sanitize_high_risk_text(text, "notes", detections)

    assert "[REDACTED_SSN]" in sanitized
    assert "123-45-6789" not in sanitized
    assert "987-65-4321" not in sanitized
    assert len(detections) == 2
    assert all(d.entity_type == "SSN" for d in detections)
    assert all(d.masked_value == "[REDACTED_SSN]" for d in detections)


def test_sanitize_secret_keys():
    detections: list[PIIDetectionItem] = []
    text = "OpenAI: sk-proj-1234567890abcdef12345678, Google: AIzaSyD-1234567890abcdef1234567890abc, Bearer: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcdef12345"
    sanitized = sanitize_high_risk_text(text, "secret_field", detections)

    assert "[REDACTED_SECRET]" in sanitized
    assert "sk-proj" not in sanitized
    assert "AIzaSyD" not in sanitized
    assert len(detections) == 3
    assert all(d.entity_type == "SECRET_KEY" for d in detections)


def test_no_double_redaction():
    detections: list[PIIDetectionItem] = []
    text = "Already clean: [REDACTED_PAYMENT_CARD] and [REDACTED_SECRET] and [REDACTED_SSN]"
    sanitized = sanitize_high_risk_text(text, "field", detections)

    assert sanitized == text
    assert len(detections) == 0


# ---------------------------------------------------------------------------
# 2. Unit tests: sanitize_json_payload and whitelist rules
# ---------------------------------------------------------------------------

def test_sanitize_nested_json():
    detections: list[PIIDetectionItem] = []
    payload = {
        "headline": "Buy now with card 4111-2222-3333-4444!",
        "ad_copy": {
            "body": "Call us, SSN is 123-45-6789",
            "metadata": [
                {"token": "sk-test1234567890abcdef1234"},
                {"safe_count": 42},
            ],
        },
    }

    sanitized = sanitize_json_payload(payload, "", "/ads/create", detections)

    assert sanitized["headline"] == "Buy now with card [REDACTED_PAYMENT_CARD]!"
    assert sanitized["ad_copy"]["body"] == "Call us, SSN is [REDACTED_SSN]"
    assert sanitized["ad_copy"]["metadata"][0]["token"] == "[REDACTED_SECRET]"
    assert sanitized["ad_copy"]["metadata"][1]["safe_count"] == 42
    assert len(detections) == 3


def test_whitelist_contact_fields_on_launch_batch():
    detections: list[PIIDetectionItem] = []
    payload = {
        "client_id": "brightleaf",
        "employee_email": "media.buyer@launchgate.com",
        "employee_phone": "+1 (555) 234-5678",
        "campaign_name": "Summer Blast with card 4111-2222-3333-4444",
    }

    sanitized = sanitize_json_payload(payload, "", "/campaign-flow/launch-batch", detections)

    # Legitimate contacts must remain untouched
    assert sanitized["employee_email"] == "media.buyer@launchgate.com"
    assert sanitized["employee_phone"] == "+1 (555) 234-5678"
    # Unwhitelisted fields must be sanitized
    assert sanitized["campaign_name"] == "Summer Blast with card [REDACTED_PAYMENT_CARD]"
    assert len(detections) == 1
    assert detections[0].field == "campaign_name"


# ---------------------------------------------------------------------------
# 3. Unit tests: sanitize_query_string
# ---------------------------------------------------------------------------

def test_sanitize_query_string():
    detections: list[PIIDetectionItem] = []
    raw_query = b"search=card+4111-2222-3333-4444&tag=summer"
    sanitized_bytes, modified = sanitize_query_string(raw_query, "/ads", detections)

    assert modified is True
    assert b"%5BREDACTED_PAYMENT_CARD%5D" in sanitized_bytes or b"[REDACTED_PAYMENT_CARD]" in sanitized_bytes
    assert b"4111" not in sanitized_bytes
    assert b"tag=summer" in sanitized_bytes
    assert len(detections) == 1
    assert detections[0].field == "query:search"


# ---------------------------------------------------------------------------
# 4. Integration tests with FastAPI App
# ---------------------------------------------------------------------------

def test_auth_login_whitelisted_credentials_untouched(monkeypatch: pytest.MonkeyPatch):
    """Ensure passwords containing high-entropy/secret-like characters are NOT altered before hashing."""
    test_password = "sk-special-pass-4111-2222-3333-4444!"
    user_record = {
        "user_id": "buyer-1",
        "email": "buyer@launchgate.com",
        "display_name": "Test Buyer",
        "password_hash": auth.hash_password(test_password),
        "role": "buyer",
        "is_active": True,
    }
    monkeypatch.setattr(auth.users, "get_user_by_email", lambda email: user_record)

    with TestClient(app) as client:
        res = client.post(
            "/auth/login",
            json={"email": user_record["email"], "password": test_password},
        )

    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_pii_sanitization_in_json_request_body():
    """Verify that incoming JSON bodies with PII are sanitized before reaching endpoint handlers."""
    test_app = FastAPI()
    test_app.add_middleware(PIISanitizationMiddleware)

    captured_data = {}
    captured_detections = []

    @test_app.post("/test-ad")
    async def handle_ad(request: Request):
        captured_data.update(await request.json())
        captured_detections.extend(request.state.pii_detections)
        return {"status": "ok"}

    client = TestClient(test_app)
    res = client.post(
        "/test-ad",
        json={
            "headline": "Special discount with card 4111-2222-3333-4444!",
            "secret_note": "Internal key: sk-proj-1234567890abcdef12345678",
            "ssn_note": "ID: 123-45-6789",
        },
    )

    assert res.status_code == 200
    assert captured_data["headline"] == "Special discount with card [REDACTED_PAYMENT_CARD]!"
    assert captured_data["secret_note"] == "Internal key: [REDACTED_SECRET]"
    assert captured_data["ssn_note"] == "ID: [REDACTED_SSN]"
    assert len(captured_detections) == 3


def test_query_parameter_sanitization_via_middleware():
    """Verify query parameters are sanitized and recorded in request.state."""
    test_app = FastAPI()
    test_app.add_middleware(PIISanitizationMiddleware)

    captured_query = {}
    captured_detections = []

    @test_app.get("/search-ads")
    async def search_ads(request: Request, query: str = ""):
        captured_query["query"] = query
        captured_detections.extend(request.state.pii_detections)
        return {"result": query}

    client = TestClient(test_app)
    res = client.get("/search-ads?query=filter+by+card+4111222233334444")

    assert res.status_code == 200
    assert "[REDACTED_PAYMENT_CARD]" in captured_query["query"]
    assert "4111222233334444" not in captured_query["query"]
    assert len(captured_detections) == 1
    assert captured_detections[0]["entity_type"] == "CREDIT_CARD"


def test_campaign_flow_launch_batch_preserves_contacts_while_scrubbing_other_pii():
    """Verify contact notification fields are preserved on /campaign-flow/launch-batch."""
    test_app = FastAPI()
    test_app.add_middleware(PIISanitizationMiddleware)

    captured_body = {}

    @test_app.post("/campaign-flow/launch-batch")
    async def launch_batch(request: Request):
        captured_body.update(await request.json())
        return {"status": "dispatched"}

    client = TestClient(test_app)
    res = client.post(
        "/campaign-flow/launch-batch",
        json={
            "client_id": "brightleaf",
            "employee_email": "notifications@agency.com",
            "employee_phone": "+1-800-555-0199",
            "campaign_name": "Growth Burst sk-proj-1234567890abcdef12345678",
        },
    )

    assert res.status_code == 200
    assert captured_body["employee_email"] == "notifications@agency.com"
    assert captured_body["employee_phone"] == "+1-800-555-0199"
    assert captured_body["campaign_name"] == "Growth Burst [REDACTED_SECRET]"


def test_multipart_file_upload_bypassed_without_corruption():
    """Verify multipart uploads to /uploads or evidence endpoints bypass body reading."""
    test_app = FastAPI()
    test_app.add_middleware(PIISanitizationMiddleware)

    captured_file_content = {}

    @test_app.post("/uploads")
    async def upload_file(request: Request):
        form = await request.form()
        uploaded_file = form.get("file")
        if uploaded_file:
            content = await uploaded_file.read()
            captured_file_content["content"] = content
        return {"status": "received", "detections": len(request.state.pii_detections)}

    client = TestClient(test_app)
    csv_bytes = b"headline,ad_text,destination_url\nPromo,Special 4111-2222-3333-4444,https://example.com"
    res = client.post(
        "/uploads",
        files={"file": ("batch.csv", io.BytesIO(csv_bytes), "text/csv")},
    )

    assert res.status_code == 200
    # Stream bytes must not be corrupted or altered during multipart stream
    assert captured_file_content["content"] == csv_bytes
    assert res.json()["detections"] == 0
