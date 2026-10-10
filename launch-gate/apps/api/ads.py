from apps.api.clients import create_client_tables
from apps.api.database import get_session


CREATE_ADS_TABLE = """
CREATE TABLE IF NOT EXISTS ads (
    ad_id TEXT PRIMARY KEY CHECK (length(trim(ad_id)) > 0),
    batch_id TEXT NOT NULL CHECK (length(trim(batch_id)) > 0),
    client_id TEXT NOT NULL REFERENCES clients (client_id),
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    platform TEXT NOT NULL CHECK (length(trim(platform)) > 0),
    ad_text TEXT,
    headline TEXT,
    destination_url TEXT NOT NULL CHECK (length(trim(destination_url)) > 0),
    creative_file TEXT,
    row_hash TEXT NOT NULL CHECK (length(trim(row_hash)) > 0),
    sheet_hash TEXT NOT NULL CHECK (length(trim(sheet_hash)) > 0),
    CHECK (
        (ad_text IS NOT NULL AND length(trim(ad_text)) > 0)
        OR (headline IS NOT NULL AND length(trim(headline)) > 0)
    )
)
"""


def create_ads_table() -> None:
    """Create the ads table after ensuring its clients-table dependency exists."""
    create_client_tables()
    with get_session() as connection:
        connection.execute(CREATE_ADS_TABLE)
