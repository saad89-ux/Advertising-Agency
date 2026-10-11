from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from apps.worker import client_rule_loader as loader


REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_ROOT = REPO_ROOT / "data" / "clients"


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, dimensions=()):
        self.calls = []
        self.dimensions = list(dimensions)

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        if statement == loader.SELECT_EXISTING_VECTOR_DIMENSIONS:
            return FakeResult([(dimension,) for dimension in self.dimensions])
        return FakeResult()


def fake_session(connection):
    @contextmanager
    def session():
        yield connection

    return session


def test_loads_every_indexed_entry_with_exact_citation_id():
    entries = loader.load_rule_entries(rules_root=RULES_ROOT)
    index = json.loads((RULES_ROOT / "rule_index.json").read_text(encoding="utf-8"))
    expected = {
        (client_id, entry_id)
        for client_id, docs in index.items()
        if client_id != "_note"
        for entry_id in [*docs["brand_book"], *docs["register"]]
    }

    assert {(entry.client_id, entry.entry_number) for entry in entries} == expected
    assert all(entry.entry_number in entry.content for entry in entries)
    assert all(entry.source_file in {"brand_book.md", "claims_register.md"} for entry in entries)


def test_loads_one_client_without_including_other_clients():
    entries = loader.load_rule_entries(rules_root=RULES_ROOT, client_id="brightleaf")

    assert entries
    assert {entry.client_id for entry in entries} == {"brightleaf"}
    assert all(entry.entry_number.startswith(("BL-BB-", "BL-C-")) for entry in entries)


def test_rejects_unknown_client():
    with pytest.raises(ValueError, match="unknown client"):
        loader.load_rule_entries(rules_root=RULES_ROOT, client_id="unknown")


def test_rejects_indexed_entry_missing_from_source(tmp_path):
    root = tmp_path / "rules"
    client_dir = root / "client"
    client_dir.mkdir(parents=True)
    (root / "rule_index.json").write_text(
        json.dumps({"client": {"brand_book": ["AB-BB-1.1"], "register": ["AB-C-01"]}}),
        encoding="utf-8",
    )
    (client_dir / "brand_book.md").write_text("# Book\n\n## AB-BB-1.1 Voice\nText.\n", encoding="utf-8")
    (client_dir / "claims_register.md").write_text("# Register\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing indexed entries: AB-C-01"):
        loader.load_rule_entries(rules_root=root)


@pytest.mark.parametrize(
    "embedding,message",
    [
        (lambda text: [], "empty vector"),
        (lambda text: [1.0, float("nan")], "finite"),
        (lambda text: [1.0, True], "numbers"),
    ],
)
def test_invalid_embeddings_fail_before_database_session(monkeypatch, embedding, message):
    monkeypatch.setattr(loader, "get_session", lambda: pytest.fail("database opened before validation"))

    with pytest.raises(ValueError, match=message):
        loader.load_client_rules(
            embedding,
            client_id="brightleaf",
            rules_root=RULES_ROOT,
        )


def test_inconsistent_embedding_dimensions_fail_before_database_session(monkeypatch):
    calls = 0

    def embed(text):
        nonlocal calls
        calls += 1
        return [1.0] if calls == 1 else [1.0, 2.0]

    monkeypatch.setattr(loader, "get_session", lambda: pytest.fail("database opened before validation"))
    with pytest.raises(ValueError, match="inconsistent dimensions"):
        loader.load_client_rules(embed, client_id="brightleaf", rules_root=RULES_ROOT)


def test_load_is_client_scoped_and_uses_pgvector_upserts(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(loader, "get_session", fake_session(connection))
    monkeypatch.setattr(loader, "register_vector", lambda conn: None)

    inserted = loader.load_client_rules(
        lambda text: [0.25, 0.75],
        client_id="brightleaf",
        rules_root=RULES_ROOT,
    )

    assert inserted == 11
    assert connection.calls[0][0] == "CREATE EXTENSION IF NOT EXISTS vector"
    assert connection.calls[1][0] == loader.CREATE_CLIENT_RULES_TABLE
    deletes = [params for statement, params in connection.calls if statement == loader.DELETE_CLIENT_RULES]
    assert deletes == [("brightleaf",)]
    inserts = [params for statement, params in connection.calls if statement == loader.INSERT_CLIENT_RULE]
    assert len(inserts) == inserted
    assert {params[0] for params in inserts} == {"brightleaf"}
    assert {params[1] for params in inserts} == {
        entry.entry_number
        for entry in loader.load_rule_entries(rules_root=RULES_ROOT, client_id="brightleaf")
    }
    assert all(params[4].to_list() == [0.25, 0.75] for params in inserts)
    assert "ON CONFLICT (client_id, entry_number) DO UPDATE" in loader.INSERT_CLIENT_RULE


def test_existing_database_vector_dimension_must_match(monkeypatch):
    connection = FakeConnection(dimensions=[3])
    monkeypatch.setattr(loader, "get_session", fake_session(connection))
    monkeypatch.setattr(loader, "register_vector", lambda conn: None)

    with pytest.raises(ValueError, match="differs from the vectors already stored"):
        loader.load_client_rules(
            lambda text: [0.25, 0.75],
            client_id="brightleaf",
            rules_root=RULES_ROOT,
        )

    assert not any(statement == loader.DELETE_CLIENT_RULES for statement, _ in connection.calls)


def test_table_keeps_client_id_and_entry_number_as_scoped_key():
    assert "client_id TEXT NOT NULL REFERENCES clients (client_id)" in loader.CREATE_CLIENT_RULES_TABLE
    assert "entry_number TEXT NOT NULL" in loader.CREATE_CLIENT_RULES_TABLE
    assert "embedding vector NOT NULL" in loader.CREATE_CLIENT_RULES_TABLE
    assert "PRIMARY KEY (client_id, entry_number)" in loader.CREATE_CLIENT_RULES_TABLE
