from apps.api.database import get_session


CREATE_CLIENTS_TABLE = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY CHECK (length(trim(client_id)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0)
)
"""

CREATE_USER_CLIENT_ASSIGNMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS user_client_assignments (
    user_id TEXT NOT NULL CHECK (length(trim(user_id)) > 0),
    client_id TEXT NOT NULL
        REFERENCES clients (client_id)
        ON DELETE CASCADE,
    PRIMARY KEY (user_id, client_id)
)
"""


def create_client_tables() -> None:
    """Create the client and user-to-client assignment tables if absent."""
    with get_session() as connection:
        connection.execute(CREATE_CLIENTS_TABLE)
        connection.execute(CREATE_USER_CLIENT_ASSIGNMENTS_TABLE)
