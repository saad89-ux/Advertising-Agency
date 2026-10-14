"""Ad platform adapters and dry-run enforcement.

PRD §4.6 & Never-break Rule 7:
- Launch Gate v1 sends ONLY to the stand-in platform in dry-run mode.
- Real platform calls (Meta Marketing API, Google Ads API, TikTok Marketing API)
  are designed and documented, but strictly switched OFF.
"""

from __future__ import annotations

import abc
import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

STAND_IN_DEFAULT_URL = os.environ.get("STAND_IN_PLATFORM_URL") or os.environ.get("STAND_IN_URL") or "http://127.0.0.1:8100"


class RealPlatformCallDisabledError(RuntimeError):
    """Raised when an attempt is made to invoke real ad platform APIs.
    
    Never-break Rule 7: Real platform calls stay switched off in v1.
    """
    pass


class BasePlatformAdapter(abc.ABC):
    """Abstract base class for ad platform adapters."""

    @abc.abstractmethod
    def send_ad(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
        timeout: float | httpx.Timeout | None = None,
    ) -> httpx.Response:
        """Submit an ad payload with an idempotency key."""
        raise NotImplementedError


class StandInPlatformAdapter(BasePlatformAdapter):
    """Stand-in dry-run ad platform adapter.
    
    Communicates with the synthetic stand-in ad platform (data/stand_in_platform/app.py)
    via POST /v1/ads with Idempotency-Key header.
    """

    def __init__(
        self,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 5.0,
    ) -> None:
        if client is not None and hasattr(client, "base_url") and str(client.base_url).rstrip("/"):
            self.base_url = str(client.base_url).rstrip("/")
        else:
            self.base_url = (base_url or STAND_IN_DEFAULT_URL).rstrip("/")
        self._external_client = client
        self.timeout = timeout

    def send_ad(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
        timeout: float | httpx.Timeout | None = None,
    ) -> httpx.Response:
        """Post an ad to the stand-in dry-run platform."""
        url = f"{self.base_url}/v1/ads"
        headers = {
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        eff_timeout = timeout if timeout is not None else self.timeout

        if self._external_client is not None:
            # TestClient warns or fails if timeout is passed
            if self._external_client.__class__.__name__ == "TestClient":
                return self._external_client.post(
                    url,
                    json=payload,
                    headers=headers,
                )
            return self._external_client.post(
                url,
                json=payload,
                headers=headers,
                timeout=eff_timeout,
            )

        with httpx.Client(timeout=eff_timeout) as client:
            return client.post(
                url,
                json=payload,
                headers=headers,
            )


# =====================================================================
# Designed and Documented Real Platform Adapters (SWITCHED OFF in v1)
# =====================================================================

class MetaMarketingApiAdapter(BasePlatformAdapter):
    """Designed adapter for Meta Marketing API (v19.0+).
    
    Architecture Specification:
      Endpoint: POST https://graph.facebook.com/v19.0/act_{ad_account_id}/ads
      Headers: Authorization: Bearer {system_user_access_token}
      Payload:
        {
          "name": f"Ad_{ad_id}",
          "adset_id": "{adset_id}",
          "creative": {
            "title": headline,
            "body": text,
            "object_url": link,
            "image_hash": creative_hash
          },
          "status": "PAUSED"
        }
    
    Status: SWITCHED OFF (PRD §4.6, Never-break Rule 7).
    """

    def send_ad(self, payload: dict[str, Any], idempotency_key: str) -> httpx.Response:
        raise RealPlatformCallDisabledError(
            "Meta Marketing API calls are designed and documented, but strictly switched OFF "
            "in Launch Gate v1 (PRD §4.6, Rule 7). Use StandInPlatformAdapter instead."
        )


class GoogleAdsApiAdapter(BasePlatformAdapter):
    """Designed adapter for Google Ads API (v16+).
    
    Architecture Specification:
      Service: GoogleAdsService.MutateAdGroupAds
      Headers: developer-token, login-customer-id, OAuth2 Bearer token
      Payload:
        AdGroupAdOperation(
          create=AdGroupAd(
            ad_group="customers/{cid}/adGroups/{gid}",
            status=AdGroupAdStatus.PAUSED,
            ad=Ad(
              responsive_search_ad=ResponsiveSearchAdInfo(
                headlines=[AdTextAsset(text=headline)],
                descriptions=[AdTextAsset(text=text)],
                final_urls=[link]
              )
            )
          )
        )
    
    Status: SWITCHED OFF (PRD §4.6, Never-break Rule 7).
    """

    def send_ad(self, payload: dict[str, Any], idempotency_key: str) -> httpx.Response:
        raise RealPlatformCallDisabledError(
            "Google Ads API calls are designed and documented, but strictly switched OFF "
            "in Launch Gate v1 (PRD §4.6, Rule 7). Use StandInPlatformAdapter instead."
        )


class TikTokAdsApiAdapter(BasePlatformAdapter):
    """Designed adapter for TikTok Marketing API (v1.3+).
    
    Architecture Specification:
      Endpoint: POST https://business-api.tiktok.com/open_api/v1.3/ad/create/
      Headers: Access-Token: {token}
      Payload:
        {
          "advertiser_id": "{advertiser_id}",
          "adgroup_id": "{adgroup_id}",
          "ad_name": f"Ad_{ad_id}",
          "ad_text": text,
          "call_to_action": "LEARN_MORE",
          "landing_page_url": link,
          "operation_status": "DISABLE"
        }
    
    Status: SWITCHED OFF (PRD §4.6, Never-break Rule 7).
    """

    def send_ad(self, payload: dict[str, Any], idempotency_key: str) -> httpx.Response:
        raise RealPlatformCallDisabledError(
            "TikTok Marketing API calls are designed and documented, but strictly switched OFF "
            "in Launch Gate v1 (PRD §4.6, Rule 7). Use StandInPlatformAdapter instead."
        )


def get_platform_adapter(
    platform_name: str = "stand_in",
    *,
    dry_run: bool = True,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 5.0,
) -> BasePlatformAdapter:
    """Retrieve platform adapter, strictly enforcing dry-run mode and Never-break Rule 7."""
    if not dry_run:
        raise RealPlatformCallDisabledError(
            "Non-dry-run mode requested, but real ad platform calls are switched OFF in v1 "
            "(PRD §4.6, Never-break Rule 7). Only stand-in dry-run is allowed."
        )

    norm_name = (platform_name or "").strip().lower()
    if norm_name in ("meta", "facebook", "instagram"):
        if not dry_run:
            return MetaMarketingApiAdapter()
    elif norm_name in ("google", "google_search", "youtube"):
        if not dry_run:
            return GoogleAdsApiAdapter()
    elif norm_name in ("tiktok",):
        if not dry_run:
            return TikTokAdsApiAdapter()

    # In v1, always return the stand-in dry-run adapter
    return StandInPlatformAdapter(base_url=base_url, client=client, timeout=timeout)
