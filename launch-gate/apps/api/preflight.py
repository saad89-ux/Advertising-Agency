"""Real, reusable preflight checks; unavailable evidence never counts as a pass."""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from packages.contracts import AdContract
from packages.rules.utm_presence import check_utm_presence
from packages.rules.utm_naming import check_utm_naming
from packages.rules.platform_text_limits import check_platform_text_limits
from packages.rules.creative_size_ratio import check_creative_size_ratio

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def review_ad(ad: AdContract) -> list[dict]:
    checks = []

    def run(name, check):
        try:
            findings = check()
            checks.append({"name": name, "status": "fail" if findings else "pass",
                           "findings": [finding.message for finding in findings]})
        except Exception:
            logger.exception("Preflight check %s unavailable", name)
            checks.append({"name": name, "status": "not_checked", "findings": ["Check unavailable; see server logs."]})

    run("utm_presence", lambda: check_utm_presence(ad))
    run("utm_naming", lambda: check_utm_naming(ad))
    try:
        limits = json.loads((ROOT / "data/platform_limits.json").read_text(encoding="utf-8"))["platforms"]
    except Exception:
        logger.exception("Platform limits unavailable")
        limits = {}
    if ad.platform in limits:
        run("platform_text_limits", lambda: check_platform_text_limits(ad, limits))
    else:
        checks.append({"name": "platform_text_limits", "status": "not_checked", "findings": ["No platform limits available."]})

    # Only inspect opaque local references. Arbitrary URLs must never trigger an API-side fetch.
    if ad.creative_file and ad.platform in limits:
        try:
            from PIL import Image
            from apps.worker.file_storage import LocalFileStore
            content = LocalFileStore().get(ad.creative_file)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
            manifest = {ad.creative_file: {"width": width, "height": height}}
            run("creative_size_ratio", lambda: check_creative_size_ratio(ad, manifest, limits))
        except Exception:
            checks.append({"name": "creative_size_ratio", "status": "not_checked", "findings": ["Creative dimensions unavailable (video, remote asset, or unreadable image)."]})
    else:
        checks.append({"name": "creative_size_ratio", "status": "not_checked", "findings": ["No creative provided."]})

    from apps.worker.model_client import is_gemini_configured
    if is_gemini_configured():
        try:
            from apps.worker.wording_check import check_wording
            result = check_wording(ad)
            checks.append({"name": "wording", "status": "not_checked" if result.model_unavailable else "fail" if result.findings else "not_checked" if result.needs_person else "pass",
                           "findings": [finding.message for finding in result.findings] or [result.summary]})
        except Exception:
            logger.exception("Wording preflight unavailable")
            checks.append({"name": "wording", "status": "not_checked", "findings": ["Wording service unavailable."]})
    else:
        checks.append({"name": "wording", "status": "not_checked", "findings": ["Model credentials not configured."]})
    return checks
