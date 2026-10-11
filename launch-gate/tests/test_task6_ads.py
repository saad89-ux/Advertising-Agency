from contextlib import contextmanager

from apps.api import ads


def test_create_ads_table_ensures_client_table_and_issues_ddl(monkeypatch):
    calls = {"clients": 0, "statements": []}

    class FakeConnection:
        def execute(self, statement):
            calls["statements"].append(statement)

    @contextmanager
    def fake_session():
        yield FakeConnection()

    def fake_create_client_tables():
        calls["clients"] += 1

    monkeypatch.setattr(ads, "get_session", fake_session)
    monkeypatch.setattr(ads, "create_client_tables", fake_create_client_tables)
    monkeypatch.setattr(ads, "create_batches_table", lambda: calls.update(batches=1))

    ads.create_ads_table()

    assert calls["clients"] == 1
    assert calls["batches"] == 1
    assert len(calls["statements"]) == 2
    statement = calls["statements"][0]
    assert "CREATE TABLE IF NOT EXISTS ads" in statement
    assert "client_id TEXT NOT NULL REFERENCES clients (client_id)" in statement
    assert "row_hash TEXT NOT NULL" in statement
    assert "sheet_hash TEXT NOT NULL" in statement
    assert "row_number > 0" in statement
    assert "ad_text IS NOT NULL" in statement
    assert "headline IS NOT NULL" in statement
    assert "batch_id TEXT NOT NULL REFERENCES batches (batch_id)" in statement
    assert "ads_batch_id_fkey" in calls["statements"][1]
