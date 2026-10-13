from contextlib import contextmanager

from apps.api.auth import CurrentUser
from apps.api import campaign_flow


class CampaignCursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        self.query = query

    def fetchone(self):
        return (False,)

    def fetchall(self):
        return self.rows if "SELECT\n                    a.ad_id" in self.query else []


class CampaignConnection:
    def __init__(self, rows):
        self.cursor_value = CampaignCursor(rows)

    def cursor(self):
        return self.cursor_value


def campaign_row(ad_id, tracked, inbound_orders):
    return (
        ad_id, "brightleaf", "facebook", "Headline", "https://example.test",
        "@brightleaf", "pending_review", "Not submitted", None, None, None,
        None, False, tracked, False, inbound_orders,
    )


def test_all_campaigns_returns_recorded_order_counts_and_preserves_missing_values(monkeypatch):
    conn = CampaignConnection([
        campaign_row("ad-zero", True, 0),
        campaign_row("ad-missing", True, None),
        campaign_row("ad-untracked", False, None),
    ])

    @contextmanager
    def fake_session():
        yield conn

    monkeypatch.setattr(campaign_flow, "get_session", fake_session)
    monkeypatch.setattr(campaign_flow, "init_flow_tables", lambda: None)
    user = CurrentUser("buyer", "buyer@example.test", "Buyer", "buyer", ("brightleaf",))

    campaigns = campaign_flow.get_all_campaigns(user=user)

    assert [campaign["inbound_orders"] for campaign in campaigns] == [0, None, None]
    assert "p.inbound_orders as inbound_orders" in conn.cursor_value.query
