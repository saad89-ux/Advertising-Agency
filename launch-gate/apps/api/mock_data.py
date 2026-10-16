"""Realistic mock & demo data seeder for Launch Gate.

Provides realistic, production-like data across 3 clients (BrightLeaf, CalmLeaf, Oak & Home)
featuring realistic ads, verdicts (Ready, Needs Review, Rejected, Approved, Sent, Dead Letter),
findings with policy citations, reviewer edits, alerts, and approval evidence.

Non-permanent and overwritable: real uploads seamlessly coexist and take precedence.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from apps.api.database import get_session
from apps.api.auth import CurrentUser, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mock-data", tags=["mock-data"])

DEMO_BATCHES = [
    {
        "batch_id": "demo-batch-brightleaf-01",
        "uploaded_by": "demo_buyer",
        "filename": "BrightLeaf_Botanical_Fall_Flight_2026.csv",
        "client_id": "brightleaf",
        "created_at": datetime.now(timezone.utc) - timedelta(hours=3),
        "sheet_hash": "hash_demo_brightleaf_001",
        "ads": [
            {
                "ad_id": "demo-ad-bl-001",
                "row_number": 1,
                "platform": "meta",
                "platform_raw": "Facebook Feed",
                "headline": "Pure Ashwagandha Root Extract | 100% Organic",
                "ad_text": "Support your natural calm and focus with our USDA-certified organic Ashwagandha root extract. Third-party lab tested with verified purity.",
                "destination_url": "https://shop.brightleaf.example/products/ashwagandha?utm_source=meta&utm_medium=cpc&utm_campaign=fall_calm_2026",
                "creative_file": "creatives/brightleaf_square_1080.png",
                "state": "pending_review",
                "verdict": "ready",
                "findings": [],
            },
            {
                "ad_id": "demo-ad-bl-002",
                "row_number": 2,
                "platform": "google_search",
                "platform_raw": "Google Ads",
                "headline": "Clinically Proven to Cure Anxiety & Insomnia Fast",
                "ad_text": "Take 2 capsules daily to permanently eliminate insomnia and stress. Guaranteed clinical relief.",
                "destination_url": "https://shop.brightleaf.example/products/sleep-formula?utm_source=google&utm_medium=cpc&utm_campaign=sleep_cure",
                "creative_file": "",
                "state": "pending_review",
                "verdict": "needs_human_review",
                "findings": [
                    {
                        "finding_id": "demo-find-bl-002-1",
                        "check_type": "wording",
                        "field": "headline",
                        "message": "Unapproved disease cure claim: 'Cure Anxiety & Insomnia' violates FDA health claim regulations and client dietary guidelines.",
                        "rule_id": "wording_disease_claims",
                        "proposed_fix": "Change to 'Supports Restful Sleep & Everyday Calm'",
                        "file_ref": "BrightLeaf Brand Book §4.2 (Dietary Supplements & Health Claims)",
                    },
                    {
                        "finding_id": "demo-find-bl-002-2",
                        "check_type": "page",
                        "field": "ad_text",
                        "message": "Storefront landing page disclaimer is missing required DSHEA statement.",
                        "rule_id": "page_disclaimer_missing",
                        "proposed_fix": "Ensure destination page includes standard FDA dietary supplement disclaimer",
                        "file_ref": "FTC Health Claim Rule 16 CFR §255",
                    },
                ],
            },
            {
                "ad_id": "demo-ad-bl-003",
                "row_number": 3,
                "platform": "meta",
                "platform_raw": "Instagram Story",
                "headline": "Fall Flash Sale — 40% Off Elderberry",
                "ad_text": "Boost seasonal immunity naturally with organic black elderberry syrup. Flash sale ends Sunday midnight!",
                "destination_url": "https://shop.brightleaf.example/products/elderberry?utm_source=instagram&utm_medium=social_ad&utm_campaign=elderberry_40off",
                "creative_file": "creatives/elderberry_story_1080x1920.png",
                "state": "approved",
                "verdict": "ready",
                "findings": [],
                "approval_evidence": {
                    "evidence_id": "demo-ev-003",
                    "evidence_type": "written_permission",
                    "notes": "Verified promotional price matches storefront discount banner; approved by Media Director Sarah K.",
                },
            },
            {
                "ad_id": "demo-ad-bl-004",
                "row_number": 4,
                "platform": "meta",
                "platform_raw": "Instagram Feed",
                "headline": "Special Offer — Only $24 Today (Save $15) with Free Shipping",
                "ad_text": "Get our top-rated Golden Milk Turmeric blend now with free express delivery. Limited stock available.",
                "destination_url": "https://shop.brightleaf.example/products/turmeric-blend",
                "creative_file": "creatives/turmeric_1200x628.jpg",
                "state": "rejected",
                "verdict": "rejected",
                "findings": [
                    {
                        "finding_id": "demo-find-bl-004-1",
                        "check_type": "mechanical",
                        "field": "destination_url",
                        "message": "Missing required UTM campaign tracking parameters (utm_source, utm_medium, utm_campaign).",
                        "rule_id": "utm_presence_check",
                        "proposed_fix": "Append ?utm_source=meta&utm_medium=cpc&utm_campaign=turmeric_sale",
                        "file_ref": "BrightLeaf Analytics Naming Convention v2.1",
                    },
                    {
                        "finding_id": "demo-find-bl-004-2",
                        "check_type": "page",
                        "field": "headline",
                        "message": "Price discrepancy: Ad claims '$24 Today', but landing page displays price '$39.00'.",
                        "rule_id": "page_price_mismatch",
                        "proposed_fix": "Update ad copy price to '$39.00' or verify discount coupon code",
                        "file_ref": "Storefront Landing Page Price Scan (HTTP 200)",
                    },
                ],
            },
            {
                "ad_id": "demo-ad-bl-005",
                "row_number": 5,
                "platform": "meta",
                "platform_raw": "Facebook Feed",
                "headline": "Organic Maca Root Capsules for Sustained Energy",
                "ad_text": "Peruvian gelatinized maca root powder capsules. Clean plant energy without caffeine crashes or jitters.",
                "destination_url": "https://shop.brightleaf.example/products/maca?utm_source=meta&utm_medium=cpc&utm_campaign=maca_energy",
                "creative_file": "creatives/maca_root_1080x1080.png",
                "state": "sent",
                "verdict": "ready",
                "findings": [],
                "send_record": {
                    "platform_ad_id": "meta_live_ad_774920194821",
                    "status": "accepted",
                },
            },
            {
                "ad_id": "demo-ad-bl-006",
                "row_number": 6,
                "platform": "tiktok",
                "platform_raw": "TikTok Video",
                "headline": "Transform Your Morning Routine with Liquid Chlorophyll Drops",
                "ad_text": "Join over 50,000 satisfied customers enjoying natural detox and glowing skin. Tap shop now to grab yours before we sell out!",
                "destination_url": "https://shop.brightleaf.example/products/chlorophyll?utm_source=tiktok&utm_medium=video_ad&utm_campaign=skin_glow",
                "creative_file": "creatives/chlorophyll_video_9x16.mp4",
                "state": "dead_letter",
                "verdict": "needs_human_review",
                "findings": [
                    {
                        "finding_id": "demo-find-bl-006-1",
                        "check_type": "wording",
                        "field": "ad_text",
                        "message": "Unsubstantiated count claim: 'over 50,000 satisfied customers' requires on-file substantiation document.",
                        "rule_id": "wording_social_proof",
                        "proposed_fix": "Attach customer testimonial evidence or use 'Thousands of satisfied customers'",
                        "file_ref": "BrightLeaf Legal Substantiation Registry §2",
                    },
                ],
                "alert": {
                    "alert_id": "demo-alert-bl-006",
                    "alert_type": "dead_letter_retries_exhausted",
                    "severity": "high",
                    "message": "Platform API connection timed out after 3 consecutive send attempts; moved to dead-letter queue.",
                },
            },
        ],
    },
    {
        "batch_id": "demo-batch-calmleaf-02",
        "uploaded_by": "demo_buyer",
        "filename": "CalmLeaf_CBD_Wellness_Q4_Launch.xlsx",
        "client_id": "calmleaf",
        "created_at": datetime.now(timezone.utc) - timedelta(days=1, hours=4),
        "sheet_hash": "hash_demo_calmleaf_002",
        "ads": [
            {
                "ad_id": "demo-ad-cl-001",
                "row_number": 1,
                "platform": "meta",
                "platform_raw": "Instagram Feed",
                "headline": "Broad Spectrum CBD Gummies — Natural Berry Flavor",
                "ad_text": "Zero THC, 25mg per gummy. Vegan, gluten-free, and crafted with sun-grown organic hemp.",
                "destination_url": "https://calmleaf.example/gummies?utm_source=meta&utm_medium=cpc&utm_campaign=berry_cbd_q4",
                "creative_file": "creatives/calmleaf_gummies_1080.png",
                "state": "pending_review",
                "verdict": "ready",
                "findings": [],
            },
            {
                "ad_id": "demo-ad-cl-002",
                "row_number": 2,
                "platform": "meta",
                "platform_raw": "Instagram Feed",
                "headline": "Eliminate Chronic Pain & Inflammation Within 15 Minutes",
                "ad_text": "Our maximum strength CBD oil cures arthritis pain and joint stiffness guaranteed.",
                "destination_url": "https://calmleaf.example/tinctures/pain-relief?utm_source=meta&utm_medium=cpc&utm_campaign=cure_pain",
                "creative_file": "creatives/tincture_bottle.png",
                "state": "rejected",
                "verdict": "rejected",
                "findings": [
                    {
                        "finding_id": "demo-find-cl-002-1",
                        "check_type": "wording",
                        "field": "headline",
                        "message": "Severe regulatory violation: Direct disease claim 'cures arthritis pain' is strictly prohibited for hemp/CBD products.",
                        "rule_id": "wording_cbd_disease_claims",
                        "proposed_fix": "Change to 'Supports Joint Comfort & Post-Workout Recovery'",
                        "file_ref": "FDA Warning Letter Guidelines / CalmLeaf Compliance Rulebook §1.1",
                    },
                ],
            },
            {
                "ad_id": "demo-ad-cl-003",
                "row_number": 3,
                "platform": "google_search",
                "platform_raw": "Google Ads",
                "headline": "Nighttime CBD Sleep Drops with Melatonin & Chamomile",
                "ad_text": "Formulated for deep restorative rest. Wake up refreshed without morning grogginess.",
                "destination_url": "https://calmleaf.example/sleep-drops?utm_source=google&utm_medium=cpc&utm_campaign=sleep_drops_search",
                "creative_file": "",
                "state": "approved",
                "verdict": "ready",
                "findings": [],
            },
        ],
    },
    {
        "batch_id": "demo-batch-oakhome-03",
        "uploaded_by": "demo_manager",
        "filename": "OakAndHome_Autumn_Mattress_Promo.csv",
        "client_id": "oakandhome",
        "created_at": datetime.now(timezone.utc) - timedelta(days=2),
        "sheet_hash": "hash_demo_oakhome_003",
        "ads": [
            {
                "ad_id": "demo-ad-oh-001",
                "row_number": 1,
                "platform": "meta",
                "platform_raw": "Facebook Carousel",
                "headline": "100% Organic Natural Latex Mattress — 100 Night Trial",
                "ad_text": "Breathable certified organic cotton and wool cover. Zero synthetic foams or chemical flame retardants.",
                "destination_url": "https://oakandhome.example/mattress?utm_source=meta&utm_medium=cpc&utm_campaign=autumn_latex_trial",
                "creative_file": "creatives/mattress_layers_1080x1080.jpg",
                "state": "approved",
                "verdict": "ready",
                "findings": [],
            },
            {
                "ad_id": "demo-ad-oh-002",
                "row_number": 2,
                "platform": "meta",
                "platform_raw": "Instagram Feed",
                "headline": "Save $300 on Handcrafted Oak Bed Frames This Weekend Only! Free Assembly Included with Every Order",
                "ad_text": "Solid American white oak frames built to last a lifetime. Designed in California with sustainable FSC-certified timber.",
                "destination_url": "https://oakandhome.example/frames?utm_source=meta&utm_medium=cpc&utm_campaign=bedframes_weekend",
                "creative_file": "creatives/bedframe_lifestyle.jpg",
                "state": "pending_review",
                "verdict": "needs_human_review",
                "findings": [
                    {
                        "finding_id": "demo-find-oh-002-1",
                        "check_type": "mechanical",
                        "field": "headline",
                        "message": "Platform text limit exceeded: Headline is 98 characters (Meta Feed limit: 40 characters for full visibility).",
                        "rule_id": "platform_headline_limit",
                        "proposed_fix": "Shorten to 'Save $300 on Handcrafted Oak Bed Frames'",
                        "file_ref": "Meta Advertising Specs (Headline truncation threshold)",
                    },
                ],
            },
        ],
    },
]


def seed_demo_data(force: bool = False) -> dict[str, Any]:
    """Seed comprehensive demo batches and ads into the local PostgreSQL database."""
    with get_session() as conn:
        # Check if already seeded and not force
        cur = conn.execute("SELECT COUNT(*) FROM batches WHERE batch_id LIKE 'demo-%'")
        count = cur.fetchone()[0]
        if count > 0 and not force:
            logger.info("Demo batches already exist; skipping mock seed.")
            return {"status": "exists", "demo_batches": count}

        logger.info("Seeding realistic demo batches, ads, findings, and approvals...")

        # Ensure clients exist
        for client_id in ["brightleaf", "calmleaf", "oakandhome"]:
            conn.execute(
                "INSERT INTO clients (client_id, name) VALUES (%s, %s) ON CONFLICT (client_id) DO NOTHING",
                (client_id, client_id.capitalize()),
            )

        # Clear existing demo data if force
        if force:
            conn.execute("DELETE FROM batches WHERE batch_id LIKE 'demo-%'")

        total_batches = 0
        total_ads = 0
        total_findings = 0

        for b in DEMO_BATCHES:
            # Insert Batch
            conn.execute(
                """INSERT INTO batches (batch_id, uploaded_by, created_at, filename, sheet_hash)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (batch_id) DO UPDATE SET
                     filename = EXCLUDED.filename,
                     created_at = EXCLUDED.created_at""",
                (b["batch_id"], b["uploaded_by"], b["created_at"], b["filename"], b["sheet_hash"]),
            )
            total_batches += 1

            for ad in b["ads"]:
                row_hash = hashlib.sha256(
                    f"{b['client_id']}:{ad['row_number']}:{ad['platform']}:{ad['headline']}:{ad['ad_text']}".encode("utf-8")
                ).hexdigest()

                # Insert Ad
                conn.execute(
                    """INSERT INTO ads (
                         ad_id, batch_id, client_id, row_number, platform, platform_raw,
                         ad_text, headline, destination_url, creative_file, row_hash, sheet_hash
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (ad_id) DO UPDATE SET
                         headline = EXCLUDED.headline,
                         ad_text = EXCLUDED.ad_text,
                         platform = EXCLUDED.platform,
                         destination_url = EXCLUDED.destination_url""",
                    (
                        ad["ad_id"],
                        b["batch_id"],
                        b["client_id"],
                        ad["row_number"],
                        ad["platform"],
                        ad["platform_raw"],
                        ad["ad_text"],
                        ad["headline"],
                        ad["destination_url"],
                        ad.get("creative_file", ""),
                        row_hash,
                        b["sheet_hash"],
                    ),
                )
                total_ads += 1

                # Insert or update approval state
                state = ad.get("state", "pending_review")
                conn.execute(
                    """INSERT INTO ad_approvals (
                         ad_id, client_id, state, content_hash, send_attempts, rejection_reason, approved_by, approved_at
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (ad_id) DO UPDATE SET
                         state = EXCLUDED.state,
                         content_hash = EXCLUDED.content_hash,
                         rejection_reason = EXCLUDED.rejection_reason""",
                    (
                        ad["ad_id"],
                        b["client_id"],
                        state,
                        row_hash,
                        3 if state == "dead_letter" else 1 if state == "sent" else 0,
                        "Unsubstantiated claims" if state == "rejected" else None,
                        "demo_buyer" if state in ("approved", "sent") else None,
                        datetime.now(timezone.utc) if state in ("approved", "sent") else None,
                    ),
                )

                # Insert Findings
                for f in ad.get("findings", []):
                    conn.execute(
                        """INSERT INTO findings (
                             finding_id, ad_id, row_number, check_type, message, field, proposed_fix, rule_id, file_ref
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (finding_id) DO UPDATE SET
                             message = EXCLUDED.message,
                             proposed_fix = EXCLUDED.proposed_fix,
                             file_ref = EXCLUDED.file_ref""",
                        (
                            f["finding_id"],
                            ad["ad_id"],
                            ad["row_number"],
                            f["check_type"],
                            f["message"],
                            f.get("field"),
                            f.get("proposed_fix"),
                            f.get("rule_id"),
                            f.get("file_ref"),
                        ),
                    )
                    total_findings += 1

                # Insert Alert if present
                if "alert" in ad:
                    al = ad["alert"]
                    conn.execute(
                        """INSERT INTO send_alerts (
                             alert_id, ad_id, client_id, batch_id, alert_type, severity, message, resolved
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (alert_id) DO NOTHING""",
                        (
                            al["alert_id"],
                            ad["ad_id"],
                            b["client_id"],
                            b["batch_id"],
                            al["alert_type"],
                            al["severity"],
                            al["message"],
                            False,
                        ),
                    )

                # Insert Evidence if present
                if "approval_evidence" in ad:
                    ev = ad["approval_evidence"]
                    conn.execute(
                        """INSERT INTO approval_evidence (
                             ad_id, client_id, content_hash, evidence_note, evidence_file, created_by
                           ) VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT DO NOTHING""",
                        (
                            ad["ad_id"],
                            b["client_id"],
                            row_hash,
                            ev["notes"],
                            "storage/evidence/promotional_pricing_waiver.pdf",
                            "demo_buyer",
                        ),
                    )

                # Insert Send Record if present
                if "send_record" in ad:
                    sr = ad["send_record"]
                    conn.execute(
                        """INSERT INTO ad_sends (
                             send_id, ad_id, client_id, batch_id, idempotency_key, content_hash,
                             platform, status, platform_ad_id
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (send_id) DO NOTHING""",
                        (
                            f"send-{ad['ad_id']}",
                            ad["ad_id"],
                            b["client_id"],
                            b["batch_id"],
                            f"idem-{ad['ad_id']}",
                            row_hash,
                            ad["platform"],
                            sr["status"],
                            sr["platform_ad_id"],
                        ),
                    )

        conn.commit()

        return {
            "status": "seeded",
            "batches_created": total_batches,
            "ads_created": total_ads,
            "findings_created": total_findings,
        }


@router.post("/reset")
def reset_mock_data(_user: CurrentUser = Depends(get_current_user)):
    """Reset or reload demo data with realistic mock batches."""
    result = seed_demo_data(force=True)
    return {"message": "Demo mock data reset successfully", "details": result}


@router.post("/seed")
def trigger_seed_mock_data():
    """Public helper to initialize mock data if missing."""
    result = seed_demo_data(force=False)
    return {"message": "Demo mock data check completed", "details": result}
