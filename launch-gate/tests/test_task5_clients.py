from contextlib import contextmanager

from apps.api import clients


def test_create_client_tables_issues_idempotent_schema(monkeypatch):
    statements = []

    class FakeConnection:
        def execute(self, statement):
            statements.append(statement)

    @contextmanager
    def fake_session():
        yield FakeConnection()

    monkeypatch.setattr(clients, "get_session", fake_session)

    clients.create_client_tables()

    schema_sql = "\n".join(statements)
    assert "CREATE TABLE IF NOT EXISTS clients" in schema_sql
    assert "client_id TEXT PRIMARY KEY" in schema_sql
    assert "name TEXT NOT NULL" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS users" in schema_sql
    assert "email TEXT NOT NULL UNIQUE" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS user_client_assignments" in schema_sql
    assert "PRIMARY KEY (user_id, client_id)" in schema_sql
    assert "REFERENCES clients (client_id)" in schema_sql
    assert "REFERENCES users (user_id)" in schema_sql
    assert "ON DELETE CASCADE" in schema_sql
    assert "CHECK (length(trim(user_id)) > 0)" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS meta_workspace_connections" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS workspace_meta_connections" in schema_sql
    assert "FOREIGN KEY (user_id) REFERENCES users (user_id)" in schema_sql
