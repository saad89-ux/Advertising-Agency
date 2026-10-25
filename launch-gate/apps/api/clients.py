from apps.api.database import get_session
from apps.api.users import CREATE_USERS_TABLE


CREATE_CLIENTS_TABLE = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY CHECK (length(trim(client_id)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    meta_ad_account_id TEXT
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
        connection.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS meta_ad_account_id TEXT")
        connection.execute(META_WORKSPACE_CONNECTIONS_TABLE)
        connection.execute(WORKSPACE_META_CONNECTIONS_MIGRATION)
        connection.execute(CREATE_USERS_TABLE)
        connection.execute(CREATE_USER_CLIENT_ASSIGNMENTS_TABLE)
        connection.execute(ENSURE_ASSIGNMENT_USER_FOREIGN_KEY)


META_WORKSPACE_CONNECTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS meta_workspace_connections (
    workspace_id TEXT PRIMARY KEY REFERENCES clients(client_id) ON DELETE CASCADE,
    ad_account_id TEXT NOT NULL,
    connection_type TEXT NOT NULL DEFAULT 'AGENCY',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (connection_type IN ('AGENCY'))
)
"""

WORKSPACE_META_CONNECTIONS_MIGRATION = """
CREATE TABLE IF NOT EXISTS workspace_meta_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT NOT NULL UNIQUE REFERENCES clients(client_id) ON DELETE CASCADE,
    meta_ad_account_id VARCHAR(255) NOT NULL,
    token_type VARCHAR(50) NOT NULL DEFAULT 'AGENCY_SYSTEM_USER',
    secret_manager_ref VARCHAR(255),
    connection_health VARCHAR(50) NOT NULL DEFAULT 'UNKNOWN'
        CHECK (connection_health IN ('UNKNOWN', 'HEALTHY', 'DEGRADED', 'UNAUTHORIZED')),
    verified_scopes TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    last_verified_at TIMESTAMPTZ,
    token_expires_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (token_type = 'AGENCY_SYSTEM_USER')
);
CREATE INDEX IF NOT EXISTS idx_workspace_meta_connections_workspace
    ON workspace_meta_connections(workspace_id);
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS token_type VARCHAR(50) NOT NULL DEFAULT 'AGENCY_SYSTEM_USER';
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS secret_manager_ref VARCHAR(255);
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS connection_health VARCHAR(50) NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS verified_scopes TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[];
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ;
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMPTZ;
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE workspace_meta_connections ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_meta_connections_id ON workspace_meta_connections(id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_meta_connections_workspace ON workspace_meta_connections(workspace_id);
INSERT INTO workspace_meta_connections (workspace_id, meta_ad_account_id)
SELECT c.client_id, COALESCE(NULLIF(m.ad_account_id, ''), NULLIF(c.meta_ad_account_id, ''))
FROM clients c
LEFT JOIN meta_workspace_connections m ON m.workspace_id = c.client_id AND m.status = 'active'
WHERE COALESCE(NULLIF(m.ad_account_id, ''), NULLIF(c.meta_ad_account_id, '')) IS NOT NULL
ON CONFLICT (workspace_id) DO NOTHING;
"""


def normalize_meta_account_id(value: str) -> str:
    """Normalize Meta's act_<numeric> form to the numeric account ID used in tool calls."""
    normalized = str(value or "").strip().removeprefix("act_")
    if not normalized.isdigit():
        raise ValueError("Meta ad account ID must be numeric or use the act_<numeric> format.")
    return normalized


def resolve_client_meta_account(connection, client_id: str) -> str:
    """Return the authorized client's configured Meta account ID."""
    row = connection.execute(
        "SELECT meta_ad_account_id, connection_health FROM workspace_meta_connections WHERE workspace_id=%s",
        (client_id,),
    ).fetchone()
    if row is None:
        row = connection.execute("SELECT ad_account_id FROM meta_workspace_connections WHERE workspace_id=%s", (client_id,)).fetchone()
    if row is None:
        row = connection.execute("SELECT meta_ad_account_id FROM clients WHERE client_id=%s", (client_id,)).fetchone()
    if row is not None and len(row) > 1 and row[1] == "UNAUTHORIZED":
        raise ValueError(f"Meta connection is unauthorized for client {client_id}.")
    account_id = str(row[0] or "").strip() if row else ""
    if not account_id:
        raise ValueError(f"Meta ad account is not configured for client {client_id}.")
    try:
        return normalize_meta_account_id(account_id)
    except ValueError as exc:
        raise ValueError(f"Meta ad account mapping is invalid for client {client_id}." ) from exc


def get_workspace_meta_connection(connection, workspace_id: str) -> dict[str, object] | None:
    """Load non-secret connection metadata for an authorized workspace."""
    row = connection.execute(
        """SELECT meta_ad_account_id, token_type, connection_health, verified_scopes,
                  last_verified_at, token_expires_at, error_message
           FROM workspace_meta_connections WHERE workspace_id=%s""",
        (workspace_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(zip((
        "meta_ad_account_id", "token_type", "connection_health", "verified_scopes",
        "last_verified_at", "token_expires_at", "error_message",
    ), row))


def update_workspace_meta_health(
    connection,
    workspace_id: str,
    *,
    health: str,
    verified_scopes: list[str] | None = None,
    error_message: str | None = None,
) -> None:
    """Persist sanitized verification metadata without storing credentials."""
    if health not in {"UNKNOWN", "HEALTHY", "DEGRADED", "UNAUTHORIZED"}:
        raise ValueError("Invalid Meta connection health state.")
    safe_error = " ".join(error_message.split())[:500] if error_message else None
    connection.execute(
        """UPDATE workspace_meta_connections SET connection_health=%s,
           verified_scopes=%s, last_verified_at=NOW(), error_message=%s, updated_at=NOW()
           WHERE workspace_id=%s""",
        (health, verified_scopes or [], safe_error, workspace_id),
    )


def list_clients() -> list[dict[str, str]]:
    """Return all clients registered in the clients table."""
    with get_session() as connection:
        rows = connection.execute(
            "SELECT client_id, name FROM clients ORDER BY name ASC;"
        ).fetchall()
    return [{"client_id": str(r[0]), "name": str(r[1])} for r in rows]

