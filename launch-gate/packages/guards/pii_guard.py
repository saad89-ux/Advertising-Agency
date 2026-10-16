"""PII (Personally Identifiable Information) detection and scrubbing middleware.

Ensures that user prompt inputs, ad text, employee contact info, and creatives
do not leak private sensitive data (emails, phones, payment cards, SSNs, API keys)
to LLM prompts or public logs.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

# Regex patterns for identifying common PII entities
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
)
_PHONE_PATTERN = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)
_CREDIT_CARD_PATTERN = re.compile(
    r"\b(?:\d{4}[-\s]?){3}\d{4}\b|\b\d{15,16}\b"
)
_SSN_PATTERN = re.compile(
    r"\b\d{3}-\d{2}-\d{4}\b"
)
_SECRET_KEY_PATTERN = re.compile(
    r"\b(?:sk-[a-zA-Z0-9_-]{20,}|AIza[0-9A-Za-z-_]{35}|ghp_[a-zA-Z0-9]{36}|Bearer\s+[a-zA-Z0-9_\-\.]{25,})\b"
)
_IPV4_PATTERN = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
)


@dataclass(frozen=True)
class PiiDetection:
    entity_type: str
    original_snippet: str
    masked_value: str
    start_pos: int
    end_pos: int


@dataclass(frozen=True)
class PiiSanitizeResult:
    cleaned_text: str
    detections: list[PiiDetection]
    has_pii: bool


def scrub_pii(text: str) -> PiiSanitizeResult:
    """Scan text for sensitive PII entities and replace them with safe redaction placeholders."""
    if not text:
        return PiiSanitizeResult(cleaned_text="", detections=[], has_pii=False)

    detections: list[PiiDetection] = []
    cleaned = text

    # Helper replacement function
    def _replace_entity(pattern: re.Pattern, entity_type: str, mask_label: str, target_text: str) -> str:
        nonlocal detections
        offset = 0
        result_chunks = []
        last_end = 0

        for match in pattern.finditer(target_text):
            val = match.group(0)
            # Skip if already a redaction token
            if val.startswith("[REDACTED_"):
                continue
            
            # Additional check for false positives on short numbers
            if entity_type == "CREDIT_CARD" and len(re.sub(r"\D", "", val)) < 13:
                continue
            if entity_type == "IPV4" and val.startswith("127.0.0.1"):
                # Allow localhost
                continue

            detections.append(
                PiiDetection(
                    entity_type=entity_type,
                    original_snippet=val[:4] + "..." if len(val) > 4 else val,
                    masked_value=mask_label,
                    start_pos=match.start(),
                    end_pos=match.end(),
                )
            )
            result_chunks.append(target_text[last_end:match.start()])
            result_chunks.append(mask_label)
            last_end = match.end()

        result_chunks.append(target_text[last_end:])
        return "".join(result_chunks)

    # 1. Scrub secrets and API keys first
    cleaned = _replace_entity(_SECRET_KEY_PATTERN, "SECRET_KEY", "[REDACTED_SECRET]", cleaned)

    # 2. Scrub payment cards
    cleaned = _replace_entity(_CREDIT_CARD_PATTERN, "CREDIT_CARD", "[REDACTED_PAYMENT_CARD]", cleaned)

    # 3. Scrub SSNs
    cleaned = _replace_entity(_SSN_PATTERN, "SSN", "[REDACTED_GOV_ID]", cleaned)

    # 4. Scrub email addresses
    cleaned = _replace_entity(_EMAIL_PATTERN, "EMAIL", "[REDACTED_EMAIL]", cleaned)

    # 5. Scrub phone numbers
    cleaned = _replace_entity(_PHONE_PATTERN, "PHONE", "[REDACTED_PHONE]", cleaned)

    return PiiSanitizeResult(
        cleaned_text=cleaned,
        detections=detections,
        has_pii=len(detections) > 0,
    )
