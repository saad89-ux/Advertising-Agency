"""Strict validation and client-isolation checks for model-produced findings."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from packages.contracts import AdContract, CheckType, FindingContract
from packages.guards.input_guard import safe_export_cell


_ALLOWED_TOP_LEVEL = {"findings", "summary", "unknown", "needs_person"}
_ALLOWED_FINDING_KEYS = {"message", "field", "proposed_fix", "rule_id", "check_type", "quote"}
_GENERIC_MESSAGE = "Model output was rejected by the safety guard."


@dataclass(frozen=True)
class OutputGuardResult:
    findings: list[FindingContract]
    rejected_reasons: list[str]
    needs_person: bool


def _reject(ad: AdContract, reason: str) -> OutputGuardResult:
    finding = FindingContract(
        finding_id=f"{ad.ad_id}:guard:rejected_model_output",
        ad_id=ad.ad_id,
        row_number=ad.row_number,
        check_type=CheckType.WORDING,
        message=_GENERIC_MESSAGE,
        rule_id=None,
    )
    return OutputGuardResult([finding], [reason], True)


def _flatten_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        result: set[str] = set()
        for nested in value.values():
            result.update(_flatten_ids(nested))
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result = set()
        for nested in value:
            result.update(_flatten_ids(nested))
        return result
    return set()


def _contains_rule_id(text: str, rule_id: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(rule_id)}(?![\w-])", text, re.I))


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)


def _contains_verbatim_span(text: str, source: str, minimum_words: int = 6) -> bool:
    candidate = _words(text)
    source_words = _words(source)
    if len(candidate) < minimum_words or len(source_words) < minimum_words:
        return False
    source_windows = {
        tuple(source_words[index:index + minimum_words])
        for index in range(len(source_words) - minimum_words + 1)
    }
    return any(
        tuple(candidate[index:index + minimum_words]) in source_windows
        for index in range(len(candidate) - minimum_words + 1)
    )


def _clean_model_message(message: str) -> str:
    cleaned = "".join(
        character for character in message
        if unicodedata.category(character) != "Cc"
    )
    if len(cleaned) > 500:
        raise ValueError("oversized_message")
    return safe_export_cell(cleaned)


def validate_model_output(
    raw: str | dict[str, Any],
    *,
    ad: AdContract,
    rule_index: dict[str, Any],
    client_texts: dict[str, str],
    retrieved_rule_ids: Sequence[Any] | None = None,
) -> OutputGuardResult:
    """Accept only strict finding objects and citations/content from this ad's client."""
    if isinstance(raw, str):
        clean_raw = raw.strip()
        if clean_raw.startswith("```"):
            lines = clean_raw.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_raw = "\n".join(lines).strip()
        try:
            value = json.loads(clean_raw)
        except (json.JSONDecodeError, TypeError):
            return _reject(ad, "invalid_json")
    elif isinstance(raw, dict):
        value = raw
    else:
        return _reject(ad, "invalid_output_type")

    if type(value) is not dict or not set(value).issubset(_ALLOWED_TOP_LEVEL) or "findings" not in value:
        return _reject(ad, "invalid_output_shape")
    raw_findings = value["findings"]
    if type(raw_findings) is not list:
        return _reject(ad, "findings_must_be_list")

    own_ids = _flatten_ids(rule_index.get(ad.client_id))
    valid_citation_ids = own_ids
    if retrieved_rule_ids is not None:
        valid_citation_ids = {
            item.entry_number if hasattr(item, "entry_number") else str(item)
            for item in retrieved_rule_ids
        }
    other_ids: set[str] = set()
    other_texts: list[str] = []
    for client_id, entries in rule_index.items():
        if client_id != ad.client_id and client_id != "_note":
            other_ids.update(_flatten_ids(entries))
    for client_id, text in client_texts.items():
        if client_id != ad.client_id:
            other_texts.append(text)

    accepted: list[FindingContract] = []
    rejected: list[str] = []
    for index, raw_finding in enumerate(raw_findings):
        if (
            type(raw_finding) is not dict
            or set(raw_finding) - _ALLOWED_FINDING_KEYS
            or "message" not in raw_finding
            or not isinstance(raw_finding["message"], str)
            or not isinstance(raw_finding.get("check_type"), str)
            or raw_finding.get("check_type") not in {"wording", "page"}
        ):
            return _reject(ad, "invalid_finding_shape")
        for optional in ("field", "proposed_fix", "rule_id", "quote"):
            if optional in raw_finding and raw_finding[optional] is not None and not isinstance(raw_finding[optional], str):
                return _reject(ad, "invalid_finding_type")

        try:
            message = _clean_model_message(raw_finding["message"])
            if not message.strip():
                raise ValueError("empty_message")
        except ValueError as exc:
            return _reject(ad, str(exc))

        field = raw_finding.get("field")
        proposed_fix = raw_finding.get("proposed_fix")
        rule_id = raw_finding.get("rule_id")
        check_type = raw_finding["check_type"]
        quote = raw_finding.get("quote")
        texts = [message]
        if field is not None:
            texts.append(field)
        if proposed_fix is not None:
            texts.append(proposed_fix)
        if quote is not None:
            texts.append(quote)
            # Verbatim substring verification for explicit quote
            ad_content = f"{ad.headline or ''} {ad.ad_text or ''}"
            if quote.strip() and quote not in ad_content:
                rejected.append(f"unverified_quote:{quote}")
                continue
        combined_text = "\n".join(texts)
        if rule_id in other_ids or any(_contains_rule_id(combined_text, other_id) for other_id in other_ids):
            rejected.append("cross_client_rule_id")
            continue
        if any(_contains_verbatim_span(combined_text, source) for source in other_texts):
            rejected.append("cross_client_rule_text")
            continue
        if check_type == "wording" and (not isinstance(rule_id, str) or rule_id not in valid_citation_ids):
            rejected.append("unknown_or_missing_rule_id")
            continue

        try:
            accepted.append(
                FindingContract(
                    finding_id=f"{ad.ad_id}:model:{check_type}:{index}",
                    ad_id=ad.ad_id,
                    row_number=ad.row_number,
                    check_type=CheckType(check_type),
                    message=message,
                    field=field,
                    proposed_fix=proposed_fix,
                    rule_id=rule_id,
                )
            )
        except ValueError:
            return _reject(ad, "invalid_finding_contract")
    return OutputGuardResult(accepted, rejected, bool(rejected))
