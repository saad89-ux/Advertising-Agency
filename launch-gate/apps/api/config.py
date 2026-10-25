"""Shared ingest configuration values."""

import re


COLUMN_ALIASES = {
    "row_number": ("Row", "#", "ad no", "id", "No.", "row number"),
    "platform": ("Platform", "Channel", "network"),
    "ad_text": (
        "Ad text and headline as typed",
        "Text / Headline",
        "Primary Text",
        "Body Copy",
        "Body",
        "Ad text",
        "Text",
        "copy",
        "Ad Copy (text / headline)",
    ),
    "headline": ("Headline",),
    "destination_url": (
        "Link as typed",
        "Destination",
        "URL",
        "Link",
        "Destination URL",
        "landing page",
    ),
    "creative_file": ("Creative file", "File", "Asset", "Creative", "creative file name"),
    "creative_url": ("Creative URL", "Image URL", "Creative image URL"),
    "campaign_key": ("campaign_key", "Campaign Key", "Campaign ID Key"),
    "ad_set_key": ("ad_set_key", "Ad Set Key", "Adset Key"),
    "ad_key": ("ad_key", "Ad Key", "Ad Row Key"),
    "campaign_name": ("campaign_name", "Campaign Name"),
    "campaign_objective": ("campaign_objective", "Campaign Objective"),
    "campaign_budget_amount": ("campaign_budget_amount", "Campaign Budget Amount"),
    "campaign_special_category": ("campaign_special_category", "Campaign Special Category"),
    "campaign_budget_strategy": ("campaign_budget_strategy", "Campaign Budget Strategy"),
    "ad_set_name": ("ad_set_name", "Ad Set Name", "Adset Name"),
    "targeting_spec": ("targeting_spec", "Targeting Spec", "Targeting Specification"),
    "optimization_goal": ("optimization_goal", "Optimization Goal"),
    "billing_event": ("billing_event", "Billing Event"),
    "ad_set_budget_amount": ("ad_set_budget_amount", "Ad Set Budget Amount", "Adset Budget Amount"),
    "ad_name": ("ad_name", "Ad Name"),
    "body_copy": ("body_copy", "Body Copy"),
    "page_id": ("page_id", "Page ID", "Facebook Page ID"),
    "instagram_actor_id": ("instagram_actor_id", "Instagram Actor ID", "Instagram User ID"),
    "placements": ("placements", "Ad Set Placements"),
    "schedule": ("schedule", "Ad Set Schedule"),
    "creative_mime_type": ("creative_mime_type", "Creative MIME Type", "Creative Content Type"),
}


def normalize_column_label(label: str) -> str:
    """Normalize case, spaces, and punctuation for alias matching."""
    if label.strip() == "#":
        return "#"
    return re.sub(r"[^a-z0-9]+", "", label.casefold())


NORMALIZED_COLUMN_ALIASES = {
    normalize_column_label(alias): canonical
    for canonical, aliases in COLUMN_ALIASES.items()
    for alias in aliases
}
