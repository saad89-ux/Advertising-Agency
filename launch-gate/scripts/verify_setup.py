import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()
print("DATABASE_URL:", os.environ.get("DATABASE_URL"))
print("LANGFUSE_PUBLIC_KEY:", os.environ.get("LANGFUSE_PUBLIC_KEY"))
print("LANGFUSE_HOST:", os.environ.get("LANGFUSE_HOST"))
print("GEMINI_MODEL:", os.environ.get("GEMINI_MODEL"))

from apps.api.database import get_session
from apps.api.schema import init_schema
from apps.api.users import create_users_table, get_user_by_email, create_user, assign_user_to_client
from apps.worker.tracing import create_trace_tables

init_schema()
create_users_table()
create_trace_tables()

with get_session() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;")
        tables = [r[0] for r in cur.fetchall()]
        print("Initialized tables in database:", tables)

m = get_user_by_email("manager@launchgate.com")
b = get_user_by_email("buyer@launchgate.com")
print("Demo Manager:", m["email"] if m else "Missing")
print("Demo Buyer:", b["email"] if b else "Missing")

from apps.api.diagnostics import get_ai_and_tracing_status
print("Diagnostics API status:", get_ai_and_tracing_status())
