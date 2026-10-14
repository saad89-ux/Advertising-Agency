"""Shared ingest configuration values."""

import re


COLUMN_ALIASES = {
    "row_number": ("Row", "#", "ad no", "id", "No.", "row number"),
    "platform": ("Platform", "Channel", "network"),
    "ad_text": (
        "Ad text and headline as typed",
        "Text / Headline",
        "Primary Text",
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
