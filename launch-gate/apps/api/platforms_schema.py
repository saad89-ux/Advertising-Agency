"""Database tables and initial seeding for social platform handles, live dispatches, and sales attribution."""

from __future__ import annotations

import logging
from typing import Any

from apps.api.database import get_session

logger = logging.getLogger(__name__)

CREATE_PLATFORMS_TABLES = """
-- 1. Connected Social Platform Accounts / Handles
CREATE TABLE IF NOT EXISTS platform_accounts (
    account_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('instagram', 'facebook', 'tiktok', 'snapchat', 'x')),
    handle TEXT NOT NULL,
    account_name TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'api_token',
    credentials_preview TEXT NOT NULL,
    encrypted_credentials TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'connected' CHECK (status IN ('connected', 'active', 'rate_limited', 'disconnected')),
    last_sync_at TIMESTAMPTZ DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_platform_accounts_client ON platform_accounts (client_id);
CREATE INDEX IF NOT EXISTS idx_platform_accounts_platform ON platform_accounts (platform);

-- 2. Live Ad Dispatches & Platform Review Status
CREATE TABLE IF NOT EXISTS ad_live_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    ad_id TEXT NOT NULL REFERENCES ads (ad_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    account_id TEXT REFERENCES platform_accounts (account_id) ON DELETE SET NULL,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active_live' CHECK (status IN ('scheduled', 'authenticating', 'dispatching', 'pending_review', 'active_live', 'rejected', 'paused', 'blocked_connector_not_configured')),
    platform_ad_id TEXT NOT NULL,
    platform_review_status TEXT NOT NULL DEFAULT 'Approved by Platform Review',
    platform_review_notes TEXT,
    live_preview_url TEXT,
    dispatched_at TIMESTAMPTZ DEFAULT NOW(),
    last_tracked_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ad_live_dispatches_ad ON ad_live_dispatches (ad_id);
CREATE INDEX IF NOT EXISTS idx_ad_live_dispatches_client ON ad_live_dispatches (client_id);

-- 3. Inbound Sales & Continuous Attribution Tracking
CREATE TABLE IF NOT EXISTS ad_performance_tracking (
    ad_id TEXT PRIMARY KEY REFERENCES ads (ad_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES batches (batch_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    inbound_sales_usd NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    inbound_orders INT NOT NULL DEFAULT 0,
    impressions INT NOT NULL DEFAULT 0,
    clicks INT NOT NULL DEFAULT 0,
    ctr_percent NUMERIC(5, 2) NOT NULL DEFAULT 0.00,
    roas NUMERIC(6, 2) NOT NULL DEFAULT 0.00,
    spend_usd NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
    has_rule_violation BOOLEAN NOT NULL DEFAULT FALSE,
    violation_details TEXT,
    last_event TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ad_perf_client ON ad_performance_tracking (client_id);
CREATE INDEX IF NOT EXISTS idx_ad_perf_platform ON ad_performance_tracking (platform);
"""


def init_platform_tables(connection: Any = None) -> None:
    """Create platform accounts, dispatches, and performance tracking tables."""
    def _run(conn: Any):
        with conn.cursor() as cur:
            cur.execute(CREATE_PLATFORMS_TABLES)
        if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
            conn.commit()

    if connection is not None:
        _run(connection)
    else:
        with get_session() as session:
            _run(session)


def seed_platform_demo_data(connection: Any = None) -> None:
    """Seed initial connected handles, dispatches, and inbound sales tracking."""
    init_platform_tables(connection)

    demo_accounts = [
        # BrightLeaf
        ("acc_bl_ig", "brightleaf", "instagram", "@brightleaf_wellness", "BrightLeaf Wellness Official", "oauth", "Bearer oauth_bl_ig_9921", "encrypted_tok_bl_ig", "connected"),
        ("acc_bl_fb", "brightleaf", "facebook", "act_8492049102", "BrightLeaf Meta Ads Manager", "api_token", "EAAX...9482", "encrypted_tok_bl_fb", "active"),
        ("acc_bl_tt", "brightleaf", "tiktok", "@brightleaf_tok", "BrightLeaf TikTok Spark Ads", "api_token", "tt_act_8849...2941", "encrypted_tok_bl_tt", "active"),
        # CalmLeaf
        ("acc_cl_ig", "calmleaf", "instagram", "@calmleaf_official", "CalmLeaf Organic Extracts", "oauth", "Bearer oauth_cl_ig_7741", "encrypted_tok_cl_ig", "connected"),
        ("acc_cl_fb", "calmleaf", "facebook", "act_5591029381", "CalmLeaf Scale Meta Account", "api_token", "EAAX...3382", "encrypted_tok_cl_fb", "active"),
        ("acc_cl_x",  "calmleaf", "x", "@calmleaf_x", "CalmLeaf Brand X Ads", "api_token", "x_oauth2_cl_7182", "encrypted_tok_cl_x", "connected"),
        ("acc_cl_snap","calmleaf", "snapchat", "@calmleaf_snap", "CalmLeaf Snap Commercials", "api_token", "sc_token_cl_9921", "encrypted_tok_cl_snap", "active"),
        # Oak & Home
        ("acc_oh_ig", "oakandhome", "instagram", "@oakandhome_living", "Oak & Home Design Studio", "oauth", "Bearer oauth_oh_ig_1104", "encrypted_tok_oh_ig", "connected"),
        ("acc_oh_fb", "oakandhome", "facebook", "act_9928172641", "Oak & Home Meta Living Catalog", "api_token", "EAAX...8814", "encrypted_tok_oh_fb", "active"),
        ("acc_oh_tt", "oakandhome", "tiktok", "@oakandhome_home", "Oak & Home TikTok Shop Ads", "api_token", "tt_act_9918...4419", "encrypted_tok_oh_tt", "active"),
    ]

    def _seed(conn: Any):
        with conn.cursor() as cur:
            for acc in demo_accounts:
                cur.execute(
                    """
                    INSERT INTO platform_accounts (
                        account_id, client_id, platform, handle, account_name,
                        auth_type, credentials_preview, encrypted_credentials, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (account_id) DO NOTHING;
                    """,
                    acc,
                )

            # Query existing ads to bind realistic live dispatches and performance data
            cur.execute("SELECT ad_id, batch_id, client_id, platform, headline, ad_text FROM ads LIMIT 30;")
            ad_rows = cur.fetchall()

            sample_metrics = [
                {"sales": 4820.50, "orders": 42, "imp": 68400, "clicks": 2840, "ctr": 4.15, "roas": 5.8, "spend": 831.00, "violation": False, "note": "Inbound order #10842 attributed via Instagram Story ad"},
                {"sales": 3210.00, "orders": 29, "imp": 44100, "clicks": 1690, "ctr": 3.83, "roas": 4.2, "spend": 764.00, "violation": False, "note": "Inbound order #10839 attributed via TikTok Feed ad"},
                {"sales": 1890.00, "orders": 18, "imp": 29800, "clicks": 980,  "ctr": 3.29, "roas": 3.6, "spend": 525.00, "violation": False, "note": "Inbound order #10835 attributed via Meta Carousel"},
                {"sales": 5420.00, "orders": 49, "imp": 82000, "clicks": 3410, "ctr": 4.16, "roas": 6.1, "spend": 888.00, "violation": False, "note": "Inbound order #10831 attributed via Instagram Reels"},
                {"sales": 0.00,    "orders": 0,  "imp": 1200,  "clicks": 42,   "ctr": 3.50, "roas": 0.0, "spend": 80.00,  "violation": True,  "note": "Platform Policy Flag: Exaggerated result claim in ad caption (disclaimers missing)"},
                {"sales": 2740.00, "orders": 24, "imp": 38900, "clicks": 1420, "ctr": 3.65, "roas": 4.0, "spend": 685.00, "violation": False, "note": "Inbound order #10826 attributed via X Sponsored Post"},
            ]

            for i, r in enumerate(ad_rows):
                ad_id, batch_id, client_id, platform, headline, ad_text = r
                m = sample_metrics[i % len(sample_metrics)]

                # Determine platform & handle
                norm_plat = (platform or "instagram").lower()
                if "meta" in norm_plat or "facebook" in norm_plat:
                    plat = "facebook"
                    handle = f"@{client_id}_ads"
                elif "tiktok" in norm_plat:
                    plat = "tiktok"
                    handle = f"@{client_id}_tok"
                elif "x" in norm_plat or "twitter" in norm_plat:
                    plat = "x"
                    handle = f"@{client_id}_x"
                elif "snap" in norm_plat:
                    plat = "snapchat"
                    handle = f"@{client_id}_snap"
                else:
                    plat = "instagram"
                    handle = f"@{client_id}_wellness" if client_id == "brightleaf" else f"@{client_id}_official"

                rev_status = "Policy Warning Flagged" if m["violation"] else "Approved & Serving Live"
                rev_notes = m["note"]

                cur.execute(
                    """
                    INSERT INTO ad_live_dispatches (
                        dispatch_id, ad_id, batch_id, client_id, platform, handle,
                        status, platform_ad_id, platform_review_status, platform_review_notes, live_preview_url
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (dispatch_id) DO NOTHING;
                    """,
                    (
                        f"disp_{ad_id}",
                        ad_id,
                        batch_id,
                        client_id,
                        plat,
                        handle,
                        "paused" if m["violation"] else "active_live",
                        f"{plat[:2].upper()}-{ad_id[:8]}",
                        rev_status,
                        rev_notes,
                        f"https://www.{plat}.com/{handle.lstrip('@')}/p/{ad_id[:10]}",
                    ),
                )

                cur.execute(
                    """
                    INSERT INTO ad_performance_tracking (
                        ad_id, batch_id, client_id, platform, handle,
                        inbound_sales_usd, inbound_orders, impressions, clicks,
                        ctr_percent, roas, spend_usd, has_rule_violation, violation_details, last_event
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ad_id) DO UPDATE SET
                        inbound_sales_usd = EXCLUDED.inbound_sales_usd,
                        inbound_orders = EXCLUDED.inbound_orders,
                        impressions = EXCLUDED.impressions,
                        clicks = EXCLUDED.clicks,
                        ctr_percent = EXCLUDED.ctr_percent,
                        roas = EXCLUDED.roas,
                        spend_usd = EXCLUDED.spend_usd,
                        has_rule_violation = EXCLUDED.has_rule_violation,
                        violation_details = EXCLUDED.violation_details,
                        last_event = EXCLUDED.last_event;
                    """,
                    (
                        ad_id,
                        batch_id,
                        client_id,
                        plat,
                        handle,
                        m["sales"],
                        m["orders"],
                        m["imp"],
                        m["clicks"],
                        m["ctr"],
                        m["roas"],
                        m["spend"],
                        m["violation"],
                        m["note"] if m["violation"] else None,
                        m["note"],
                    ),
                )

        if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
            conn.commit()

    if connection is not None:
        _seed(connection)
    else:
        with get_session() as session:
            _seed(session)
