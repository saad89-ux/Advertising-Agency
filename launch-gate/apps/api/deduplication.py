"""Client-scoped sheet deduplication primitives."""

from psycopg import Connection

from apps.api.ads import create_ads_table
from apps.api.batches import create_batches_table
from apps.api.database import get_session


CREATE_SHEET_CLAIMS_TABLE = """
CREATE TABLE IF NOT EXISTS sheet_claims (
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    sheet_hash TEXT NOT NULL CHECK (length(trim(sheet_hash)) > 0),
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    PRIMARY KEY (client_id, sheet_hash)
)
"""


def create_deduplication_constraints() -> None:
    """Ensure dependencies exist, then create per-client sheet claim storage."""
    create_batches_table()
    create_ads_table()
    with get_session() as connection:
        connection.execute(CREATE_SHEET_CLAIMS_TABLE)


def claim_sheet(
    connection: Connection, client_id: str, sheet_hash: str, batch_id: str
) -> str | None:
    """Claim a sheet in the caller's transaction; return prior batch ID on repeat."""
    if not client_id.strip() or not sheet_hash.strip() or not batch_id.strip():
        raise ValueError("client_id, sheet_hash, and batch_id must be non-empty")
    result = connection.execute(
        """INSERT INTO sheet_claims (client_id, sheet_hash, batch_id)
           VALUES (%s, %s, %s) ON CONFLICT (client_id, sheet_hash) DO NOTHING
           RETURNING batch_id""",
        (client_id, sheet_hash, batch_id),
    )
    if result.fetchone() is not None:
        return None
    prior = connection.execute(
        "SELECT batch_id FROM sheet_claims WHERE client_id = %s AND sheet_hash = %s",
        (client_id, sheet_hash),
    ).fetchone()
    return str(prior[0]) if prior else None
