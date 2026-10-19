"""Meta Graph API & Token Diagnostics Script.

Queries the Meta Graph API using the current active API version (v26.0)
to inspect token validity, granted permissions/scopes, and remaining lifetime.
Never prints or logs secret token values.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
import httpx

CURRENT_GRAPH_API_VERSION = "v26.0"
LEGACY_GRAPH_API_VERSION = "v21.0"


def mask_token(token: str | None) -> str:
    """Mask token string safely."""
    if not token or not token.strip():
        return "<NOT SET>"
    clean = token.strip()
    if len(clean) <= 8:
        return "***"
    return f"{clean[:4]}...{clean[-4:]} (len: {len(clean)})"


def run_diagnostics() -> dict[str, any]:
    """Execute token inspection against Meta Graph API /debug_token."""
    load_dotenv()
    token = os.environ.get("META_MCP_ACCESS_TOKEN", "").strip()
    ad_account = os.environ.get("META_AD_ACCOUNT_ID", "").strip()

    print("=" * 70)
    print("  LAUNCH GATE: Meta Graph API Token Diagnostics")
    print(f"  Using Current Graph API Version: {CURRENT_GRAPH_API_VERSION}")
    print("=" * 70)
    print(f"Token Configured: {mask_token(token)}")
    print(f"Ad Account ID:    {ad_account[:4] + '***' if len(ad_account) > 4 else '<NOT SET>'}")
    print("-" * 70)

    if not token:
        print("[FAIL] META_MCP_ACCESS_TOKEN is not set in environment or .env.")
        return {"status": "missing_token"}

    url = f"https://graph.facebook.com/{CURRENT_GRAPH_API_VERSION}/debug_token"
    try:
        res = httpx.get(
            url,
            params={"input_token": token, "access_token": token},
            timeout=10.0,
        )
    except Exception as exc:
        print(f"[ERROR] Network connection failed: {exc}")
        return {"status": "network_error", "error": str(exc)}

    print(f"HTTP Status: {res.status_code}")
    data = res.json()

    if res.status_code == 200:
        d = data.get("data", {})
        is_valid = d.get("is_valid", False)
        exp = d.get("expires_at", 0)
        app_id = d.get("app_id")
        scopes = d.get("scopes", [])

        print(f"Token Is Valid: {is_valid}")
        print(f"App ID:         {app_id}")
        print(f"Scopes:         {scopes}")

        if exp == 0:
            print("Token Expiration: Never (Permanent / System User token)")
            return {"status": "valid", "is_valid": True, "remaining_seconds": None, "scopes": scopes}
        elif isinstance(exp, int) and exp > 0:
            now = int(time.time())
            remaining = exp - now
            exp_utc = datetime.fromtimestamp(exp, timezone.utc).isoformat()
            if remaining > 0:
                print(f"Token Expiration: {exp_utc} UTC")
                print(f"Remaining Lifetime: {remaining} seconds (~{remaining // 3600} hours)")
                return {"status": "valid", "is_valid": True, "remaining_seconds": remaining, "scopes": scopes}
            else:
                print(f"Token Expired At: {exp_utc} UTC ({abs(remaining)} seconds ago)")
                return {"status": "expired", "is_valid": False, "remaining_seconds": 0}
    else:
        err = data.get("error", {})
        err_msg = err.get("message", "Unknown error")
        err_code = err.get("code")
        err_subcode = err.get("error_subcode")
        print(f"[EXPIRED/ERROR] Code {err_code} (Subcode {err_subcode}): {err_msg}")
        return {
            "status": "error",
            "http_status": res.status_code,
            "code": err_code,
            "error_subcode": err_subcode,
            "message": err_msg,
        }


if __name__ == "__main__":
    run_diagnostics()
