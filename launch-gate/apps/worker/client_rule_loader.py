"""Load numbered client rule entries and embeddings into PostgreSQL pgvector."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any

from pgvector import Vector
from pgvector.psycopg import register_vector

from apps.api.database import get_session


_ENTRY_HEADING = re.compile(r"^##\s+([A-Z]{2}-BB-[\w.]+|[A-Z]{2}-C-[\w.-]+)(?:\s|$).*$")
_ALLOWED_ID = re.compile(r"^(?:[A-Z]{2}-BB-[\w.]+|[A-Z]{2}-C-[\w.-]+)$")

CREATE_CLIENT_RULES_TABLE = """
CREATE TABLE IF NOT EXISTS client_rules (
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    entry_number TEXT NOT NULL CHECK (length(trim(entry_number)) > 0),
    source_file TEXT NOT NULL CHECK (length(trim(source_file)) > 0),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    embedding vector NOT NULL,
    PRIMARY KEY (client_id, entry_number)
)
"""

DELETE_CLIENT_RULES = "DELETE FROM client_rules WHERE client_id = %s"
INSERT_CLIENT_RULE = """
INSERT INTO client_rules (client_id, entry_number, source_file, content, embedding)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (client_id, entry_number) DO UPDATE SET
    source_file = EXCLUDED.source_file,
    content = EXCLUDED.content,
    embedding = EXCLUDED.embedding
"""
SELECT_EXISTING_VECTOR_DIMENSIONS = """
SELECT DISTINCT vector_dims(embedding) AS dimensions
FROM client_rules
"""


@dataclass(frozen=True)
class ClientRuleEntry:
    client_id: str
    entry_number: str
    source_file: str
    content: str


EmbedText = Callable[[str], Sequence[float]]


def _default_rules_root() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "clients"


def _read_rule_index(rules_root: Path) -> dict[str, dict[str, list[str]]]:
    index_path = rules_root / "rule_index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"client rule index is unreadable: {index_path.name}") from exc
    if not isinstance(index, dict):
        raise ValueError("client rule index must contain a JSON object")

    normalized: dict[str, dict[str, list[str]]] = {}
    for client_id, documents in index.items():
        if client_id == "_note":
            continue
        if not isinstance(client_id, str) or not isinstance(documents, dict):
            raise ValueError("client rule index contains an invalid client entry")
        client_docs: dict[str, list[str]] = {}
        for doc_key in ("brand_book", "register"):
            entries = documents.get(doc_key)
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"client rule index is missing {client_id}.{doc_key} entries")
            if any(not isinstance(entry, str) or not _ALLOWED_ID.fullmatch(entry) for entry in entries):
                raise ValueError(f"client rule index contains an invalid entry number for {client_id}")
            if len(entries) != len(set(entries)):
                raise ValueError(f"client rule index contains duplicate entry numbers for {client_id}")
            client_docs[doc_key] = entries
        normalized[client_id] = client_docs
    if not normalized:
        raise ValueError("client rule index contains no clients")
    return normalized


def _extract_document_entries(path: Path, expected_ids: list[str]) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"client rule document is unreadable: {path.name}") from exc

    found: dict[str, str] = {}
    active_id: str | None = None
    active_lines: list[str] = []

    def finish_entry() -> None:
        if active_id is not None:
            content = "\n".join(active_lines).strip()
            if not content:
                raise ValueError(f"client rule entry {active_id} is empty")
            if active_id in found:
                raise ValueError(f"client rule entry {active_id} appears more than once")
            found[active_id] = content

    for line in text.splitlines():
        heading = _ENTRY_HEADING.match(line)
        if heading:
            finish_entry()
            active_id = heading.group(1)
            if active_id not in expected_ids:
                raise ValueError(f"client rule document contains unindexed entry {active_id}")
            active_lines = [line]
        elif active_id is not None:
            active_lines.append(line)
    finish_entry()

    missing = [entry_id for entry_id in expected_ids if entry_id not in found]
    if missing:
        raise ValueError(
            f"client rule document {path.name} is missing indexed entries: "
            + ", ".join(missing)
        )
    return found


def load_rule_entries(
    *, rules_root: str | Path | None = None, client_id: str | None = None
) -> list[ClientRuleEntry]:
    """Read indexed Markdown sections without changing source files."""
    root = Path(rules_root) if rules_root is not None else _default_rules_root()
    index = _read_rule_index(root)
    if client_id is not None and client_id not in index:
        raise ValueError(f"unknown client in rule index: {client_id}")

    selected_clients = [client_id] if client_id is not None else list(index)
    entries: list[ClientRuleEntry] = []
    for selected_client in selected_clients:
        documents = index[selected_client]
        for doc_key, filename in (
            ("brand_book", "brand_book.md"),
            ("register", "claims_register.md"),
        ):
            expected_ids = documents[doc_key]
            extracted = _extract_document_entries(
                root / selected_client / filename,
                expected_ids,
            )
            for entry_number in expected_ids:
                entries.append(
                    ClientRuleEntry(
                        client_id=selected_client,
                        entry_number=entry_number,
                        source_file=filename,
                        content=extracted[entry_number],
                    )
                )
    return entries


def _validated_vector(embed_text: EmbedText, content: str) -> tuple[float, ...]:
    try:
        raw_vector = embed_text(content)
    except Exception as exc:
        raise ValueError("embedding function failed for a client rule entry") from exc
    if isinstance(raw_vector, (str, bytes)) or not isinstance(raw_vector, Sequence):
        raise ValueError("embedding function must return a sequence of numbers")
    if not raw_vector:
        raise ValueError("embedding function returned an empty vector")

    vector: list[float] = []
    for value in raw_vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("embedding vector values must be numbers")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("embedding vector values must be finite")
        vector.append(numeric)
    return tuple(vector)


def load_client_rules(
    embed_text: EmbedText,
    *,
    client_id: str | None = None,
    rules_root: str | Path | None = None,
) -> int:
    """Embed indexed rule entries and replace their PostgreSQL records atomically.

    `embed_text` is required so this loader does not choose an undocumented model or
    provider. All embeddings are validated before opening the database transaction.
    """
    if not callable(embed_text):
        raise TypeError("embed_text must be callable")
    entries = load_rule_entries(rules_root=rules_root, client_id=client_id)
    prepared: list[tuple[ClientRuleEntry, tuple[float, ...]]] = []
    dimensions: set[int] = set()
    for entry in entries:
        vector = _validated_vector(embed_text, entry.content)
        dimensions.add(len(vector))
        prepared.append((entry, vector))
    if len(dimensions) > 1:
        raise ValueError("embedding function returned vectors with inconsistent dimensions")

    clients_to_replace = sorted({entry.client_id for entry, _ in prepared})
    with get_session() as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        connection.execute(CREATE_CLIENT_RULES_TABLE)
        register_vector(connection)

        existing_dimensions = {
            row["dimensions"] if isinstance(row, dict) else row[0]
            for row in connection.execute(SELECT_EXISTING_VECTOR_DIMENSIONS).fetchall()
        }
        if existing_dimensions and dimensions and existing_dimensions != dimensions:
            raise ValueError(
                "embedding dimension differs from the vectors already stored in client_rules"
            )

        for selected_client in clients_to_replace:
            connection.execute(DELETE_CLIENT_RULES, (selected_client,))
        for entry, vector in prepared:
            connection.execute(
                INSERT_CLIENT_RULE,
                (
                    entry.client_id,
                    entry.entry_number,
                    entry.source_file,
                    entry.content,
                    Vector(list(vector)),
                ),
            )
    return len(prepared)
