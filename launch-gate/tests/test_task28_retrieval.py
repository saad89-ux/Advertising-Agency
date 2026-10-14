"""Tests for client-filtered vector retrieval and strict client isolation."""

from __future__ import annotations

from contextlib import contextmanager
import math
from pathlib import Path
from typing import Any, Sequence

import pytest

from apps.worker import client_rule_loader as loader
from apps.worker import retrieval
from apps.worker.retrieval import (
    RetrievedRule,
    format_retrieved_rules_for_context,
    list_client_rules,
    retrieve_client_rules,
    retrieve_rule_by_entry,
    verify_citation,
)
from pgvector import Vector

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_ROOT = REPO_ROOT / "data" / "clients"


def _cosine_dist(u: Sequence[float], v: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(u, v))
    norm_u = math.sqrt(sum(a * a for a in u))
    norm_v = math.sqrt(sum(b * b for b in v))
    if norm_u == 0.0 or norm_v == 0.0:
        return 1.0
    sim = dot / (norm_u * norm_v)
    return 1.0 - max(-1.0, min(1.0, sim))


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class FakeVectorDBConnection:
    """Simulates PostgreSQL pgvector table with client scoping and vector distance."""

    def __init__(self):
        self.calls: list[tuple[str, Any]] = []
        self.rules: list[dict[str, Any]] = []

    def add_rule(
        self,
        client_id: str,
        entry_number: str,
        source_file: str,
        content: str,
        vector: Sequence[float],
    ) -> None:
        self.rules.append(
            {
                "client_id": client_id,
                "entry_number": entry_number,
                "source_file": source_file,
                "content": content,
                "vector": list(vector),
            }
        )

    def execute(self, statement: str, params: Any = None):
        self.calls.append((statement, params))

        if "FROM client_rules" in statement and "embedding" in statement:
            # Vector retrieval query: (vec_param, clean_client_id, vec_param, top_k)
            query_vec = params[0].to_list() if hasattr(params[0], "to_list") else list(params[0])
            client_id = params[1]
            top_k = params[3]

            # Enforce SQL-level client filter
            matching = [r for r in self.rules if r["client_id"] == client_id]
            scored = []
            for r in matching:
                dist = _cosine_dist(query_vec, r["vector"])
                scored.append((r, dist))
            scored.sort(key=lambda item: item[1])
            selected = scored[:top_k]

            rows = [
                (
                    r["client_id"],
                    r["entry_number"],
                    r["source_file"],
                    r["content"],
                    dist,
                )
                for r, dist in selected
            ]
            return FakeResult(rows)

        if "SELECT client_id, entry_number, source_file, content" in statement:
            if "entry_number = %s" in statement:
                # retrieve_rule_by_entry: (clean_client_id, clean_entry_number)
                client_id, entry_number = params[0], params[1]
                rows = [
                    (r["client_id"], r["entry_number"], r["source_file"], r["content"])
                    for r in self.rules
                    if r["client_id"] == client_id and r["entry_number"] == entry_number
                ]
                return FakeResult(rows)

            # list_client_rules: (clean_client_id,)
            client_id = params[0]
            rows = [
                (r["client_id"], r["entry_number"], r["source_file"], r["content"])
                for r in self.rules
                if r["client_id"] == client_id
            ]
            return FakeResult(rows)

        return FakeResult([])


def fake_session(connection):
    @contextmanager
    def session():
        yield connection

    return session


# --- Strict Client Isolation Tests ---


def test_retrieval_sql_query_contains_strict_client_id_filter():
    conn = FakeVectorDBConnection()
    conn.add_rule("brightleaf", "BL-BB-1.1", "brand_book.md", "Voice guidelines", [1.0, 0.0])

    retrieve_client_rules("brightleaf", [1.0, 0.0], top_k=3, connection=conn)

    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "WHERE client_id = %s" in sql
    assert "ORDER BY embedding <=> %s ASC" in sql
    assert "LIMIT %s" in sql
    assert params[1] == "brightleaf"
    assert params[3] == 3


def test_client_isolation_never_returns_other_client_rules_even_on_exact_vector_match():
    """Verify that if query vector matches Client B exactly, Client A query never leaks Client B."""
    conn = FakeVectorDBConnection()
    # oakandhome rule has vector [1.0, 0.0]
    conn.add_rule(
        "oakandhome",
        "OH-C-01",
        "claims_register.md",
        "100% Solid Oak Furniture - sustainably sourced",
        [1.0, 0.0],
    )
    # brightleaf rule has vector [0.0, 1.0]
    conn.add_rule(
        "brightleaf",
        "BL-C-01",
        "claims_register.md",
        "Organic Aloe Vera extract with hyaluronic acid",
        [0.0, 1.0],
    )

    # Query for brightleaf using [1.0, 0.0] (the EXACT embedding of oakandhome's claim)
    results = retrieve_client_rules("brightleaf", [1.0, 0.0], top_k=5, connection=conn)

    assert len(results) == 1
    assert results[0].client_id == "brightleaf"
    assert results[0].entry_number == "BL-C-01"
    # Never leaked oakandhome
    assert all(r.client_id == "brightleaf" for r in results)
    assert not any("Oak" in r.content for r in results)
    assert not any(r.entry_number.startswith("OH-") for r in results)


def test_client_isolation_empty_results_when_client_has_no_rules():
    """Client with no rules returns empty list, never leaking existing clients."""
    conn = FakeVectorDBConnection()
    conn.add_rule("oakandhome", "OH-C-01", "claims_register.md", "Oak wood", [1.0, 0.0])
    conn.add_rule("calmleaf", "CL-C-01", "claims_register.md", "CBD gummies", [0.0, 1.0])

    results = retrieve_client_rules("brightleaf", [1.0, 0.0], top_k=5, connection=conn)

    assert results == []


def test_client_isolation_defense_in_depth_drops_cross_client_row():
    """If DB query somehow returned a row with another client_id, code drops it."""
    class LeakyConnection:
        def execute(self, statement, params=None):
            # Adversarial mock that injects an oakandhome row into brightleaf query
            return FakeResult(
                [
                    ("oakandhome", "OH-C-99", "claims_register.md", "Leaked claim", 0.01),
                    ("brightleaf", "BL-C-01", "claims_register.md", "Valid claim", 0.25),
                ]
            )

    results = retrieve_client_rules(
        "brightleaf", [1.0, 0.0], top_k=5, connection=LeakyConnection()
    )

    assert len(results) == 1
    assert results[0].client_id == "brightleaf"
    assert results[0].entry_number == "BL-C-01"


def test_retrieve_rule_by_entry_cross_client_isolation():
    conn = FakeVectorDBConnection()
    conn.add_rule("oakandhome", "OH-C-01", "claims_register.md", "Oak claims", [1.0, 0.0])
    conn.add_rule("brightleaf", "BL-C-01", "claims_register.md", "Brightleaf claims", [0.0, 1.0])

    # Attempt to fetch oakandhome rule using brightleaf client_id
    cross_client_lookup = retrieve_rule_by_entry("brightleaf", "OH-C-01", connection=conn)
    assert cross_client_lookup is None

    # Legitimate lookup works
    valid_lookup = retrieve_rule_by_entry("brightleaf", "BL-C-01", connection=conn)
    assert valid_lookup is not None
    assert valid_lookup.client_id == "brightleaf"
    assert valid_lookup.entry_number == "BL-C-01"


def test_list_client_rules_is_strictly_scoped():
    conn = FakeVectorDBConnection()
    conn.add_rule("brightleaf", "BL-BB-1.1", "brand_book.md", "BL Book", [0.1, 0.2])
    conn.add_rule("brightleaf", "BL-C-01", "claims_register.md", "BL Claim", [0.3, 0.4])
    conn.add_rule("calmleaf", "CL-BB-1.1", "brand_book.md", "CL Book", [0.5, 0.6])

    bl_rules = list_client_rules("brightleaf", connection=conn)
    assert len(bl_rules) == 2
    assert all(r.client_id == "brightleaf" for r in bl_rules)

    cl_rules = list_client_rules("calmleaf", connection=conn)
    assert len(cl_rules) == 1
    assert cl_rules[0].client_id == "calmleaf"


# --- Vector Ranking and Distance Tests ---


def test_ranking_orders_by_cosine_distance_ascending():
    conn = FakeVectorDBConnection()
    # Query vector is [1.0, 0.0]
    # Distances to [1.0, 0.0]:
    # [1.0, 0.0] -> dist = 0.0
    # [0.707, 0.707] -> dist ≈ 0.293
    # [0.0, 1.0] -> dist = 1.0
    conn.add_rule("brightleaf", "R3", "doc.md", "Rule 3 (orthogonal)", [0.0, 1.0])
    conn.add_rule("brightleaf", "R1", "doc.md", "Rule 1 (exact)", [1.0, 0.0])
    conn.add_rule("brightleaf", "R2", "doc.md", "Rule 2 (close)", [0.707, 0.707])

    results = retrieve_client_rules("brightleaf", [1.0, 0.0], top_k=3, connection=conn)

    assert len(results) == 3
    assert [r.entry_number for r in results] == ["R1", "R2", "R3"]
    assert results[0].distance == pytest.approx(0.0, abs=1e-4)
    assert results[0].similarity == pytest.approx(1.0, abs=1e-4)
    assert results[1].distance < results[2].distance


def test_top_k_limits_number_of_returned_rules():
    conn = FakeVectorDBConnection()
    for i in range(10):
        angle = (i / 10.0) * (math.pi / 2)
        vec = [math.cos(angle), math.sin(angle)]
        conn.add_rule("brightleaf", f"R{i}", "doc.md", f"Content {i}", vec)

    results = retrieve_client_rules("brightleaf", [1.0, 0.0], top_k=3, connection=conn)
    assert len(results) == 3


def test_query_with_text_and_embed_callable():
    conn = FakeVectorDBConnection()
    conn.add_rule("brightleaf", "BL-1", "doc.md", "Botanical serum", [0.8, 0.6])

    def mock_embed(text: str) -> list[float]:
        assert text == "organic serum"
        return [0.8, 0.6]

    results = retrieve_client_rules(
        "brightleaf",
        query_text="organic serum",
        embed_text=mock_embed,
        connection=conn,
    )

    assert len(results) == 1
    assert results[0].entry_number == "BL-1"
    assert results[0].distance == pytest.approx(0.0, abs=1e-4)


# --- Citation & Formatting Helper Tests ---


def test_verify_citation_matches_only_retrieved_rules():
    rule1 = RetrievedRule("brightleaf", "BL-BB-1.1", "brand_book.md", "Voice", 0.1)
    rule2 = RetrievedRule("brightleaf", "BL-C-02", "claims_register.md", "Hydration", 0.3)
    retrieved = [rule1, rule2]

    assert verify_citation(retrieved, "BL-BB-1.1") is True
    assert verify_citation(retrieved, "BL-C-02") is True
    assert verify_citation(retrieved, "BL-C-01") is False
    assert verify_citation(retrieved, "OH-C-01") is False
    assert verify_citation(retrieved, "") is False


def test_format_retrieved_rules_for_context():
    assert format_retrieved_rules_for_context([]) == ""

    rule = RetrievedRule("brightleaf", "BL-C-01", "claims_register.md", "Content text", 0.1)
    formatted = format_retrieved_rules_for_context([rule])
    assert "[BL-C-01] (claims_register.md):" in formatted
    assert "Content text" in formatted


# --- Validation Error Tests ---


@pytest.mark.parametrize("invalid_client_id", ["", "   ", None, 123])
def test_rejects_invalid_client_id(invalid_client_id):
    with pytest.raises(ValueError, match="client_id must be a non-empty string"):
        retrieve_client_rules(invalid_client_id, [1.0, 0.0])


@pytest.mark.parametrize("invalid_top_k", [0, -1, "5", True, False])
def test_rejects_invalid_top_k(invalid_top_k):
    with pytest.raises(ValueError, match="top_k must be a positive integer"):
        retrieve_client_rules("brightleaf", [1.0, 0.0], top_k=invalid_top_k)


def test_rejects_unsupported_metric():
    with pytest.raises(ValueError, match="unsupported metric"):
        retrieve_client_rules("brightleaf", [1.0, 0.0], metric="manhattan")


@pytest.mark.parametrize(
    "invalid_vec,match",
    [
        ([], "empty"),
        ([1.0, float("nan")], "finite"),
        ([1.0, float("inf")], "finite"),
        ([1.0, "bad"], "numbers"),
        ([1.0, True], "numbers"),
    ],
)
def test_rejects_invalid_vector(invalid_vec, match):
    with pytest.raises(ValueError, match=match):
        retrieve_client_rules("brightleaf", invalid_vec)


def test_rejects_missing_both_embedding_and_text():
    with pytest.raises(ValueError, match="either query_embedding or query_text"):
        retrieve_client_rules("brightleaf")


def test_rejects_query_text_without_embed_callable():
    with pytest.raises(ValueError, match="embed_text callable must be provided"):
        retrieve_client_rules("brightleaf", query_text="hello")


def test_rejects_empty_query_text():
    with pytest.raises(ValueError, match="query_text cannot be empty"):
        retrieve_client_rules("brightleaf", query_text="   ", embed_text=lambda t: [1.0])


# --- Isolation with Real Data Pack Entries ---


def test_isolation_with_all_real_data_pack_entries():
    """Load all real entries from data/clients/ and verify strict client partitioning."""
    all_entries = loader.load_rule_entries(rules_root=RULES_ROOT)
    conn = FakeVectorDBConnection()

    # Deterministic synthetic embedding from entry content
    def embed(text: str) -> list[float]:
        # Simple bag of characters hash into 4 dimensions
        v = [0.0, 0.0, 0.0, 0.0]
        for idx, char in enumerate(text):
            v[idx % 4] += ord(char)
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    for entry in all_entries:
        conn.add_rule(
            client_id=entry.client_id,
            entry_number=entry.entry_number,
            source_file=entry.source_file,
            content=entry.content,
            vector=embed(entry.content),
        )

    # For each client, retrieve top 5 using a query and verify zero cross-contamination
    clients = {"brightleaf", "calmleaf", "oakandhome"}
    for client_id in clients:
        query_vec = [1.0, 0.0, 0.0, 0.0]
        results = retrieve_client_rules(client_id, query_vec, top_k=5, connection=conn)
        assert results, f"Expected results for client {client_id}"
        for r in results:
            assert r.client_id == client_id, (
                f"Isolation breach: retrieved rule for {r.client_id} during query for {client_id}"
            )
            # Entry prefix check
            expected_prefix = {
                "brightleaf": "BL-",
                "calmleaf": "CL-",
                "oakandhome": "OH-",
            }[client_id]
            assert r.entry_number.startswith(expected_prefix)
