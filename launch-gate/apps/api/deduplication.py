"""Client-scoped sheet and row deduplication primitives."""

from collections.abc import Sequence

from apps.api.clients import create_client_tables
from apps.api.database import get_session
from apps.api.ingest import hash_normalized_row


CREATE_INGESTED_SHEETS_TABLE = """
CREATE TABLE IF NOT EXISTS ingested_sheets (
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    sheet_hash TEXT NOT NULL CHECK (length(trim(sheet_hash)) > 0),
    PRIMARY KEY (client_id, sheet_hash)
)
"""

CREATE_CLIENT_ROW_HASH_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ads_client_row_hash_unique
ON ads (client_id, row_hash)
"""


def create_deduplication_constraints() -> None:
    """Create sheet identity storage and enforce client-scoped row uniqueness."""
    create_client_tables()
    with get_session() as connection:
        connection.execute(CREATE_INGESTED_SHEETS_TABLE)
        connection.execute(CREATE_CLIENT_ROW_HASH_INDEX)


def claim_sheet(client_id: str, sheet_hash: str) -> bool:
    """Atomically record a client sheet hash; return False when already seen."""
    if not client_id.strip() or not sheet_hash.strip():
        raise ValueError("client_id and sheet_hash must be non-empty")
    with get_session() as connection:
        result = connection.execute(
            """INSERT INTO ingested_sheets (client_id, sheet_hash)
               VALUES (%s, %s) ON CONFLICT (client_id, sheet_hash) DO NOTHING
               RETURNING sheet_hash""",
            (client_id, sheet_hash),
        )
        return result.fetchone() is not None


def filter_unseen_rows(
    client_id: str, rows: Sequence[dict[str, str]]
) -> list[dict[str, str]]:
    """Return input rows whose client-scoped hashes are not already in ads.

    The unique index created by ``create_deduplication_constraints`` remains the
    final concurrency guard when callers persist returned rows.
    """
    if not client_id.strip():
        raise ValueError("client_id must be non-empty")
    hashes = [hash_normalized_row(row) for row in rows]
    if not hashes:
        return []
    with get_session() as connection:
        result = connection.execute(
            "SELECT row_hash FROM ads WHERE client_id = %s AND row_hash = ANY(%s)",
            (client_id, hashes),
        )
        existing = {record[0] for record in result.fetchall()}
    seen = set(existing)
    unique_rows: list[dict[str, str]] = []
    for row, row_hash in zip(rows, hashes):
        if row_hash not in seen:
            unique_rows.append(row)
            seen.add(row_hash)
    return unique_rows
