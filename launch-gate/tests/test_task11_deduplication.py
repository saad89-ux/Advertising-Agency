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
    second = {
        "destination_url": first["destination_url"],
        "ad_text": "hello",
        "row_number": "99",
        "platform": "Meta",
        "unknown_extra": "ignored",
    }

    assert hash_normalized_row(first) == hash_normalized_row(second)
    assert hash_normalized_row(first) != hash_normalized_row(ad("1", ad_text="changed"))
    parsed = ParsedCsvUpload(columns=(), rows=[first])
    assert parsed.row_hashes == (hash_normalized_row(first),)


def test_row_hash_changes_when_any_canonical_field_changes():
    original = ad("1")
    for field, changed in (
        ("platform", "Search"),
        ("ad_text", "different text"),
        ("headline", "title"),
        ("destination_url", "https://other.test"),
        ("creative_file", "asset.png"),
    ):
        assert hash_normalized_row({**original, field: changed}) != hash_normalized_row(original)


def test_sheet_hash_uses_valid_normalized_rows_in_source_order():
    first = [ad("1"), ad("2", ad_text="second")]

    assert hash_normalized_sheet(first) == hash_normalized_sheet(
        [ad("100", ignored="one"), ad("200", ad_text="second", ignored="two")]
    )
    assert hash_normalized_sheet(first) != hash_normalized_sheet(list(reversed(first)))
    assert ParsedCsvUpload(columns=(), rows=first).sheet_hash == hash_normalized_sheet(first)


def test_claim_sheet_uses_caller_transaction_and_returns_prior_batch_id():
    class FakeResult:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    inserted = True

    class FakeConnection:
        def execute(self, statement, params):
            if "INSERT INTO sheet_claims" in statement:
                assert "ON CONFLICT (client_id, sheet_hash) DO NOTHING" in statement
                assert params == ("client-1", "sheet-hash", "batch-new")
                return FakeResult(("batch-new",) if inserted else None)
            assert "SELECT batch_id FROM sheet_claims" in statement
            return FakeResult(("batch-original",))

    connection = FakeConnection()
    assert deduplication.claim_sheet(connection, "client-1", "sheet-hash", "batch-new") is None

    inserted = False
    assert deduplication.claim_sheet(connection, "client-1", "sheet-hash", "batch-new") == "batch-original"


def test_schema_ensures_ads_table_then_creates_only_client_scoped_sheet_uniqueness(monkeypatch):
    calls = []

    class FakeConnection:
        def execute(self, statement):
            calls.append(statement)

    @contextmanager
    def fake_session():
        yield FakeConnection()

    def fake_create_batches_table():
        calls.append("batches table")

    def fake_create_ads_table():
        calls.append("ads table")

    monkeypatch.setattr(deduplication, "create_batches_table", fake_create_batches_table)
    monkeypatch.setattr(deduplication, "create_ads_table", fake_create_ads_table)
    monkeypatch.setattr(deduplication, "get_session", fake_session)

    deduplication.create_deduplication_constraints()

    assert calls[:2] == ["batches table", "ads table"]
    assert "PRIMARY KEY (client_id, sheet_hash)" in calls[2]
    assert "batch_id TEXT NOT NULL REFERENCES batches (batch_id)" in calls[2]
    assert all("UNIQUE INDEX" not in statement for statement in calls if isinstance(statement, str))
