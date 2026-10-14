from contextlib import contextmanager
import json

import pytest

from apps.api import rejected_rows
from apps.api.ingest import RejectedRow


class FakeConnection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params))


def test_persists_rejections_with_batch_id_and_raw_cells(monkeypatch):
    connection = FakeConnection()

    @contextmanager
    def fake_session():
        yield connection

    monkeypatch.setattr(rejected_rows, "get_session", fake_session)
    rejected_rows.persist_rejected_rows(
        "batch-1",
        [
            RejectedRow(
                data_row_index=3,
                row_number="bad",
                reason_code="invalid_row_number",
                reason="row number must be positive",
                raw_cells=("bad", "Meta", "text", "/page"),
            )
        ],
    )

    statement, params = connection.calls[0]
    assert statement == rejected_rows.INSERT_REJECTED_ROW
    assert params[0:2] == ("batch-1", 3)
    assert json.loads(params[2]) == "bad"
    assert params[3:5] == ("invalid_row_number", "row number must be positive")
    assert json.loads(params[5]) == ["bad", "Meta", "text", "/page"]


def test_rejects_blank_batch_id_without_opening_database(monkeypatch):
    def fail_if_called():
        raise AssertionError("database should not be opened")

    monkeypatch.setattr(rejected_rows, "get_session", fail_if_called)
    with pytest.raises(ValueError, match="batch_id"):
        rejected_rows.persist_rejected_rows(" ", [])
