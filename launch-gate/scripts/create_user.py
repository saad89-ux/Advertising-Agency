"""One-off real account provisioning: python -m scripts.create_user --help."""
import argparse
import getpass
import uuid

from dotenv import load_dotenv
from apps.api.auth import hash_password
from apps.api.database import get_session
from apps.api.schema import init_schema


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=["buyer", "account_manager"], required=True)
    parser.add_argument("--client-id", action="append", required=True)
    args = parser.parse_args()
    if "@" not in args.email:
        parser.error("A valid email is required.")
    password = getpass.getpass("Password: ")
    if len(password) < 8:
        parser.error("Password must contain at least eight characters.")
    init_schema()
    user_id = str(uuid.uuid4())
    with get_session() as conn:
        for client_id in args.client_id:
            if not conn.execute("SELECT 1 FROM clients WHERE client_id = %s", (client_id,)).fetchone():
                parser.error(f"Unknown client: {client_id}. Create the real client first.")
        conn.execute("INSERT INTO users (user_id,email,display_name,password_hash,role) VALUES (%s,%s,%s,%s,%s)",
                     (user_id,args.email,args.name,hash_password(password),args.role))
        for client_id in args.client_id:
            conn.execute("INSERT INTO user_client_assignments (user_id,client_id) VALUES (%s,%s)", (user_id,client_id))
    print(f"Created user {args.email}.")


if __name__ == "__main__":
    main()
