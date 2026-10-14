from apps.api.clients import create_client_tables
from apps.api.batches import create_batches_table
from apps.api.database import get_session


CREATE_ADS_TABLE = """
CREATE TABLE IF NOT EXISTS ads (
    ad_id TEXT PRIMARY KEY CHECK (length(trim(ad_id)) > 0),
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id),
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    platform TEXT NOT NULL CHECK (length(trim(platform)) > 0),
    platform_raw TEXT,
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

INSERT_AD = """
INSERT INTO ads (
    ad_id, batch_id, client_id, row_number, platform, platform_raw, ad_text, headline,
    destination_url, creative_file, row_hash, sheet_hash
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

ENSURE_PLATFORM_RAW_COLUMN = "ALTER TABLE ads ADD COLUMN IF NOT EXISTS platform_raw TEXT"

ENSURE_ADS_BATCH_FOREIGN_KEY = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ads_batch_id_fkey'
          AND conrelid = 'ads'::regclass
    ) THEN
        ALTER TABLE ads ADD CONSTRAINT ads_batch_id_fkey
            FOREIGN KEY (batch_id) REFERENCES batches (batch_id) ON DELETE CASCADE NOT VALID;
    END IF;
END $$
"""


def create_ads_table() -> None:
    """Create the ads table after ensuring its clients-table dependency exists."""
    create_batches_table()
    create_client_tables()
    with get_session() as connection:
        connection.execute(CREATE_ADS_TABLE)
        connection.execute(ENSURE_ADS_BATCH_FOREIGN_KEY)


def ensure_platform_raw_column() -> None:
    """Add the raw platform column to databases created before this schema change."""
    with get_session() as connection:
        connection.execute(ENSURE_PLATFORM_RAW_COLUMN)


def persist_ads_on_connection(connection, ads: list[dict[str, object]]) -> None:
    for ad in ads:
        connection.execute(
            INSERT_AD,
            (
                ad["ad_id"],
                ad["batch_id"],
                ad["client_id"],
                ad["row_number"],
                ad["platform"],
                ad.get("platform_raw", ad["platform"]),
                ad.get("ad_text"),
                ad.get("headline"),
                ad["destination_url"],
                ad.get("creative_file", ""),
                ad["row_hash"],
                ad["sheet_hash"],
            ),
        )
