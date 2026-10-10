import pytest

from apps.api import database


def test_database_url_requires_nonblank_configuration(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(database, "load_dotenv", lambda: False)

    with pytest.raises(RuntimeError, match="DATABASE_URL must be set"):
        database.get_database_url()

    monkeypatch.setenv("DATABASE_URL", "   ")
    with pytest.raises(RuntimeError, match="DATABASE_URL must be set"):
        database.get_database_url()


def test_database_url_uses_environment_value(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", " postgresql://localhost/launch_gate ")
    monkeypatch.setattr(database, "load_dotenv", lambda: False)

    assert database.get_database_url() == "postgresql://localhost/launch_gate"


def test_database_url_loads_dotenv_before_reading(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    calls = []

    def fake_load_dotenv():
        calls.append(True)
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/from-dotenv")

    monkeypatch.setattr(database, "load_dotenv", fake_load_dotenv)

    assert database.get_database_url() == "postgresql://localhost/from-dotenv"
    assert calls == [True]


def test_get_connection_passes_configured_url_to_psycopg(monkeypatch):
    sentinel = object()
    calls = []
    monkeypatch.setattr(database, "get_database_url", lambda: "postgresql://db/local")
    monkeypatch.setattr(
        database.psycopg,
        "connect",
        lambda url: calls.append(url) or sentinel,
    )

    assert database.get_connection() is sentinel
    assert calls == ["postgresql://db/local"]


@pytest.mark.parametrize("raises", [False, True])
def test_session_uses_psycopg_transaction_context(monkeypatch, raises):
    class FakeConnection:
        exited_with = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.exited_with = exc_type

    connection = FakeConnection()
    monkeypatch.setattr(database, "get_connection", lambda: connection)

    if raises:
        with pytest.raises(ValueError, match="operation failed"):
            with database.get_session() as session:
                assert session is connection
                raise ValueError("operation failed")
        assert connection.exited_with is ValueError
    else:
        with database.get_session() as session:
            assert session is connection
        assert connection.exited_with is None
