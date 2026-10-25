"""Real MCP Smoke Test Script for Meta Ads & Social Platforms.

Connects to the official remote MCP server, performs the initialize handshake,
lists discovered tools, and executes ONE read-only tool to verify live connectivity.

Safety & Contract:
- Reads credentials strictly from environment (`.env`).
- Never exposes or logs secret tokens.
- Never performs mutations or writes.
- Exit code 0: OK (verified real response from provider)
- Exit code 1: Configuration missing (.env incomplete)
- Exit code 3: Authentication / authorization error (HTTP 401 / 403)
- Exit code 4: API / transport / server error
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from apps.worker.meta_mcp_client import (
    MetaMCPClient,
    MetaMCPConfigurationError,
    ACTION_READ_PERFORMANCE,
    ACTION_READ_AD_REVIEW_STATUS,
    resolve_tool_name,
)


def _mask_token(tok: str) -> str:
    """Mask secret token for safe logging."""
    if not tok:
        return "<EMPTY>"
    if len(tok) <= 8:
        return "***"
    return f"{tok[:4]}...{tok[-4:]}"


def run_meta_smoke() -> int:
    """Execute smoke test against Meta Ads MCP server."""
    print("=" * 70)
    print("  LAUNCH GATE: Meta Ads MCP Live Smoke Test")
    print("=" * 70)

    load_dotenv()

    server_url = os.environ.get("MCP_META_SERVER_URL", "").strip() or "https://mcp.facebook.com/ads"
    access_token = os.environ.get("META_SYSTEM_USER_TOKEN", "").strip()
    ad_account_id = ""

    print(f"Target MCP Endpoint : {server_url}")
    print(f"Token Configured    : {'YES (' + _mask_token(access_token) + ')' if access_token else 'NO'}")
    print(f"Ad Account ID       : {ad_account_id or '<NOT SET>'}")
    print("-" * 70)

    # 1. Configuration Check
    if not access_token:
        print("\n[CONFIG ERROR] META_SYSTEM_USER_TOKEN is missing in .env.")
        print("Please add META_SYSTEM_USER_TOKEN to .env with the agency System User token.")
        print("Required scopes: ads_management, ads_read, ads_mcp_management.")
        return 1

    client = MetaMCPClient(
        server_url=server_url,
        access_token=access_token,
        allow_writes=False,  # Enforce read-only
        timeout_seconds=25.0,
        max_retries=2,
    )

    # 2. Handshake & Tool Listing
    print("\n[Step 1/2] Connecting to Meta MCP server and listing tools...")
    start_time = time.monotonic()
    try:
        tools = client.list_tools_sync()
        duration = time.monotonic() - start_time
        print(f"[OK] Handshake successful in {duration:.2f}s. Discovered {len(tools)} tools.")
        tool_names = [t.name for t in tools]
        print(f"Sample discovered tools: {', '.join(tool_names[:8])}...")
    except BaseException as exc:
        duration = time.monotonic() - start_time
        err_msg = str(exc)
        print(f"\n[FAIL] Handshake failed after {duration:.2f}s: {err_msg}")
        if "401" in err_msg or "unauthorized" in err_msg.lower():
            print("\n>> Cause: HTTP 401 Unauthorized.")
            print(">> Your META_SYSTEM_USER_TOKEN is invalid, expired, or missing required scopes.")
            return 3
        if "403" in err_msg or "forbidden" in err_msg.lower():
            print("\n>> Cause: HTTP 403 Forbidden.")
            print(">> Your token lacks permission for this Meta Business account.")
            return 3
        return 4

    # 3. Call ONE Read-Only Tool
    print("\n[Step 2/2] Executing read-only tool verification...")
    if ad_account_id:
        target_account = ad_account_id.replace("act_", "").strip()
        chosen_tool = resolve_tool_name(ACTION_READ_PERFORMANCE, tool_names)
        tool_args = {
            "ad_account_id": target_account,
        }
        print(f"Calling tool '{chosen_tool}' for account '{target_account}'...")
    else:
        # Fallback to ads_get_ad_accounts if no specific account ID is set
        chosen_tool = "ads_get_ad_accounts" if "ads_get_ad_accounts" in tool_names else tool_names[0]
        tool_args = {}
        print(f"No workspace account was supplied. Calling account listing tool '{chosen_tool}'...")

    call_start = time.monotonic()
    res = client.call_tool_sync(chosen_tool, tool_args)
    call_dur = time.monotonic() - call_start

    if not res.success:
        print(f"\n[FAIL] Tool execution '{chosen_tool}' failed after {call_dur:.2f}s:")
        print(f"Error: {res.error}")
        if res.status_code in {401, 403} or ("401" in (res.error or "") or "403" in (res.error or "")):
            return 3
        return 4

    print(f"[OK] Real response received from Meta in {call_dur:.2f}s:")
    print("-" * 70)
    print(json.dumps(res.result, indent=2))
    print("-" * 70)
    print("\n[SUCCESS] Meta MCP connection and read-only verification completed successfully.")
    return 0


def run_tiktok_smoke() -> int:
    """Execute smoke test against TikTok for Business MCP server."""
    print("=" * 70)
    print("  LAUNCH GATE: TikTok for Business MCP Live Smoke Test")
    print("=" * 70)

    load_dotenv()

    server_url = (
        os.environ.get("MCP_TIKTOK_SERVER_URL", "").strip()
        or "https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat"
    )
    access_token = os.environ.get("TIKTOK_MCP_ACCESS_TOKEN", "").strip()
    advertiser_id = os.environ.get("TIKTOK_ADVERTISER_ID", "").strip()

    print(f"Target MCP Endpoint : {server_url}")
    print(f"Token Configured    : {'YES (' + _mask_token(access_token) + ')' if access_token else 'NO'}")
    print(f"Advertiser ID       : {advertiser_id or '<NOT SET>'}")
    print("-" * 70)

    if not access_token:
        print("\n[CONFIG ERROR] TIKTOK_MCP_ACCESS_TOKEN is missing in .env.")
        print("Please add TIKTOK_MCP_ACCESS_TOKEN to .env with your TikTok for Business Bearer token.")
        print("Authorization lifespan is 30 days.")
        return 1

    from apps.worker.tiktok_mcp_client import TikTokMCPClient

    client = TikTokMCPClient(
        server_url=server_url,
        access_token=access_token,
        advertiser_id=advertiser_id,
        allow_writes=False,
        timeout_seconds=25.0,
        max_retries=2,
    )

    print("\n[Step 1/2] Connecting to TikTok MCP server and listing tools...")
    start_time = time.monotonic()
    try:
        tools = client.list_tools_sync()
        duration = time.monotonic() - start_time
        print(f"[OK] Handshake successful in {duration:.2f}s. Discovered {len(tools)} tools.")
        tool_names = [t.name for t in tools]
        print(f"Sample discovered tools: {', '.join(tool_names[:8])}...")
    except BaseException as exc:
        duration = time.monotonic() - start_time
        err_msg = str(exc)
        print(f"\n[FAIL] Handshake failed after {duration:.2f}s: {err_msg}")
        if "401" in err_msg or "unauthorized" in err_msg.lower():
            print("\n>> Cause: HTTP 401 Unauthorized.")
            print(">> Your TIKTOK_MCP_ACCESS_TOKEN is invalid or has expired.")
            return 3
        if "403" in err_msg or "forbidden" in err_msg.lower():
            print("\n>> Cause: HTTP 403 Forbidden.")
            return 3
        return 4

    print("\n[Step 2/2] Executing read-only tool verification...")
    chosen_tool = "campaign_get" if "campaign_get" in tool_names else tool_names[0]
    tool_args: dict[str, Any] = {}
    if advertiser_id:
        tool_args["advertiser_id"] = advertiser_id

    call_start = time.monotonic()
    res = client.call_tool_sync(chosen_tool, tool_args)
    call_dur = time.monotonic() - call_start

    if not res.success:
        print(f"\n[FAIL] Tool execution '{chosen_tool}' failed after {call_dur:.2f}s:")
        print(f"Error: {res.error}")
        if res.status_code in {401, 403} or ("401" in (res.error or "") or "403" in (res.error or "")):
            return 3
        return 4

    print(f"[OK] Real response received from TikTok in {call_dur:.2f}s:")
    print("-" * 70)
    print(json.dumps(res.result, indent=2))
    print("-" * 70)
    print("\n[SUCCESS] TikTok MCP connection and read-only verification completed successfully.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Live smoke test for Social Ad Platform MCP servers.")
    parser.add_argument(
        "--platform",
        choices=["meta", "tiktok"],
        default="meta",
        help="Target platform to smoke test (default: meta).",
    )
    args = parser.parse_args()

    if args.platform == "tiktok":
        exit_code = run_tiktok_smoke()
    else:
        exit_code = run_meta_smoke()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
