"""Client-filtered vector retrieval for client rules and claims."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
import math
from typing import Any

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

from apps.api.database import get_session
from apps.worker.client_rule_loader import CREATE_CLIENT_RULES_TABLE

logger = logging.getLogger(__name__)

EmbedText = Callable[[str], Sequence[float]]

METRIC_OPERATORS: dict[str, str] = {
    "cosine": "<=>",
    "l2": "<->",
    "euclidean": "<->",
    "inner_product": "<#>",
    "ip": "<#>",
}

SELECT_CLIENT_RULES = """
SELECT client_id, entry_number, source_file, content, (embedding <=> %s) AS distance
FROM client_rules
WHERE client_id = %s
ORDER BY embedding <=> %s ASC
LIMIT %s
"""


_TABLE_EXISTS_CACHE: dict[str, bool] = {}


def clear_table_cache() -> None:
    """Clear cached table existence checks (useful in migrations or tests)."""
    _TABLE_EXISTS_CACHE.clear()


def _table_exists(conn: Any, table_name: str) -> bool:
    """Check if table exists in PostgreSQL and cache the result."""
    if table_name in _TABLE_EXISTS_CACHE:
        return _TABLE_EXISTS_CACHE[table_name]
    try:
        cursor = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            (table_name,),
        )
        if hasattr(cursor, "fetchone"):
            row = cursor.fetchone()
        elif hasattr(cursor, "fetchall"):
            rows = cursor.fetchall()
            row = rows[0] if rows else None
        else:
            row = None
        exists = bool(row)
    except Exception:
        exists = False
    _TABLE_EXISTS_CACHE[table_name] = exists
    return exists


@dataclass(frozen=True)
class RetrievedRule:
    """A retrieved client rule or claim entry with vector similarity distance."""

    client_id: str
    entry_number: str
    source_file: str
    content: str
    distance: float | None = None
    retrieval_mode: str = "vector_search"

    @property
    def similarity(self) -> float | None:
        """Cosine similarity derived from distance (1 - distance)."""
        if self.distance is None:
            return None
        return 1.0 - self.distance


def _validated_query_vector(raw_vector: Any) -> tuple[float, ...]:
    """Validate query vector elements are finite numeric floats."""
    if isinstance(raw_vector, (str, bytes)) or not isinstance(raw_vector, Sequence):
        raise ValueError("query embedding must be a sequence of numbers")
    if not raw_vector:
        raise ValueError("query embedding cannot be empty")

    vector: list[float] = []
    for value in raw_vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("query embedding values must be numbers")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("query embedding values must be finite")
        vector.append(numeric)
    return tuple(vector)


def _format_query(operator: str) -> str:
    return f"""
SELECT client_id, entry_number, source_file, content, (embedding {operator} %s) AS distance
FROM client_rules
WHERE client_id = %s
ORDER BY embedding {operator} %s ASC
LIMIT %s
"""


def retrieve_client_rules(
    client_id: str,
    query_embedding: Sequence[float] | None = None,
    *,
    query_text: str | None = None,
    embed_text: EmbedText | None = None,
    top_k: int = 5,
    metric: str = "cosine",
    connection: Any = None,
) -> list[RetrievedRule]:
    """Retrieve top-k rules strictly filtered to `client_id` by vector distance.

    Never returns rules belonging to another client, even if another client's
    rule has a closer or exact matching embedding.
    """
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id must be a non-empty string")
    clean_client_id = client_id.strip()

    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")

    if metric not in METRIC_OPERATORS:
        raise ValueError(
            f"unsupported metric {metric!r}; supported: {sorted(METRIC_OPERATORS)}"
        )
    operator = METRIC_OPERATORS[metric]

    if query_embedding is None:
        if query_text is None:
            raise ValueError("either query_embedding or query_text must be provided")
        if not isinstance(query_text, str) or not query_text.strip():
            raise ValueError("query_text cannot be empty")
        if embed_text is None or not callable(embed_text):
            raise ValueError("embed_text callable must be provided when query_text is given")
        try:
            query_embedding = embed_text(query_text)
        except Exception as exc:
            raise ValueError("embedding function failed for query text") from exc

    vector = _validated_query_vector(query_embedding)

    def _execute(conn: Any) -> list[RetrievedRule]:
        if not _table_exists(conn, "client_rules"):
            return []
        try:
            register_vector(conn)
        except Exception:
            pass

        vec_param = Vector(list(vector))
        query = _format_query(operator)
        cursor = conn.execute(query, (vec_param, clean_client_id, vec_param, top_k))
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else cursor

        results: list[RetrievedRule] = []
        for row in rows:
            if isinstance(row, dict):
                cid = row["client_id"]
                entry_number = row["entry_number"]
                source_file = row["source_file"]
                content = row["content"]
                distance = float(row["distance"])
            else:
                cid, entry_number, source_file, content, distance = (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    float(row[4]),
                )

            # Defensive client isolation barrier: drop any cross-client row
            if cid != clean_client_id:
                logger.error(
                    "Isolation violation prevented: retrieved rule client_id %r does not match %r",
                    cid,
                    clean_client_id,
                )
                continue

            results.append(
                RetrievedRule(
                    client_id=cid,
                    entry_number=entry_number,
                    source_file=source_file,
                    content=content,
                    distance=distance,
                )
            )
        return results

    if connection is not None:
        return _execute(connection)
    with get_session() as session:
        return _execute(session)


def retrieve_rule_by_entry(
    client_id: str,
    entry_number: str,
    *,
    connection: Any = None,
) -> RetrievedRule | None:
    """Retrieve a single rule by client_id and exact entry_number."""
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id must be a non-empty string")
    if not isinstance(entry_number, str) or not entry_number.strip():
        raise ValueError("entry_number must be a non-empty string")
    clean_client_id = client_id.strip()
    clean_entry_number = entry_number.strip()

    def _from_files() -> RetrievedRule | None:
        from apps.worker.client_rule_loader import load_rule_entries
        try:
            entries = load_rule_entries(client_id=clean_client_id)
            for e in entries:
                if e.client_id == clean_client_id and e.entry_number == clean_entry_number:
                    return RetrievedRule(
                        client_id=e.client_id,
                        entry_number=e.entry_number,
                        source_file=e.source_file,
                        content=e.content,
                        distance=None,
                        retrieval_mode="exact_lookup",
                    )
            return None
        except Exception:
            return None

    def _execute(conn: Any) -> RetrievedRule | None:
        if not _table_exists(conn, "client_rules"):
            return _from_files()
        cursor = conn.execute(
            """
            SELECT client_id, entry_number, source_file, content
            FROM client_rules
            WHERE client_id = %s AND entry_number = %s
            """,
            (clean_client_id, clean_entry_number),
        )
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else cursor
        if not rows:
            return _from_files()
        row = rows[0]
        if isinstance(row, dict):
            cid = row["client_id"]
            entry = row["entry_number"]
            source = row["source_file"]
            content = row["content"]
        else:
            cid, entry, source, content = row[0], row[1], row[2], row[3]

        if cid != clean_client_id:
            return None

        return RetrievedRule(
            client_id=cid,
            entry_number=entry,
            source_file=source,
            content=content,
            distance=None,
            retrieval_mode="exact_lookup",
        )

    if connection is not None:
        return _execute(connection)
    with get_session() as session:
        return _execute(session)


def list_client_rules(
    client_id: str,
    *,
    connection: Any = None,
) -> list[RetrievedRule]:
    """List all stored rules for a specific client_id."""
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id must be a non-empty string")
    clean_client_id = client_id.strip()

    def _from_files() -> list[RetrievedRule]:
        from apps.worker.client_rule_loader import load_rule_entries
        try:
            entries = load_rule_entries(client_id=clean_client_id)
            return [
                RetrievedRule(
                    client_id=e.client_id,
                    entry_number=e.entry_number,
                    source_file=e.source_file,
                    content=e.content,
                    distance=None,
                    retrieval_mode="unranked_fallback",
                )
                for e in entries
                if e.client_id == clean_client_id
            ]
        except Exception:
            return []

    def _execute(conn: Any) -> list[RetrievedRule]:
        if not _table_exists(conn, "client_rules"):
            return _from_files()
        try:
            cursor = conn.execute(
                """
                SELECT client_id, entry_number, source_file, content
                FROM client_rules
                WHERE client_id = %s
                ORDER BY entry_number ASC
                """,
                (clean_client_id,),
            )
            rows = cursor.fetchall() if hasattr(cursor, "fetchall") else cursor
            if not rows:
                return _from_files()

            results: list[RetrievedRule] = []
            for row in rows:
                if isinstance(row, dict):
                    cid = row["client_id"]
                    entry_number = row["entry_number"]
                    source_file = row["source_file"]
                    content = row["content"]
                else:
                    cid, entry_number, source_file, content = row[0], row[1], row[2], row[3]

                if cid != clean_client_id:
                    continue

                results.append(
                    RetrievedRule(
                        client_id=cid,
                        entry_number=entry_number,
                        source_file=source_file,
                        content=content,
                        distance=None,
                        retrieval_mode="unranked_fallback",
                    )
                )
            return results or _from_files()
        except Exception:
            return _from_files()

    if connection is not None:
        return _execute(connection)
    with get_session() as session:
        return _execute(session)


def verify_citation(
    retrieved_rules: Sequence[RetrievedRule],
    cited_entry: str,
) -> bool:
    """Verify whether a cited rule entry number exists in retrieved rules."""
    if not cited_entry or not isinstance(cited_entry, str):
        return False
    target = cited_entry.strip()
    return any(rule.entry_number == target for rule in retrieved_rules)


def format_retrieved_rules_for_context(
    retrieved_rules: Sequence[RetrievedRule],
) -> str:
    """Format retrieved rules into text block for LLM wording check context."""
    if not retrieved_rules:
        return ""
    blocks: list[str] = []
    for rule in retrieved_rules:
        blocks.append(f"[{rule.entry_number}] ({rule.source_file}):\n{rule.content}")
    return "\n\n".join(blocks)
