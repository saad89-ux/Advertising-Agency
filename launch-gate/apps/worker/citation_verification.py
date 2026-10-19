"""Citation verification for wording check findings.

Enforces:
1. Every wording check finding must cite an entry that exists.
2. The cited entry must come from the retrieved rules for that client (no invented rules).
3. Cross-client citations are strictly rejected.
4. Unretrieved, invented, or missing citations flag needs_person.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import json
import logging
from pathlib import Path
import re
from typing import Any

from apps.worker.retrieval import RetrievedRule, retrieve_rule_by_entry
from packages.contracts import AdContract, CheckType, FindingContract

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENTS_DATA_ROOT = REPO_ROOT / "data" / "clients"

CLIENT_PREFIX_MAP: dict[str, str] = {
    "BL": "brightleaf",
    "CL": "calmleaf",
    "OH": "oakandhome",
}


class CitationStatus(str, Enum):
    VERIFIED = "verified"
    NOT_RETRIEVED = "not_retrieved"
    INVENTED = "invented"
    CROSS_CLIENT = "cross_client"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"
    CITATION_NOT_SUPPORTED = "citation_not_supported"


@dataclass(frozen=True)
class CitationVerification:
    """Result of verifying a single citation rule_id."""

    rule_id: str | None
    status: CitationStatus
    is_valid: bool
    reason: str
    retrieved_rule: RetrievedRule | None = None


@dataclass(frozen=True)
class FindingCitationResult:
    """Verification result bound to a FindingContract."""

    finding: FindingContract
    status: CitationStatus
    is_valid: bool
    reason: str
    retrieved_rule: RetrievedRule | None = None


@dataclass(frozen=True)
class CitationVerificationReport:
    """Summary report for a batch of findings verified against retrieved rules."""

    is_valid: bool
    verified_count: int
    failed_count: int
    results: list[FindingCitationResult]
    accepted_findings: list[FindingContract]
    rejected_findings: list[FindingContract]
    rejection_reasons: list[str]
    needs_person: bool


def _load_rule_index(rules_root: Path | None = None) -> dict[str, dict[str, list[str]]]:
    root = rules_root or CLIENTS_DATA_ROOT
    index_path = root / "rule_index.json"
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k != "_note" and isinstance(v, dict)}
    except Exception as exc:
        logger.warning("Could not load rule index from %s: %s", index_path, exc)
        return {}


def get_known_client_rule_ids(
    client_id: str, *, rules_root: Path | None = None
) -> set[str]:
    """Return all known indexed rule IDs for a given client."""
    index = _load_rule_index(rules_root)
    client_docs = index.get(client_id, {})
    ids: set[str] = set()
    for doc_entries in client_docs.values():
        if isinstance(doc_entries, list):
            ids.update(doc_entries)
    return ids


def get_all_known_rule_ids(*, rules_root: Path | None = None) -> dict[str, str]:
    """Return a mapping of rule_id -> client_id for all known indexed rules."""
    index = _load_rule_index(rules_root)
    mapping: dict[str, str] = {}
    for client_id, docs in index.items():
        for doc_entries in docs.values():
            if isinstance(doc_entries, list):
                for entry in doc_entries:
                    mapping[entry] = client_id
    return mapping


def infer_client_from_citation(rule_id: str) -> str | None:
    """Infer the expected client_id from standard rule_id prefix (e.g. 'BL-BB-1.1' -> 'brightleaf')."""
    if not rule_id or not isinstance(rule_id, str):
        return None
    prefix = rule_id.split("-", 1)[0].upper()
    return CLIENT_PREFIX_MAP.get(prefix)


def verify_citation_entry(
    cited_entry: str | None,
    retrieved_rules: Sequence[RetrievedRule | str] = (),
    *,
    client_id: str | None = None,
    connection: Any = None,
    rules_root: Path | None = None,
) -> CitationVerification:
    """Verify whether a single cited rule_id exists and comes from retrieved rules.

    Checks:
    1. Citation must not be empty or missing.
    2. Citation must exist in retrieved_rules for the target client.
    3. If not in retrieved_rules, distinguishes:
       - NOT_RETRIEVED: exists for target client, but not retrieved.
       - CROSS_CLIENT: belongs to another client.
       - INVENTED: does not exist anywhere.
    """
    if cited_entry is None or not isinstance(cited_entry, str) or not cited_entry.strip():
        return CitationVerification(
            rule_id=cited_entry,
            status=CitationStatus.MISSING,
            is_valid=False,
            reason="Citation rule_id is missing or empty",
        )

    clean_id = cited_entry.strip()
    all_rules_map = get_all_known_rule_ids(rules_root=rules_root)

    # Build index of retrieved rules
    retrieved_by_id: dict[str, RetrievedRule | None] = {}
    for r in retrieved_rules:
        if isinstance(r, RetrievedRule):
            retrieved_by_id[r.entry_number] = r
        elif isinstance(r, str):
            retrieved_by_id[r] = None

    # Check 1: Is it in retrieved rules?
    if clean_id in retrieved_by_id:
        matched_rule = retrieved_by_id[clean_id]
        rule_owner = (
            matched_rule.client_id
            if matched_rule is not None
            else (all_rules_map.get(clean_id) or infer_client_from_citation(clean_id))
        )
        if rule_owner is not None and client_id is not None and rule_owner != client_id:
            return CitationVerification(
                rule_id=clean_id,
                status=CitationStatus.CROSS_CLIENT,
                is_valid=False,
                reason=f"Citation {clean_id!r} belongs to client {rule_owner!r}, not {client_id!r}",
                retrieved_rule=matched_rule,
            )
        return CitationVerification(
            rule_id=clean_id,
            status=CitationStatus.VERIFIED,
            is_valid=True,
            reason=f"Citation {clean_id!r} verified in retrieved rules",
            retrieved_rule=matched_rule,
        )

    # Check 2: Not in retrieved rules. Look up known rules across all clients.
    owner_client = all_rules_map.get(clean_id)

    # Check database if connection is provided and rule wasn't in index file
    if owner_client is None and connection is not None:
        try:
            cursor = connection.execute(
                "SELECT client_id FROM client_rules WHERE entry_number = %s",
                (clean_id,),
            )
            rows = cursor.fetchall() if hasattr(cursor, "fetchall") else cursor
            if rows:
                row = rows[0]
                owner_client = row["client_id"] if isinstance(row, dict) else row[0]
        except Exception:
            pass

    # If it belongs to another client -> CROSS_CLIENT
    if owner_client is not None and client_id is not None and owner_client != client_id:
        return CitationVerification(
            rule_id=clean_id,
            status=CitationStatus.CROSS_CLIENT,
            is_valid=False,
            reason=f"Citation {clean_id!r} belongs to client {owner_client!r}, not {client_id!r}",
        )

    # If it belongs to the target client -> NOT_RETRIEVED
    if owner_client is not None and (client_id is None or owner_client == client_id):
        return CitationVerification(
            rule_id=clean_id,
            status=CitationStatus.NOT_RETRIEVED,
            is_valid=False,
            reason=f"Citation {clean_id!r} exists for client {owner_client!r} but was not among retrieved rules",
        )

    # Check if prefix belongs to another known client
    inferred_client = infer_client_from_citation(clean_id)
    if inferred_client is not None and client_id is not None and inferred_client != client_id:
        return CitationVerification(
            rule_id=clean_id,
            status=CitationStatus.CROSS_CLIENT,
            is_valid=False,
            reason=f"Citation {clean_id!r} prefix indicates client {inferred_client!r}, not {client_id!r}",
        )

    # Does not exist in any client rules -> INVENTED
    return CitationVerification(
        rule_id=clean_id,
        status=CitationStatus.INVENTED,
        is_valid=False,
        reason=f"Citation {clean_id!r} does not exist in any client rules (invented citation)",
    )


def extract_forbidden_phrases(rule_content: str | None) -> list[str] | None:
    """Extract list of forbidden phrases if rule is a [FORBIDDEN] phrases list."""
    if not rule_content or not isinstance(rule_content, str):
        return None
    has_forbidden_tag = "[FORBIDDEN]" in rule_content.upper() or "FORBIDDEN" in rule_content.upper()
    match = re.search(r"(?:^|\n)\s*[-*]?\s*Phrases:\s*([^\n]+)", rule_content, re.IGNORECASE)
    if not match or not has_forbidden_tag:
        return None
    raw_phrases = match.group(1).split(",")
    phrases = [
        p.strip().strip("`'\"")
        for p in raw_phrases
        if p.strip() and p.strip().lower() != "n/a"
    ]
    return phrases if phrases else None


def _normalize_phrase(text: str) -> str:
    """Normalize phrase: lowercase, collapse whitespace, strip punctuation."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Calculate standard Levenshtein distance."""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def is_phrase_grounded(quote: str, forbidden_phrases: Sequence[str]) -> bool:
    """Check if quote matches any forbidden phrase in the rule (case-insensitive substring or typo dist <= 1)."""
    norm_quote = _normalize_phrase(quote)
    if not norm_quote:
        return False
    quote_words = norm_quote.split()

    for phrase in forbidden_phrases:
        norm_phrase = _normalize_phrase(phrase)
        if not norm_phrase:
            continue
        # Direction: forbidden phrase must appear INSIDE the quote (or equal after normalisation)
        if norm_phrase in norm_quote:
            return True
        # Check single-word near match (typo distance <= 1 for words of length >= 5)
        phrase_words = norm_phrase.split()
        if len(phrase_words) == 1 and len(quote_words) == 1:
            w1, w2 = phrase_words[0], quote_words[0]
            if len(w1) >= 5 and len(w2) >= 5 and _levenshtein_distance(w1, w2) <= 1:
                return True
        elif len(phrase_words) == len(quote_words) and len(phrase_words) > 1:
            all_words_match = True
            for w1, w2 in zip(phrase_words, quote_words):
                if w1 != w2:
                    if len(w1) >= 5 and len(w2) >= 5 and _levenshtein_distance(w1, w2) <= 1:
                        continue
                    all_words_match = False
                    break
            if all_words_match:
                return True
    return False


def is_contradictory_message(message: str) -> bool:
    """Check if message states the claim is not covered in the rules while citing a rule."""
    if not message:
        return False
    msg_lower = message.lower()
    patterns = [
        r"\bnot covered (?:in|by) (?:the |any )?rules\b",
        r"\bnot mentioned in (?:the |any )?rules\b",
        r"\bnot found in (?:the |any )?rules\b",
        r"\bclaim not in (?:the |any )?rules\b",
        r"\bnot (?:addressed|supported|listed) in (?:the |any )?rules\b",
        r"\bclaim cannot be verified from (?:the |available )?rules\b",
        r"\bnot covered in the rules\b",
    ]
    return any(re.search(pat, msg_lower) for pat in patterns)


def verify_finding_citation(
    finding: FindingContract,
    retrieved_rules: Sequence[RetrievedRule | str] = (),
    *,
    client_id: str | None = None,
    connection: Any = None,
    rules_root: Path | None = None,
    quote: str | None = None,
) -> FindingCitationResult:
    """Verify citation compliance and deterministic grounding for a FindingContract."""
    if finding.check_type != CheckType.WORDING:
        # Non-wording checks (mechanical, page) do not require citation verification
        return FindingCitationResult(
            finding=finding,
            status=CitationStatus.NOT_APPLICABLE,
            is_valid=True,
            reason=f"Citation verification not applicable for {finding.check_type.value} check",
        )

    check = verify_citation_entry(
        finding.rule_id,
        retrieved_rules,
        client_id=client_id,
        connection=connection,
        rules_root=rules_root,
    )

    if not check.is_valid:
        return FindingCitationResult(
            finding=finding,
            status=check.status,
            is_valid=False,
            reason=check.reason,
            retrieved_rule=check.retrieved_rule,
        )

    # Grounding check 1: Reject findings whose message asserts claim is "not covered in the rules"
    if finding.rule_id and is_contradictory_message(finding.message):
        return FindingCitationResult(
            finding=finding,
            status=CitationStatus.CITATION_NOT_SUPPORTED,
            is_valid=False,
            reason=f"citation_not_supported: finding message asserts claim is not covered in rules while citing {finding.rule_id!r}",
            retrieved_rule=check.retrieved_rule,
        )

    # Grounding check 2: If cited rule has a [FORBIDDEN] phrases list, quote must match one of the listed phrases
    if check.retrieved_rule is not None and check.retrieved_rule.content:
        forbidden_phrases = extract_forbidden_phrases(check.retrieved_rule.content)
        if forbidden_phrases is not None:
            active_quote = quote or getattr(finding, "quote", None)
            if not active_quote:
                # Check for explicit quotes in message
                q_matches = re.findall(r'["\']([^"\']{2,})["\']', finding.message)
                if q_matches:
                    active_quote = q_matches[0]
            if not active_quote or not is_phrase_grounded(active_quote, forbidden_phrases):
                return FindingCitationResult(
                    finding=finding,
                    status=CitationStatus.CITATION_NOT_SUPPORTED,
                    is_valid=False,
                    reason=f"citation_not_supported: quote {active_quote!r} does not match forbidden phrases {forbidden_phrases!r} for rule {finding.rule_id!r}",
                    retrieved_rule=check.retrieved_rule,
                )

    return FindingCitationResult(
        finding=finding,
        status=check.status,
        is_valid=check.is_valid,
        reason=check.reason,
        retrieved_rule=check.retrieved_rule,
    )


def verify_findings_citations(
    findings: Sequence[FindingContract],
    retrieved_rules: Sequence[RetrievedRule | str] = (),
    *,
    client_id: str | None = None,
    connection: Any = None,
    rules_root: Path | None = None,
    quotes_by_index: Mapping[int, str | None] | None = None,
) -> CitationVerificationReport:
    """Verify all findings in a list and produce a comprehensive CitationVerificationReport."""
    results: list[FindingCitationResult] = []
    accepted: list[FindingContract] = []
    rejected: list[FindingContract] = []
    reasons: list[str] = []

    verified_count = 0
    failed_count = 0

    for idx, finding in enumerate(findings):
        quote_val = quotes_by_index.get(idx) if quotes_by_index is not None else None
        res = verify_finding_citation(
            finding,
            retrieved_rules,
            client_id=client_id,
            connection=connection,
            rules_root=rules_root,
            quote=quote_val,
        )
        results.append(res)

        if res.is_valid:
            accepted.append(finding)
            if finding.check_type == CheckType.WORDING:
                verified_count += 1
        else:
            rejected.append(finding)
            failed_count += 1
            reasons.append(f"{res.status.value}:{finding.rule_id or 'none'}")

    all_valid = (failed_count == 0)
    needs_person = bool(failed_count > 0)

    return CitationVerificationReport(
        is_valid=all_valid,
        verified_count=verified_count,
        failed_count=failed_count,
        results=results,
        accepted_findings=accepted,
        rejected_findings=rejected,
        rejection_reasons=reasons,
        needs_person=needs_person,
    )


def verify_ad_findings(
    ad: AdContract,
    findings: Sequence[FindingContract],
    retrieved_rules: Sequence[RetrievedRule | str] = (),
    *,
    connection: Any = None,
    rules_root: Path | None = None,
) -> CitationVerificationReport:
    """Verify all findings for a specific ad using ad.client_id."""
    return verify_findings_citations(
        findings,
        retrieved_rules,
        client_id=ad.client_id,
        connection=connection,
        rules_root=rules_root,
    )
