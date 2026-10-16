from apps.api.database import get_session
from apps.api.users import CREATE_USERS_TABLE


CREATE_CLIENTS_TABLE = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY CHECK (length(trim(client_id)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0)
)
"""

CREATE_USER_CLIENT_ASSIGNMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS user_client_assignments (
    user_id TEXT NOT NULL CHECK (length(trim(user_id)) > 0)
        REFERENCES users (user_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL
        REFERENCES clients (client_id)
        ON DELETE CASCADE,
    PRIMARY KEY (user_id, client_id)
)
"""

ENSURE_ASSIGNMENT_USER_FOREIGN_KEY = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'user_client_assignments_user_id_fkey'
          AND conrelid = 'user_client_assignments'::regclass
    ) THEN
        ALTER TABLE user_client_assignments
            ADD CONSTRAINT user_client_assignments_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
    END IF;
END $$
"""


def create_client_tables() -> None:
    """Create the client and user-to-client assignment tables if absent."""
    with get_session() as connection:
        connection.execute(CREATE_CLIENTS_TABLE)
        connection.execute(CREATE_USERS_TABLE)
        connection.execute(CREATE_USER_CLIENT_ASSIGNMENTS_TABLE)
        connection.execute(ENSURE_ASSIGNMENT_USER_FOREIGN_KEY)


def list_clients() -> list[dict[str, str]]:
    """Return all clients registered in the clients table."""
    with get_session() as connection:
        rows = connection.execute(
            "SELECT client_id, name FROM clients ORDER BY name ASC;"
        ).fetchall()
    return [{"client_id": str(r[0]), "name": str(r[1])} for r in rows]

