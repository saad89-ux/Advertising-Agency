"""PostgreSQL persistence for rows isolated during batch upload parsing."""

import json

from apps.api.database import get_session
from apps.api.ingest import RejectedRow
from psycopg import Connection


CREATE_REJECTED_ROWS_TABLE = """
CREATE TABLE IF NOT EXISTS batch_rejected_rows (
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    data_row_index INTEGER NOT NULL CHECK (data_row_index > 0),
    row_number JSONB,
    reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    raw_cells JSONB NOT NULL,
    PRIMARY KEY (batch_id, data_row_index)
)
"""

INSERT_REJECTED_ROW = """
INSERT INTO batch_rejected_rows (
    batch_id, data_row_index, row_number, reason_code, reason, raw_cells
)
VALUES (%s, %s, %s::jsonb, %s, %s, %s::jsonb)
ON CONFLICT (batch_id, data_row_index) DO NOTHING
"""

ENSURE_REJECTED_BATCH_FOREIGN_KEY = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'batch_rejected_rows_batch_id_fkey'
          AND conrelid = 'batch_rejected_rows'::regclass
    ) THEN
        ALTER TABLE batch_rejected_rows
            ADD CONSTRAINT batch_rejected_rows_batch_id_fkey
            FOREIGN KEY (batch_id) REFERENCES batches (batch_id) ON DELETE CASCADE NOT VALID;
    END IF;
END $$
"""


def create_rejected_rows_table() -> None:
    """Create the rejected-row table if it does not exist."""
    with get_session() as connection:
        connection.execute(CREATE_REJECTED_ROWS_TABLE)
        connection.execute(ENSURE_REJECTED_BATCH_FOREIGN_KEY)


def persist_rejected_rows(batch_id: str, rejected_rows: list[RejectedRow]) -> None:
    """Store rejected row details under the upload's batch ID in one transaction."""
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("batch_id must be a non-empty string")

    with get_session() as connection:
        persist_rejected_rows_on_connection(connection, batch_id, rejected_rows)


def persist_rejected_rows_on_connection(
    connection: Connection, batch_id: str, rejected_rows: list[RejectedRow]
) -> None:
    """Persist rejected rows using a caller-owned transaction."""
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("batch_id must be a non-empty string")

    for row in rejected_rows:
        connection.execute(
            INSERT_REJECTED_ROW,
            (
                batch_id,
                row.data_row_index,
                json.dumps(row.row_number, ensure_ascii=False, default=str),
                row.reason_code,
                row.reason,
                json.dumps(list(row.raw_cells), ensure_ascii=False),
            ),
        )
