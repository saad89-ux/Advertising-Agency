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

    assert len(statements) == 4
    assert "CREATE TABLE IF NOT EXISTS clients" in statements[0]
    assert "client_id TEXT PRIMARY KEY" in statements[0]
    assert "name TEXT NOT NULL" in statements[0]
    assert "CREATE TABLE IF NOT EXISTS users" in statements[1]
    assert "email TEXT NOT NULL UNIQUE" in statements[1]
    assert "CREATE TABLE IF NOT EXISTS user_client_assignments" in statements[2]
    assert "PRIMARY KEY (user_id, client_id)" in statements[2]
    assert "REFERENCES clients (client_id)" in statements[2]
    assert "REFERENCES users (user_id)" in statements[2]
    assert "ON DELETE CASCADE" in statements[2]
    assert "CHECK (length(trim(user_id)) > 0)" in statements[2]
    assert "FOREIGN KEY (user_id) REFERENCES users (user_id)" in statements[3]
