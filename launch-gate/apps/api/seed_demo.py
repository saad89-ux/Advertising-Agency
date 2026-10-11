"""Interactive seed for SYNTHETIC local pilot clients and demo users."""

from __future__ import annotations

from getpass import getpass

from apps.api.auth import hash_password
from apps.api.client_inference import load_domain_map
from apps.api.clients import create_client_tables
from apps.api.database import get_session
from apps.api.users import assign_user_to_client, create_user, create_users_table


def _prompt_demo_user(
    display_name: str, *, optional: bool = False
) -> tuple[str, str] | None:
    prompt = f"{display_name} email"
    if optional:
        prompt += " (blank to skip)"
    email = input(prompt + ": ").strip()
    if not email:
        if optional:
            return None
        raise ValueError(f"an email is required for {display_name}")
    password = getpass(f"{display_name} password (input hidden): ")
    if not password:
        raise ValueError("a non-empty password is required")
    return email, password


def main() -> None:
    print("SYNTHETIC local seed; no real client or user data is included.")
    clients = sorted({value for value in load_domain_map().values() if value is not None})
    create_users_table()
    with get_session() as connection:
        for client_id in clients:
            connection.execute(
                """INSERT INTO clients (client_id, name) VALUES (%s, %s)
                   ON CONFLICT (client_id) DO UPDATE SET name = EXCLUDED.name""",
                (client_id, client_id),
            )

    for client_id in clients:
        user_id = f"synthetic-buyer-{client_id}"
        details = _prompt_demo_user(f"SYNTHETIC buyer for {client_id}")
        if details:
            email, password = details
            create_user(
                user_id=user_id,
                email=email,
                display_name=f"SYNTHETIC buyer for {client_id}",
                password_hash=hash_password(password),
                role="buyer",
            )
            assign_user_to_client(user_id, client_id)

    for role, user_id, display_name in (
        ("account_manager", "synthetic-account-manager", "SYNTHETIC account manager"),
        ("admin", "synthetic-admin", "SYNTHETIC admin"),
    ):
        details = _prompt_demo_user(display_name, optional=True)
        if details:
            email, password = details
            create_user(
                user_id=user_id,
                email=email,
                display_name=display_name,
                password_hash=hash_password(password),
                role=role,
            )
            if role == "account_manager":
                for client_id in clients:
                    assign_user_to_client(user_id, client_id)

    print("SYNTHETIC clients and the entered demo users are seeded.")


if __name__ == "__main__":
    main()
