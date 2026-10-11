from contextlib import contextmanager

import pytest

from apps.api import deduplication, rejected_rows
from apps.api.ingest import RejectedRow


def test_failure_after_sheet_claim_rolls_back_claim_ads_and_rejected_rows(monkeypatch):
    committed = {"claims": set(), "ads": [], "rejected_rows": []}

    class Result:
        def __init__(self, value=None):
            self.value = value

        def fetchone(self):
            return self.value

    class TransactionConnection:
        def __init__(self):
            self.pending = {"claims": set(), "ads": [], "rejected_rows": []}

        def execute(self, statement, params=None):
            if "INSERT INTO sheet_claims" in statement:
                claim = (params[0], params[1])
                if claim in committed["claims"] or claim in self.pending["claims"]:
                    return Result()
                self.pending["claims"].add(claim)
                return Result((params[2],))
            if "INSERT INTO ads" in statement:
                self.pending["ads"].append(params)
            if "INSERT INTO batch_rejected_rows" in statement:
                self.pending["rejected_rows"].append(params)
            return Result()

    @contextmanager
    def fake_session():
        connection = TransactionConnection()
        try:
            yield connection
        except Exception:
            raise
        else:
            for key in committed:
                committed[key].update(connection.pending[key]) if isinstance(
                    committed[key], set
                ) else committed[key].extend(connection.pending[key])

    monkeypatch.setattr(deduplication, "get_session", fake_session)
    monkeypatch.setattr(rejected_rows, "get_session", fake_session)

    with pytest.raises(RuntimeError, match="injected failure after writes"):
        with fake_session() as connection:
            assert deduplication.claim_sheet(connection, "client-1", "sheet-1", "batch-1") is None
            connection.execute("INSERT INTO ads VALUES (...) ", ("ad-1",))
            rejected_rows.persist_rejected_rows_on_connection(
                connection,
                "batch-1",
                [
                    RejectedRow(
                        data_row_index=2,
                        row_number="bad",
                        reason_code="invalid_row_number",
                        reason="row number is invalid",
                        raw_cells=("bad", "Meta", "Ad", "/page"),
                    )
                ],
            )
            raise RuntimeError("injected failure after writes")

    assert committed == {"claims": set(), "ads": [], "rejected_rows": []}
