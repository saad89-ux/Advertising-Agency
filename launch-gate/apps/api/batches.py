"""Batch and per-client sheet-claim persistence."""

from apps.api.clients import create_client_tables
from apps.api.database import get_session


CREATE_BATCHES_TABLE = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY CHECK (length(trim(batch_id)) > 0),
    uploaded_by TEXT NOT NULL REFERENCES users (user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    filename TEXT NOT NULL CHECK (length(filename) <= 255),
    sheet_hash TEXT NOT NULL CHECK (length(trim(sheet_hash)) > 0)
)
"""


def create_batches_table() -> None:
    create_client_tables()
    with get_session() as connection:
        connection.execute(CREATE_BATCHES_TABLE)


def create_batch_on_connection(
    connection, *, batch_id: str, uploaded_by: str, filename: str, sheet_hash: str
) -> None:
    connection.execute(
        """INSERT INTO batches (batch_id, uploaded_by, filename, sheet_hash)
           VALUES (%s, %s, %s, %s)""",
        (batch_id, uploaded_by, filename, sheet_hash),
    )
