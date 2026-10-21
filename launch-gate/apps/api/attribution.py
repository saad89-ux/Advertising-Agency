"""Continuous inbound sales and conversion attribution tracking API."""

from __future__ import annotations

import logging
import random
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apps.api.access import require_client_access
from apps.api.auth import CurrentUser, get_current_user
from apps.api.database import get_session
from apps.api.platforms_schema import init_platform_tables

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attribution", tags=["attribution"])


class SimulateSaleRequest(BaseModel):
    ad_id: str
    order_amount: float = Field(default=89.50, description="Order value in USD")
    customer_city: str = Field(default="Austin, TX", description="Customer location")


@router.get("/summary")
def get_attribution_summary(
    batch_id: str | None = None,
    client_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Get aggregated inbound sales, orders, ROAS, and platform performance breakdown."""
    init_platform_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            if client_id:
                require_client_access(user, client_id)
            allowed = [client_id] if client_id else list(user.assigned_client_ids)
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(inbound_sales_usd), 0.0),
                    COALESCE(SUM(inbound_orders), 0),
                    COALESCE(SUM(spend_usd), 0.0),
                    COALESCE(SUM(impressions), 0),
                    COALESCE(SUM(clicks), 0),
                    COALESCE(COUNT(*), 0),
                    COALESCE(COUNT(*) FILTER (WHERE has_rule_violation = TRUE), 0)
                FROM ad_performance_tracking
                WHERE client_id = ANY(%s) AND (%s::text IS NULL OR batch_id = %s);
                """,
                (allowed, batch_id, batch_id),
            )
            row = cur.fetchone()
            sales = float(row[0])
            orders = int(row[1])
            spend = float(row[2])
            imp = int(row[3])
            clicks = int(row[4])
            total_tracked = int(row[5])
            violations = int(row[6])

            overall_roas = round(sales / spend, 2) if spend > 0 else 0.0
            overall_ctr = round((clicks / imp) * 100, 2) if imp > 0 else 0.0

            # Breakdown by platform
            cur.execute(
                """
                SELECT platform,
                       COALESCE(SUM(inbound_sales_usd), 0.0),
                       COALESCE(SUM(inbound_orders), 0),
                       COALESCE(SUM(spend_usd), 0.0),
                       COUNT(*)
                FROM ad_performance_tracking
                WHERE client_id = ANY(%s) AND (%s::text IS NULL OR batch_id = %s)
                GROUP BY platform
                ORDER BY SUM(inbound_sales_usd) DESC;
                """,
                (allowed, batch_id, batch_id),
            )
            plat_rows = cur.fetchall()
            platforms_breakdown = [
                {
                    "platform": r[0],
                    "sales_usd": float(r[1]),
                    "orders": int(r[2]),
                    "spend_usd": float(r[3]),
                    "active_ads": int(r[4]),
                    "roas": round(float(r[1]) / float(r[3]), 2) if float(r[3]) > 0 else 0.0,
                }
                for r in plat_rows
            ]

            return {
                "total_inbound_sales_usd": sales,
                "total_inbound_orders": orders,
                "total_spend_usd": spend,
                "overall_roas": overall_roas,
                "overall_ctr_percent": overall_ctr,
                "total_impressions": imp,
                "total_clicks": clicks,
                "active_ads_count": total_tracked,
                "flagged_violations_count": violations,
                "clean_ads_count": total_tracked - violations,
                "platforms_breakdown": platforms_breakdown,
            }


@router.get("/ads")
def list_ads_attribution(
    client_id: str | None = None,
    batch_id: str | None = None,
    platform: str | None = None,
    sort_by: str = "sales",
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List ads with live inbound sales, orders, ROAS, and rule violation alerts."""
    init_platform_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            allowed = list(user.assigned_client_ids)
            query = """
                SELECT p.ad_id, p.batch_id, p.client_id, p.platform, p.handle,
                       p.inbound_sales_usd, p.inbound_orders, p.impressions, p.clicks,
                       p.ctr_percent, p.roas, p.spend_usd, p.has_rule_violation,
                       p.violation_details, p.last_event, p.updated_at,
                       a.headline, a.ad_text, a.destination_url,
                       COALESCE(d.platform_review_status, 'Active & Serving')
                FROM ad_performance_tracking p
                JOIN ads a ON p.ad_id = a.ad_id
                LEFT JOIN ad_live_dispatches d ON p.ad_id = d.ad_id
                WHERE p.client_id = ANY(%s)
            """
            params: list[Any] = [allowed]

            if client_id:
                require_client_access(user, client_id)
                query += " AND p.client_id = %s"
                params.append(client_id)

            if batch_id:
                query += " AND p.batch_id = %s"
                params.append(batch_id)

            if platform:
                query += " AND LOWER(p.platform) = LOWER(%s)"
                params.append(platform)

            if sort_by == "sales":
                query += " ORDER BY p.inbound_sales_usd DESC"
            elif sort_by == "roas":
                query += " ORDER BY p.roas DESC"
            elif sort_by == "orders":
                query += " ORDER BY p.inbound_orders DESC"
            elif sort_by == "violations":
                query += " ORDER BY p.has_rule_violation DESC, p.inbound_sales_usd DESC"
            else:
                query += " ORDER BY p.updated_at DESC"

            cur.execute(query, params)
            rows = cur.fetchall()

            return [
                {
                    "ad_id": r[0],
                    "batch_id": r[1],
                    "client_id": r[2],
                    "platform": r[3],
                    "handle": r[4],
                    "inbound_sales_usd": float(r[5]),
                    "inbound_orders": int(r[6]),
                    "impressions": int(r[7]),
                    "clicks": int(r[8]),
                    "ctr_percent": float(r[9]),
                    "roas": float(r[10]),
                    "spend_usd": float(r[11]),
                    "has_rule_violation": bool(r[12]),
                    "violation_details": r[13],
                    "last_event": r[14],
                    "updated_at": str(r[15]),
                    "headline": r[16],
                    "ad_text": r[17],
                    "destination_url": r[18],
                    "platform_review_status": r[19],
                }
                for r in rows
            ]


@router.get("/live-events")
def get_live_events_stream(
    batch_id: str | None = None,
    client_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return simulated live inbound conversion events and platform review checks."""
    init_platform_tables()
    with get_session() as conn:
        with conn.cursor() as cur:
            if client_id:
                require_client_access(user, client_id)
            allowed = [client_id] if client_id else list(user.assigned_client_ids)
            cur.execute(
                """
                SELECT p.ad_id, p.client_id, p.platform, p.handle, p.inbound_sales_usd,
                       p.last_event, p.has_rule_violation, a.headline
                FROM ad_performance_tracking p
                JOIN ads a ON p.ad_id = a.ad_id
                WHERE p.client_id = ANY(%s) AND (%s::text IS NULL OR p.batch_id = %s)
                ORDER BY p.updated_at DESC
                LIMIT 15;
                """,
                (allowed, batch_id, batch_id),
            )
            rows = cur.fetchall()

            events = []
            for r in rows:
                events.append({
                    "ad_id": r[0],
                    "client_id": r[1],
                    "platform": r[2],
                    "handle": r[3],
                    "type": "policy_alert" if r[6] else "inbound_sale",
                    "event_text": r[5] or f"Serving impressions on {r[3]}",
                    "headline": r[7],
                })
            return events


@router.post("/simulate-sale")
def simulate_inbound_sale(
    req: SimulateSaleRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Simulate an incoming verified customer purchase attributed to an ad via UTM tracking."""
    with get_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT client_id, platform, handle, inbound_sales_usd, inbound_orders FROM ad_performance_tracking WHERE ad_id = %s;",
                (req.ad_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Ad tracking record not found")
            client_id, platform, handle, curr_sales, curr_orders = row
            require_client_access(user, client_id)

            order_id = random.randint(10900, 19999)
            new_sales = float(curr_sales) + req.order_amount
            new_orders = int(curr_orders) + 1
            event_msg = f"Inbound Order #{order_id} (${req.order_amount:.2f}) from {req.customer_city} attributed to {handle}"

            cur.execute(
                """
                UPDATE ad_performance_tracking
                SET inbound_sales_usd = %s,
                    inbound_orders = %s,
                    last_event = %s,
                    updated_at = NOW()
                WHERE ad_id = %s;
                """,
                (new_sales, new_orders, event_msg, req.ad_id),
            )
        conn.commit()

    return {
        "status": "attributed",
        "ad_id": req.ad_id,
        "new_total_sales": new_sales,
        "new_orders_count": new_orders,
        "event": event_msg,
    }
