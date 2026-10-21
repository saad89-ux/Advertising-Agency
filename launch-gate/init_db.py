import os
from dotenv import load_dotenv
import psycopg
from pgvector.psycopg import register_vector

# Load environment variables from your .env file
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:mysecretpassword@localhost:5432/launch_gate_test")

def init_database():
    print("Connecting to Docker PostgreSQL database...")
    # Connect using psycopg 3
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # 1. Enable the vector extension safely
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            print("✅ pgvector extension verified/enabled.")
            
            # 2. Create a test items table with an embedding column (e.g., 3 dimensions)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    embedding vector(3)
                );
            """)
            print("✅ 'items' table created successfully.")
            conn.commit()

if __name__ == "__main__":
    init_database()
