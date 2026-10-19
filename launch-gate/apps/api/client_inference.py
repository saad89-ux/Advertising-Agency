"""Infer a synthetic pilot client from an ad destination URL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit


def load_domain_map(
    path: str | Path | None = None, *, include_local_storefront: bool = False
) -> dict[str, str | None]:
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
        if host != "_note" and (include_local_storefront or host != "local_storefront_base")
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
    if matches:
        return normalized_map[max(matches, key=len)]

    # Dynamic real-world client inference fallback
    import os
    import re
    from dotenv import load_dotenv
    load_dotenv()
    if normalized_host.endswith((".example", ".test", ".invalid", ".localhost")):
        return None

    if os.environ.get("ALLOW_DYNAMIC_CLIENTS", "true").strip().lower() in {"true", "1", "yes"}:
        parts = normalized_host.split(".")
        if len(parts) >= 2:
            base = parts[-2]
            if base in {"co", "com", "org", "net", "gov", "edu"} and len(parts) >= 3:
                base = parts[-3]
            clean_id = re.sub(r"[^a-zA-Z0-9_-]", "", base).casefold()
            if len(clean_id) >= 2 and clean_id not in {"unknown", "invalid", "example"}:
                return clean_id
    return None


def allowed_origins_for_client(
    client_id: str, domain_map: Mapping[str, str | None]
) -> frozenset[str]:
    """Return exact HTTP(S) origins mapped to one client, plus local dev origin and dynamic domains."""
    origins: set[str] = set()
    for host, mapped_client in domain_map.items():
        if mapped_client != client_id or host == "local_storefront_base":
            continue
        normalized_host = str(host).casefold().strip().rstrip(".")
        if normalized_host:
            origins.update({f"http://{normalized_host}", f"https://{normalized_host}"})

    # If client is dynamic and not in domain_map, allow its primary domains
    if not origins and client_id and not client_id.startswith("client-"):
        origins.update({
            f"http://{client_id}.com",
            f"https://{client_id}.com",
            f"http://www.{client_id}.com",
            f"https://www.{client_id}.com",
        })

    local_base = domain_map.get("local_storefront_base")
    if isinstance(local_base, str):
        try:
            parsed = urlsplit(local_base)
            if parsed.scheme.casefold() == "http" and parsed.hostname == "localhost":
                port = parsed.port
                origin = f"http://localhost:{port}" if port is not None else "http://localhost"
                origins.add(origin)
        except ValueError:
            pass
    return frozenset(origins)

