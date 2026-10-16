from collections.abc import Iterator
from contextlib import contextmanager
import os

import psycopg
from dotenv import load_dotenv


def get_database_url() -> str:
    """Load and validate the PostgreSQL connection string from `.env`/environment."""
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url.startswith("DATABASE_URL="):
        database_url = database_url[len("DATABASE_URL="):].strip()
    database_url = database_url.strip(" \t\r\n\"'")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set in the environment or .env file")
    return database_url


def get_connection() -> psycopg.Connection:
    """Open a PostgreSQL connection using the configured connection string."""
    return psycopg.connect(get_database_url())


@contextmanager
def get_session() -> Iterator[psycopg.Connection]:
    """Yield a transaction-scoped connection; psycopg commits or rolls back and closes it."""
    with get_connection() as connection:
        yield connection
