from contextlib import contextmanager

import pytest

from apps.api import findings
from packages.contracts import CheckType, FindingContract


class FakeConnection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params))


def mechanical_finding(**overrides):
    values = {
        "finding_id": "finding-1",
        "ad_id": "ad-1",
        "row_number": 7,
        "check_type": CheckType.MECHANICAL,
        "message": "UTM source is missing",
        "field": "destination_url",
        "proposed_fix": "Add utm_source",
        "rule_id": "utm_presence",
        "file_ref": None,
    }
    values.update(overrides)
    return FindingContract(**values)


def test_findings_table_references_ads_and_contract_fields():
    assert "REFERENCES ads (ad_id) ON DELETE CASCADE" in findings.CREATE_FINDINGS_TABLE
    for column in (
        "finding_id", "ad_id", "row_number", "check_type", "message",
        "field", "proposed_fix", "rule_id", "file_ref",
    ):
        assert column in findings.CREATE_FINDINGS_TABLE


def test_persists_all_mechanical_finding_fields_on_existing_connection():
    connection = FakeConnection()
    finding = mechanical_finding()

    findings.persist_mechanical_findings_on_connection(connection, [finding])

    assert connection.calls == [
        (
            findings.INSERT_FINDING,
            (
                "finding-1", "ad-1", 7, "mechanical", "UTM source is missing",
                "destination_url", "Add utm_source", "utm_presence", None,
            ),
        )
    ]


def test_rejects_non_mechanical_findings_before_writing():
    connection = FakeConnection()
    finding = mechanical_finding(check_type=CheckType.PAGE)

    with pytest.raises(ValueError, match="only mechanical findings"):
        findings.persist_mechanical_findings_on_connection(connection, [finding])

    assert connection.calls == []


def test_empty_findings_does_not_open_database_session(monkeypatch):
    def fail_if_opened():
        raise AssertionError("empty persistence should not open a session")

    monkeypatch.setattr(findings, "get_session", fail_if_opened)
    findings.persist_mechanical_findings([])


def test_persist_wrapper_uses_one_session(monkeypatch):
    connection = FakeConnection()
    entered = []

    @contextmanager
    def fake_session():
        entered.append(True)
        yield connection

    monkeypatch.setattr(findings, "get_session", fake_session)
    findings.persist_mechanical_findings([mechanical_finding()])

    assert entered == [True]
    assert len(connection.calls) == 1
