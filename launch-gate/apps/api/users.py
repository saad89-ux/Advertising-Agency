"""User schema and database helpers; passwords are handled by the auth module."""

from __future__ import annotations

from typing import Literal

from apps.api.database import get_session


Role = Literal["buyer", "account_manager"]
VALID_ROLES = {"buyer", "account_manager"}

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY CHECK (length(trim(user_id)) > 0),
    email TEXT NOT NULL UNIQUE CHECK (length(trim(email)) > 0),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    password_hash TEXT NOT NULL CHECK (length(trim(password_hash)) > 0),
    role TEXT NOT NULL CHECK (role IN ('buyer', 'account_manager')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE
)
"""


def create_users_table() -> None:
    """Create users and dependent client-assignment tables idempotently."""
    from apps.api.clients import create_client_tables

    create_client_tables()


def create_user(
    *,
    user_id: str,
    email: str,
    display_name: str,
    password_hash: str,
    role: Role,
    is_active: bool = True,
) -> None:
    """Insert or update a user without ever accepting a cleartext password."""
    if role not in VALID_ROLES:
        raise ValueError("role must be buyer or account_manager")
    if not all(isinstance(value, str) and value.strip() for value in (user_id, email, display_name, password_hash)):
        raise ValueError("user_id, email, display_name, and password_hash are required")
    with get_session() as connection:
        connection.execute(
            """INSERT INTO users (user_id, email, display_name, password_hash, role, is_active)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                 email = EXCLUDED.email,
                 display_name = EXCLUDED.display_name,
                 password_hash = EXCLUDED.password_hash,
                 role = EXCLUDED.role,
                 is_active = EXCLUDED.is_active""",
            (user_id, email, display_name, password_hash, role, is_active),
        )


def get_user_by_email(email: str) -> dict[str, object] | None:
    with get_session() as connection:
        row = connection.execute(
            """SELECT user_id, email, display_name, password_hash, role, is_active
               FROM users WHERE email = %s""",
            (email,),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(("user_id", "email", "display_name", "password_hash", "role", "is_active"), row))


def get_user_by_id(user_id: str) -> dict[str, object] | None:
    with get_session() as connection:
        row = connection.execute(
            """SELECT user_id, email, display_name, role, is_active
               FROM users WHERE user_id = %s""",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(("user_id", "email", "display_name", "role", "is_active"), row))


def get_user_client_ids(user_id: str) -> list[str]:
    with get_session() as connection:
        rows = connection.execute(
            "SELECT client_id FROM user_client_assignments WHERE user_id = %s ORDER BY client_id",
            (user_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def assign_user_to_client(user_id: str, client_id: str) -> None:
    with get_session() as connection:
        connection.execute(
            """INSERT INTO user_client_assignments (user_id, client_id)
               VALUES (%s, %s) ON CONFLICT (user_id, client_id) DO NOTHING""",
            (user_id, client_id),
        )
