"""Unit tests for deterministic citation grounding and contradictory message detection."""

from apps.worker.citation_verification import (
    CitationStatus,
    extract_forbidden_phrases,
    is_contradictory_message,
    is_phrase_grounded,
    verify_finding_citation,
)
from apps.worker.retrieval import RetrievedRule
from packages.contracts import CheckType, FindingContract


RULE_BL_C_01 = RetrievedRule(
    client_id="brightleaf",
    entry_number="BL-C-01",
    source_file="claims_register.md",
    content="## BL-C-01 [FORBIDDEN]\n- Phrases: clinically proven, reduces wrinkles in 14 days, proven to reduce wrinkles\n- Rule: No claim of clinical proof or timed wrinkle results.",
)

RULE_BL_BB_1_1 = RetrievedRule(
    client_id="brightleaf",
    entry_number="BL-BB-1.1",
    source_file="brand_book.md",
    content="## BL-BB-1.1 Voice\nWarm, calm and expert. Short sentences. Talk about how skin looks, not what it treats.",
)


def test_extract_forbidden_phrases():
    phrases = extract_forbidden_phrases(RULE_BL_C_01.content)
    assert phrases == [
        "clinically proven",
        "reduces wrinkles in 14 days",
        "proven to reduce wrinkles",
    ]

    # Non-forbidden rule returns None
    assert extract_forbidden_phrases(RULE_BL_BB_1_1.content) is None


def test_is_phrase_grounded_exact_and_substring():
    forbidden = ["clinically proven", "reduces wrinkles in 14 days"]
    
    # Exact and substring matches
    assert is_phrase_grounded("clinically proven", forbidden) is True
    assert is_phrase_grounded("our clinically proven serum", forbidden) is True
    assert is_phrase_grounded("reduces wrinkles", forbidden) is True

    # Typo match (dist <= 1)
    assert is_phrase_grounded("clincally proven", forbidden) is True

    # Ungrounded quote
    assert is_phrase_grounded("cures arthritis in 2 days guaranteed", forbidden) is False
    assert is_phrase_grounded("miracle joint care", forbidden) is False


def test_is_contradictory_message():
    assert is_contradictory_message("The ad text contains a claim not covered in the rules.") is True
    assert is_contradictory_message("This claim is not covered by rules.") is True
    assert is_contradictory_message("Claim cannot be verified from available rules.") is True
    assert is_contradictory_message("Product treats arthritis violating how skin looks rule.") is False


def test_grounding_rejects_unsupported_quote():
    finding = FindingContract(
        finding_id="f1",
        ad_id="ad1",
        row_number=1,
        check_type=CheckType.WORDING,
        message="Violates clinical claim rules.",
        field="ad_text",
        rule_id="BL-C-01",
    )

    # Quote does not match BL-C-01 forbidden phrases
    res = verify_finding_citation(
        finding,
        retrieved_rules=[RULE_BL_C_01],
        client_id="brightleaf",
        quote="cures arthritis in 2 days guaranteed",
    )

    assert res.is_valid is False
    assert res.status == CitationStatus.CITATION_NOT_SUPPORTED
    assert "citation_not_supported" in res.reason


def test_grounding_rejects_contradictory_message():
    finding = FindingContract(
        finding_id="f2",
        ad_id="ad1",
        row_number=1,
        check_type=CheckType.WORDING,
        message="The ad text contains a medical efficacy claim not covered in the rules.",
        field="ad_text",
        rule_id="BL-C-01",
    )

    res = verify_finding_citation(
        finding,
        retrieved_rules=[RULE_BL_C_01],
        client_id="brightleaf",
        quote="clinically proven",
    )

    assert res.is_valid is False
    assert res.status == CitationStatus.CITATION_NOT_SUPPORTED
    assert "not covered in rules" in res.reason


def test_grounding_accepts_valid_voice_and_forbidden_citations():
    # Voice rule with treatment quote
    voice_finding = FindingContract(
        finding_id="f3",
        ad_id="ad1",
        row_number=1,
        check_type=CheckType.WORDING,
        message="Talks about what it treats rather than how skin looks.",
        field="ad_text",
        rule_id="BL-BB-1.1",
    )
    res_voice = verify_finding_citation(
        voice_finding,
        retrieved_rules=[RULE_BL_BB_1_1],
        client_id="brightleaf",
        quote="cures arthritis",
    )
    assert res_voice.is_valid is True
    assert res_voice.status == CitationStatus.VERIFIED

    # Forbidden rule with grounded quote
    forbidden_finding = FindingContract(
        finding_id="f4",
        ad_id="ad1",
        row_number=1,
        check_type=CheckType.WORDING,
        message="Ad claims clinical proof.",
        field="ad_text",
        rule_id="BL-C-01",
    )
    res_forbidden = verify_finding_citation(
        forbidden_finding,
        retrieved_rules=[RULE_BL_C_01],
        client_id="brightleaf",
        quote="clinically proven",
    )
    assert res_forbidden.is_valid is True
    assert res_forbidden.status == CitationStatus.VERIFIED
