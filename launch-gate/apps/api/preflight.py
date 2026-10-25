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


def summarize_preflight_checks(checks: list[dict]) -> dict[str, object]:
    failed = [check for check in checks if check.get("status") == "fail"]
    unresolved = [check for check in checks if check.get("status") in {"needs_review", "not_checked"} or check.get("review_required")]
    if failed:
        status = "checks_failed"
        message = "Content failed: " + ", ".join(check["name"] for check in failed)
        if unresolved:
            message += "; human review required: " + ", ".join(check["name"] for check in unresolved)
    elif unresolved:
        status = "needs_review"
        message = "Human review required: " + ", ".join(check["name"] for check in unresolved)
    else:
        status = "ready_for_review"
        message = "All available preflight checks passed."

    def blocker(check: dict) -> dict[str, object]:
        return {
            "name": check["name"],
            "status": check.get("status"),
            "reason": check.get("reason"),
            "evidence_source": check.get("evidence_source"),
            "ad_id": check.get("ad_id"),
            "platform": check.get("platform"),
        }

    return {
        "status": status,
        "message": message,
        "failed_checks": [check["name"] for check in failed],
        "failed_check_details": [blocker(check) for check in failed],
        "review_required_checks": [check["name"] for check in unresolved],
        "review_required_check_details": [blocker(check) for check in unresolved],
    }


def review_ad(ad: AdContract) -> list[dict]:
    checks = []

    def run(name, check, evidence_source):
        try:
            findings = check()
            checks.append({"name": name, "status": "fail" if findings else "pass",
                           "reason": "rule_violation" if findings else "check_passed",
                           "evidence_source": evidence_source,
                           "findings": [finding.message for finding in findings],
                           "details": [{"field": finding.field, "actual_value": finding.actual_value,
                                        "expected_format": finding.expected_format,
                                        "proposed_fix": finding.proposed_fix} for finding in findings]})
        except Exception:
            logger.exception("Preflight check %s unavailable", name)
            checks.append({"name": name, "status": "not_checked", "reason": "check_unavailable",
                           "evidence_source": evidence_source, "findings": ["Check unavailable; see server logs."], "details": []})

    run("utm_presence", lambda: check_utm_presence(ad), "campaign_data")
    run("utm_naming", lambda: check_utm_naming(ad), "client_naming_convention")
    try:
        limits = json.loads((ROOT / "data/platform_limits.json").read_text(encoding="utf-8"))["platforms"]
    except Exception:
        logger.exception("Platform limits unavailable")
        limits = {}
    if ad.platform in limits:
        run("platform_text_limits", lambda: check_platform_text_limits(ad, limits), "platform_limits")
    else:
        checks.append({"name": "platform_text_limits", "status": "not_checked", "reason": "platform_limits_unavailable",
                       "evidence_source": "platform_rules", "findings": ["No platform limits available."]})

    # Only inspect opaque local references. Arbitrary URLs must never trigger an API-side fetch.
    if not ad.creative_file:
        allowed = limits.get(ad.platform, {}).get("ratios", {}).get("value", [])
        checks.append({"name": "creative_size_ratio", "status": "not_checked", "reason": "creative_missing",
                       "evidence_source": "creative_file", "findings": ["No creative provided; dimensions were not checked."],
                       "details": [{"field": "creative_file", "actual_value": "<missing>",
                                    "expected_format": "/".join(allowed) or None}]})
    elif ad.platform not in limits:
        checks.append({"name": "creative_size_ratio", "status": "not_checked", "reason": "platform_limits_unavailable",
                       "evidence_source": "creative_file_and_platform_limits",
                       "findings": ["Creative dimensions cannot be evaluated without platform limits."], "details": []})
    else:
        try:
            from PIL import Image
            from apps.worker.file_storage import LocalFileStore
            content = LocalFileStore().get(ad.creative_file)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
            manifest = {ad.creative_file: {"width": width, "height": height}}
            findings = check_creative_size_ratio(ad, manifest, limits)
            ratios = limits[ad.platform].get("ratios", {}).get("value", [])
            actual_ratio = width / height
            checks.append({
                "name": "creative_size_ratio",
                "status": "fail" if findings else "pass",
                "reason": "ratio_not_allowed" if findings else "dimensions_checked",
                "evidence_source": "creative_file_and_platform_limits",
                "findings": [finding.message for finding in findings],
                "details": [{"field": "creative_file", "actual_value": f"{width}x{height} pixels ({width}:{height}, {actual_ratio:.3f}:1)",
                             "expected_format": "/".join(ratios) + " (2% ratio tolerance)" if ratios else "No ratio limits configured",
                             "proposed_fix": findings[0].proposed_fix if findings else None}],
            })
        except Exception:
            logger.exception("Creative dimensions unavailable for %s", ad.ad_id)
            checks.append({"name": "creative_size_ratio", "status": "not_checked", "reason": "creative_dimensions_unavailable",
                           "evidence_source": "creative_file", "findings": ["Creative dimensions unavailable (video, remote asset, or unreadable image)."], "details": []})

    from apps.worker.model_client import is_gemini_configured
    if is_gemini_configured():
        try:
            from apps.worker.wording_check import check_wording
            result = check_wording(ad)
            no_rules = not result.retrieved_rules
            verified_findings = [finding for finding in result.findings if finding.rule_id]
            status = "fail" if verified_findings else "not_checked" if result.model_unavailable else "needs_review" if no_rules or result.needs_person else "pass"
            reason = "verified_rule_violation" if verified_findings else result.review_reason or ("client_rules_missing" if no_rules else "human_review_required" if result.needs_person else "rules_checked")
            checks.append({"name": "wording", "status": status, "reason": reason,
                           "review_required": bool(result.needs_person or no_rules or result.model_unavailable),
                           "evidence_source": "client_rules_and_model",
                           "findings": [finding.message for finding in result.findings] or [result.summary]})
        except Exception:
            logger.exception("Wording preflight unavailable")
            checks.append({"name": "wording", "status": "not_checked", "reason": "wording_service_unavailable",
                           "evidence_source": "client_rules_and_model", "findings": ["Wording service unavailable."]})
    else:
        checks.append({"name": "wording", "status": "not_checked", "reason": "model_credentials_unavailable",
                       "evidence_source": "client_rules_and_model", "findings": ["Model credentials not configured; human review required."]})
    return checks
