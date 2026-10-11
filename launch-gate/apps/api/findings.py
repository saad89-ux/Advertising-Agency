"""PostgreSQL persistence for mechanical ad-check findings."""

from collections.abc import Sequence

from apps.api.database import get_session
from packages.contracts import CheckType, FindingContract
from psycopg import Connection


CREATE_FINDINGS_TABLE = """
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY CHECK (length(trim(finding_id)) > 0),
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    check_type TEXT NOT NULL CHECK (check_type IN ('mechanical', 'page', 'wording')),
    message TEXT NOT NULL CHECK (length(trim(message)) > 0),
    field TEXT,
    proposed_fix TEXT,
    rule_id TEXT,
    file_ref TEXT
)
"""

INSERT_FINDING = """
INSERT INTO findings (
    finding_id, ad_id, row_number, check_type, message, field,
    proposed_fix, rule_id, file_ref
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def create_findings_table() -> None:
    """Create the findings table after ensuring its ads dependency exists."""
    from apps.api.ads import create_ads_table

    create_ads_table()
    with get_session() as connection:
        connection.execute(CREATE_FINDINGS_TABLE)


def persist_mechanical_findings(findings: Sequence[FindingContract]) -> None:
    """Persist mechanical findings in a database-managed transaction."""
    if not findings:
        return
    with get_session() as connection:
        persist_mechanical_findings_on_connection(connection, findings)


def persist_mechanical_findings_on_connection(
    connection: Connection, findings: Sequence[FindingContract]
) -> None:
    """Persist mechanical findings in the caller's transaction."""
    if any(finding.check_type != CheckType.MECHANICAL for finding in findings):
        raise ValueError("only mechanical findings can be persisted by this function")

    for finding in findings:
        connection.execute(
            INSERT_FINDING,
            (
                finding.finding_id,
                finding.ad_id,
                finding.row_number,
                finding.check_type.value,
                finding.message,
                finding.field,
                finding.proposed_fix,
                finding.rule_id,
                finding.file_ref,
            ),
        )
