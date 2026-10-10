from contextlib import contextmanager

from apps.api import deduplication
from apps.api.ingest import ParsedCsvUpload, hash_normalized_row, hash_normalized_sheet


def ad(row_number="1", **overrides):
    values = {
        "row_number": row_number,
        "platform": "Meta",
        "ad_text": "hello",
        "destination_url": "https://example.test",
    }
    values.update(overrides)
    return values


def test_row_hash_ignores_source_row_number_and_is_key_order_independent():
    first = ad("1")
    second = {"destination_url": first["destination_url"], "ad_text": "hello", "row_number": "99", "platform": "Meta"}

    assert hash_normalized_row(first) == hash_normalized_row(second)
    assert hash_normalized_row(first) != hash_normalized_row(ad("1", ad_text="changed"))
    parsed = ParsedCsvUpload(columns=(), rows=[first])
    assert parsed.row_hashes == (hash_normalized_row(first),)


def test_sheet_hash_uses_valid_normalized_rows_in_source_order():
    first = [ad("1"), ad("2", ad_text="second")]

    assert hash_normalized_sheet(first) == hash_normalized_sheet(
        [ad("100"), ad("200", ad_text="second")]
    )
    assert hash_normalized_sheet(first) != hash_normalized_sheet(list(reversed(first)))
    assert ParsedCsvUpload(columns=(), rows=first).sheet_hash == hash_normalized_sheet(first)


def test_filter_unseen_rows_removes_persisted_and_repeated_input_rows(monkeypatch):
    duplicate = ad("1")
    existing = hash_normalized_row(ad("20", ad_text="stored"))

    class FakeResult:
        def fetchall(self):
            return [(existing,)]

    class FakeConnection:
        def execute(self, statement, params):
            assert "client_id = %s" in statement
            assert params[0] == "client-1"
            return FakeResult()

    @contextmanager
    def fake_session():
        yield FakeConnection()

    monkeypatch.setattr(deduplication, "get_session", fake_session)
    result = deduplication.filter_unseen_rows(
        "client-1", [duplicate, ad("2"), ad("3"), ad("4", ad_text="stored")]
    )

    assert [row["row_number"] for row in result] == ["1"]


def test_claim_sheet_is_atomic_and_returns_whether_inserted(monkeypatch):
    class FakeResult:
        def __init__(self, inserted):
            self.inserted = inserted

        def fetchone(self):
            return ("sheet-hash",) if self.inserted else None

    inserted = True

    class FakeConnection:
        def execute(self, statement, params):
            assert "ON CONFLICT (client_id, sheet_hash) DO NOTHING" in statement
            assert params == ("client-1", "sheet-hash")
            return FakeResult(inserted)

    @contextmanager
    def fake_session():
        yield FakeConnection()

    monkeypatch.setattr(deduplication, "get_session", fake_session)
    assert deduplication.claim_sheet("client-1", "sheet-hash") is True

    inserted = False
    assert deduplication.claim_sheet("client-1", "sheet-hash") is False


def test_schema_enforces_client_scoped_hash_uniqueness(monkeypatch):
    calls = []

    class FakeConnection:
        def execute(self, statement):
            calls.append(statement)

    @contextmanager
    def fake_session():
        yield FakeConnection()

    monkeypatch.setattr(deduplication, "create_client_tables", lambda: None)
    monkeypatch.setattr(deduplication, "get_session", fake_session)

    deduplication.create_deduplication_constraints()

    assert len(calls) == 2
    assert "PRIMARY KEY (client_id, sheet_hash)" in calls[0]
    assert "UNIQUE INDEX" in calls[1]
    assert "ON ads (client_id, row_hash)" in calls[1]
