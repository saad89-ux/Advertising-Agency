"""Infer a synthetic pilot client from an ad destination URL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit


def load_domain_map(path: str | Path | None = None) -> dict[str, str | None]:
    """Load the read-only domain map shipped with the repository's data pack."""
    map_path = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[2] / "data" / "clients" / "domain_map.json"
    )
    with map_path.open("r", encoding="utf-8") as source:
        raw = json.load(source)
    if not isinstance(raw, dict):
        raise ValueError("domain map must be a JSON object")
    return {
        str(host).casefold().strip(): value
        for host, value in raw.items()
        if host != "_note" and host != "local_storefront_base"
    }


def infer_client_id(
    destination_url: str | None, domain_map: Mapping[str, str | None]
) -> str | None:
    """Return the mapped client for a URL, or None when no rule maps its host."""
    if not isinstance(destination_url, str) or not destination_url.strip():
        return None

    value = destination_url.strip()
    try:
        if value.startswith("//"):
            parsed = urlsplit(value)
        else:
            parsed = urlsplit(value if "://" in value else f"//{value}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None

    normalized_host = host.casefold().rstrip(".")
    if parsed.scheme.casefold() == "http" and normalized_host == "localhost" and port == 8099:
        path_parts = [unquote(part) for part in parsed.path.split("/") if part]
        known_clients = {client for client in domain_map.values() if client is not None}
        return path_parts[0] if path_parts and path_parts[0] in known_clients else None

    normalized_map = {
        str(mapped_host).casefold().strip().rstrip("."): client
        for mapped_host, client in domain_map.items()
        if mapped_host != "local_storefront_base"
    }
    matches = [
        mapped_host
        for mapped_host in normalized_map
        if normalized_host == mapped_host or normalized_host.endswith("." + mapped_host)
    ]
    if not matches:
        return None
    return normalized_map[max(matches, key=len)]
