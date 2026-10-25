"""Read-only Ad Library retrieval with explicit external failure states."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class AdLibraryResult:
    status: str
    http_status: int | None = None
    message: str | None = None
    ads: list[dict[str, Any]] | None = None
    user_supplied_fallback_available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def retrieve_ad_library(
    url: str,
    *,
    client: Any = None,
    timeout_seconds: float = 15.0,
) -> AdLibraryResult:
    """Retrieve public Ad Library evidence without judging unavailable ads as invalid."""
    http_client = client or httpx.Client(timeout=timeout_seconds)
    owns_client = client is None
    try:
        response = http_client.get(url)
        if response.status_code == 403:
            return AdLibraryResult(
                status="retrieval_forbidden",
                http_status=403,
                message="Ad Library denied access to the requested page.",
            )
        if response.status_code >= 400:
            return AdLibraryResult(
                status="retrieval_failed",
                http_status=response.status_code,
                message="Ad Library returned an unsuccessful response.",
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return AdLibraryResult(
                status="malformed_response",
                http_status=response.status_code,
                message="Ad Library returned a non-JSON response.",
            )
        if not isinstance(payload, (dict, list)):
            return AdLibraryResult(
                status="malformed_response",
                http_status=response.status_code,
                message="Ad Library returned an unsupported response shape.",
            )
        ads = payload.get("ads") if isinstance(payload, dict) else payload
        if not isinstance(ads, list):
            ads = [] if isinstance(payload, dict) else list(payload)
        return AdLibraryResult(status="retrieved", http_status=response.status_code, ads=ads)
    except httpx.TimeoutException:
        return AdLibraryResult(status="retrieval_timeout", message="Ad Library retrieval timed out.")
    except Exception:
        return AdLibraryResult(status="retrieval_failed", message="Ad Library retrieval was unavailable.")
    finally:
        if owns_client:
            http_client.close()
