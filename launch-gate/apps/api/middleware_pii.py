"""Global Security and PII Sanitization Middleware.

Intercepts incoming HTTP requests to scan and sanitize sensitive high-risk PII:
- Credit card numbers -> [REDACTED_PAYMENT_CARD]
- SSNs -> [REDACTED_SSN]
- Private secret keys (e.g. sk-..., AIza...) -> [REDACTED_SECRET]

Protects server logs, audit traces, and downstream LLM prompts from accidental leaks.
Preserves functional application fields:
- Endpoint '/auth/login' (passwords preserved unaltered before hashing).
- Contact notification fields ('employee_email', 'employee_phone', 'email', 'phone')
  in '/campaign-flow/launch-batch' and '/campaign-flow/hitl-remediate' (required for SMS/email alerts).
- Bypasses streaming / large multipart uploads (e.g. CSV/XLSX file uploads on '/uploads'
  and evidence files) to ensure file streams are not corrupted or buffered in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any
import urllib.parse

from starlette.middleware.base import BaseHTTPMiddleware

from packages.guards.pii_guard import (
    _CREDIT_CARD_PATTERN,
    _SECRET_KEY_PATTERN,
    _SSN_PATTERN,
)

logger = logging.getLogger(__name__)


@dataclass
class PIIDetectionItem:
    """Record of an intercepted PII entity."""
    entity_type: str
    field: str
    masked_value: str
    original_snippet: str = ""

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def __contains__(self, item: str) -> bool:
        return hasattr(self, item) or item in (self.entity_type, self.masked_value, self.field)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return other == self.entity_type or other == self.masked_value
        if isinstance(other, PIIDetectionItem):
            return (self.entity_type, self.field, self.masked_value) == (
                other.entity_type,
                other.field,
                other.masked_value,
            )
        return False

    def keys(self) -> list[str]:
        return ["entity_type", "field", "masked_value", "original_snippet"]

    def values(self) -> list[Any]:
        return [self.entity_type, self.field, self.masked_value, self.original_snippet]

    def items(self) -> list[tuple[str, Any]]:
        return [(k, getattr(self, k)) for k in self.keys()]


WHITELISTED_ENDPOINTS = {
    "/auth/login",
}

WHITELISTED_CONTACT_PATHS = {
    "/campaign-flow/launch-batch",
    "/campaign-flow/hitl-remediate",
}

WHITELISTED_CONTACT_FIELDS = {
    "employee_email",
    "employee_phone",
    "email",
    "phone",
}


def sanitize_high_risk_text(
    text: str,
    field_name: str,
    detections: list[PIIDetectionItem],
) -> str:
    """Scan text for sensitive PII entities (Secret keys, Credit Cards, SSNs) and replace them."""
    if not text or not isinstance(text, str):
        return text

    modified = text

    def _replace_entity(pattern: re.Pattern, entity_type: str, mask_label: str, target: str) -> str:
        def _repl(match: re.Match) -> str:
            val = match.group(0)
            if val.startswith("[REDACTED_"):
                return val
            if entity_type == "CREDIT_CARD" and len(re.sub(r"\D", "", val)) < 13:
                return val
            snippet = val[:4] + "..." if len(val) > 4 else val
            detections.append(
                PIIDetectionItem(
                    entity_type=entity_type,
                    field=field_name,
                    masked_value=mask_label,
                    original_snippet=snippet,
                )
            )
            return mask_label

        return pattern.sub(_repl, target)

    # 1. Scrub secret keys first
    modified = _replace_entity(_SECRET_KEY_PATTERN, "SECRET_KEY", "[REDACTED_SECRET]", modified)
    # 2. Scrub payment cards
    modified = _replace_entity(_CREDIT_CARD_PATTERN, "CREDIT_CARD", "[REDACTED_PAYMENT_CARD]", modified)
    # 3. Scrub SSNs
    modified = _replace_entity(_SSN_PATTERN, "SSN", "[REDACTED_SSN]", modified)

    return modified


def sanitize_json_payload(
    obj: Any,
    path: str,
    endpoint_path: str,
    detections: list[PIIDetectionItem],
) -> Any:
    """Recursively traverse and sanitize JSON dicts and lists."""
    is_contact_endpoint = endpoint_path.rstrip("/") in WHITELISTED_CONTACT_PATHS

    if isinstance(obj, dict):
        new_dict: dict[str, Any] = {}
        for k, v in obj.items():
            field_path = f"{path}.{k}" if path else k
            # Whitelist contact notification fields on contact endpoints
            if is_contact_endpoint and k.lower() in WHITELISTED_CONTACT_FIELDS:
                new_dict[k] = v
            else:
                new_dict[k] = sanitize_json_payload(v, field_path, endpoint_path, detections)
        return new_dict

    elif isinstance(obj, list):
        return [
            sanitize_json_payload(item, f"{path}[{i}]", endpoint_path, detections)
            for i, item in enumerate(obj)
        ]

    elif isinstance(obj, str):
        return sanitize_high_risk_text(obj, path, detections)

    return obj


def sanitize_query_string(
    raw_query: bytes,
    endpoint_path: str,
    detections: list[PIIDetectionItem],
) -> tuple[bytes, bool]:
    """Sanitize query parameters in URL, returning (new_query_bytes, was_modified)."""
    if not raw_query:
        return raw_query, False

    try:
        decoded_query = raw_query.decode("latin-1")
    except Exception:
        return raw_query, False

    parsed_qsl = urllib.parse.parse_qsl(decoded_query, keep_blank_values=True)
    is_contact_endpoint = endpoint_path.rstrip("/") in WHITELISTED_CONTACT_PATHS

    new_qsl = []
    modified_any = False

    for param, val in parsed_qsl:
        field_name = f"query:{param}"
        if is_contact_endpoint and param.lower() in WHITELISTED_CONTACT_FIELDS:
            new_qsl.append((param, val))
        else:
            initial_count = len(detections)
            sanitized_val = sanitize_high_risk_text(val, field_name, detections)
            if len(detections) > initial_count or sanitized_val != val:
                modified_any = True
            new_qsl.append((param, sanitized_val))

    if modified_any:
        new_query_str = urllib.parse.urlencode(new_qsl)
        return new_query_str.encode("ascii"), True

    return raw_query, False


class PIISanitizationMiddleware(BaseHTTPMiddleware):
    """Starlette Pure ASGI middleware with BaseHTTPMiddleware compatibility for PII sanitization."""

    def __init__(self, app: Any, dispatch: Any = None) -> None:
        super().__init__(app, dispatch=dispatch)
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Ensure request.state dictionary exists
        if "state" not in scope or scope["state"] is None:
            scope["state"] = {}
        detections: list[PIIDetectionItem] = []
        scope["state"]["pii_detections"] = detections

        path = scope.get("path", "")
        clean_path = path.rstrip("/")

        # 1. Whitelist endpoint: /auth/login
        if clean_path in WHITELISTED_ENDPOINTS:
            await self.app(scope, receive, send)
            return

        # 2. Check for streaming/multipart uploads to bypass body processing safely
        headers_dict = {k.lower(): v for k, v in scope.get("headers", [])}
        content_type = headers_dict.get(b"content-type", b"").lower()

        is_multipart_or_stream = (
            b"multipart/form-data" in content_type
            or b"application/octet-stream" in content_type
            or path.startswith("/uploads")
            or "/evidence" in path
        )

        # 3. Sanitize query parameters
        raw_query = scope.get("query_string", b"")
        if raw_query:
            new_query, modified = sanitize_query_string(raw_query, clean_path, detections)
            if modified:
                scope["query_string"] = new_query

        # If upload/streaming or method doesn't carry JSON body, bypass body processing
        method = scope.get("method", "GET").upper()
        if is_multipart_or_stream or method in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        # 4. Check if request body is JSON
        is_json = (
            b"application/json" in content_type
            or b"+json" in content_type
            or not content_type  # Default fallback if client didn't specify header
        )

        if not is_json:
            await self.app(scope, receive, send)
            return

        # Buffer the JSON body
        body_chunks = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if chunk:
                body_chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body_bytes = b"".join(body_chunks)
        if not body_bytes:
            replayed = False

            async def empty_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.disconnect"}

            await self.app(scope, empty_receive, send)
            return

        # Attempt JSON parsing and sanitization
        try:
            parsed_json = json.loads(body_bytes.decode("utf-8"))
            initial_count = len(detections)
            sanitized_json = sanitize_json_payload(parsed_json, "", clean_path, detections)

            if len(detections) > initial_count:
                body_bytes = json.dumps(sanitized_json).encode("utf-8")
                # Update Content-Length header
                new_headers = []
                for k, v in scope.get("headers", []):
                    if k.lower() == b"content-length":
                        new_headers.append((b"content-length", str(len(body_bytes)).encode("ascii")))
                    else:
                        new_headers.append((k, v))
                scope["headers"] = new_headers
        except Exception as exc:
            logger.debug("PII middleware skipped non-JSON body: %s", exc)

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)
