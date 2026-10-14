"""Model Context Protocol (MCP) Client & Tool Dispatcher for Meta Ads.

Implements JSON-RPC 2.0 tool execution protocol for:
- Meta Ads Graph MCP

Standardizes Meta campaign deployment, live status polling, and policy violation
alerts via standard MCP tool contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
import uuid

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
META_CREATE_TOOLS = ("ads_create_campaign", "ads_create_ad_set", "ads_create_creative", "ads_create_ad")
META_WRITE_TOOLS = frozenset((*META_CREATE_TOOLS, "ads_activate_entity", "ads_creative_upload_local_image", "ads_finalize_local_ad_image_upload"))
_META_TOOL_CATALOG = Path(__file__).resolve().parents[2] / "data" / "meta_mcp_tools.json"


def meta_live_writes_enabled() -> bool:
    truthy = {"1", "true", "yes"}
    return all(os.environ.get(key, "false").strip().lower() in truthy for key in ("META_MCP_ENABLE_LIVE", "META_MCP_ALLOW_WRITES"))


def discovered_meta_schemas() -> dict[str, dict[str, Any]]:
    with _META_TOOL_CATALOG.open("r", encoding="utf-8") as source:
        catalog = json.load(source)
    return {tool["name"]: tool["input_schema"] for tool in catalog.get("tools", [])}


def validate_meta_arguments(tool_name: str, arguments: dict[str, Any]) -> None:
    schema = discovered_meta_schemas().get(tool_name)
    if schema is None:
        raise ValueError(f"Meta tool '{tool_name}' is not in the discovered schema catalog.")
    properties = schema.get("properties", {})
    missing = [name for name in schema.get("required", []) if arguments.get(name) in (None, "")]
    if missing:
        raise ValueError(f"Meta tool '{tool_name}' requires: {', '.join(missing)}.")
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ValueError(f"Meta tool '{tool_name}' does not accept: {', '.join(unknown)}.")
    for name, value in arguments.items():
        allowed = properties[name].get("enum")
        if allowed and value not in allowed:
            raise ValueError(f"{value!r} is not allowed for {name} on {tool_name}.")


def is_discovered_write_tool(tool_name: str) -> bool:
    with _META_TOOL_CATALOG.open("r", encoding="utf-8") as source:
        catalog = json.load(source)
    return any(tool.get("name") == tool_name and bool(tool.get("is_write")) for tool in catalog.get("tools", []))


@dataclass
class MCPToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPResponse:
    success: bool
    platform: str
    tool_name: str
    result: dict[str, Any]
    error: str | None = None
    rpc_id: str = field(default_factory=lambda: f"mcp_{uuid.uuid4().hex[:8]}")


class MCPPlatformDispatcher:
    """Manages Meta MCP tool execution for Facebook and Instagram placements."""

    def __init__(self) -> None:
        self.meta_mcp_url = os.environ.get("MCP_META_SERVER_URL", "").strip()

    def list_tools(self, platform: str) -> list[MCPToolDefinition]:
        """Return the real Meta campaign creation tools from the discovered catalog."""
        if platform.lower() not in {"facebook", "instagram", "meta"}:
            return []
        schemas = discovered_meta_schemas()
        return [MCPToolDefinition(name, "Discovered Meta Ads MCP tool.", schemas[name]) for name in META_CREATE_TOOLS if name in schemas]

    def call_tool(
        self,
        platform: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> MCPResponse:
        """Execute an MCP tool call over standard JSON-RPC 2.0 protocol.

        Mutations require both explicit live/write opt-ins and schema-valid arguments.
        """
        rpc_id = f"mcp_{uuid.uuid4().hex[:8]}"

        logger.info("[MCP EXECUTE] Platform: %s | Tool: %s | RPC ID: %s", platform, tool_name, rpc_id)

        if platform not in {"facebook", "instagram"}:
            return MCPResponse(
                success=False,
                platform=platform,
                tool_name=tool_name,
                result={},
                error="unsupported_platform_meta_only",
                rpc_id=rpc_id,
            )

        if tool_name.endswith("_dispatch_ad"):
            return MCPResponse(
                success=False,
                platform=platform,
                tool_name=tool_name,
                result={},
                error="unsupported_legacy_dispatch_tool",
                rpc_id=rpc_id,
            )

        live_mcp_enabled = os.environ.get("META_MCP_ENABLE_LIVE", "false").strip().lower() in {
            "1", "true", "yes"
        }
        writes_allowed = os.environ.get("META_MCP_ALLOW_WRITES", "false").strip().lower() in {"1", "true", "yes"}
        is_write = tool_name in META_WRITE_TOOLS or is_discovered_write_tool(tool_name)
        if is_write and not (live_mcp_enabled and writes_allowed):
            return MCPResponse(
                success=False,
                platform=platform,
                tool_name=tool_name,
                result={},
                error="real_platform_calls_disabled",
                rpc_id=rpc_id,
            )
        try:
            validate_meta_arguments(tool_name, arguments)
        except ValueError as exc:
            return MCPResponse(False, platform, tool_name, {}, str(exc), rpc_id)
        account_id = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
        access_token = os.environ.get("META_MCP_ACCESS_TOKEN", "").strip()
        if is_write and not account_id:
            return MCPResponse(False, platform, tool_name, {}, "META_AD_ACCOUNT_ID is not configured.", rpc_id)
        if live_mcp_enabled and self.meta_mcp_url and access_token:
            try:
                from apps.worker.meta_mcp_client import MetaMCPClient

                response = MetaMCPClient(enable_live=live_mcp_enabled, allow_writes=writes_allowed).call_tool_sync(
                    tool_name,
                    arguments,
                    tool_description=f"Meta {tool_name} campaign lifecycle operation",
                )
                return MCPResponse(
                    success=response.success,
                    platform=platform,
                    tool_name=tool_name,
                    result=response.result,
                    error=response.error,
                    rpc_id=response.rpc_id,
                )
            except Exception as exc:
                logger.exception("Meta MCP dispatch failed for %s", tool_name)
                return MCPResponse(
                    success=False,
                    platform=platform,
                    tool_name=tool_name,
                    result={},
                    error=f"meta_mcp_error: {exc}",
                    rpc_id=rpc_id,
                )

        return MCPResponse(
            success=False,
            platform=platform,
            tool_name=tool_name,
            result={},
            error="connector_not_configured",
            rpc_id=rpc_id,
        )

    def dispatch_paused_ad(
        self,
        *,
        platform: str,
        ad_id: str,
        campaign_name: str,
        headline: str,
        ad_text: str,
        destination_url: str,
        ad_account_id: str,
        page_id: str,
        instagram_user_id: str | None,
        targeting_country: str,
        daily_budget_minor_units: int,
        image_url: str,
        special_ad_category: str = "NONE",
        existing_ids: dict[str, str] | None = None,
        persist_stage=None,
    ) -> MCPResponse:
        """Create or resume one deterministic four-object chain, leaving every object paused."""
        if platform not in {"facebook", "instagram"}:
            return MCPResponse(False, platform, "ads_create_campaign", {}, "unsupported_platform_meta_only")
        if not meta_live_writes_enabled():
            return MCPResponse(False, platform, "ads_create_campaign", {}, "real_platform_calls_disabled")
        if not all((ad_account_id, page_id, targeting_country, image_url)) or daily_budget_minor_units < 1:
            return MCPResponse(False, platform, "ads_create_campaign", {}, "Required Meta launch details are missing.")
        if platform == "instagram" and not instagram_user_id:
            return MCPResponse(False, platform, "ads_create_creative", {}, "Instagram account ID is required for Instagram placement.")
        if not image_url.startswith("https://"):
            return MCPResponse(False, platform, "ads_create_creative", {}, "Meta requires a publicly accessible HTTPS creative image URL.")

        safe_name = re.sub(r"[^A-Za-z0-9 _-]+", "", campaign_name).strip()[:80] or "Launch Gate Campaign"
        suffix = f"{ad_id}-{platform}"
        names = {
            "campaign": f"{safe_name[:50]}-{suffix}",
            "ad_set": f"{suffix}-adset",
            "creative": f"{suffix}-creative",
            "ad": f"{suffix}-ad",
        }
        ids = dict(existing_ids or {})
        conversation_id = uuid.uuid4().hex[:20]
        advertiser_request = "Review and update the campaign preview, then confirm launch on the selected Meta platform(s)."
        advertiser_job = "create_and_launch_new_ads"

        def rows_from(result: Any) -> list[dict[str, Any]]:
            if isinstance(result, list):
                return [row for row in result if isinstance(row, dict)]
            if not isinstance(result, dict):
                return []
            for key in ("data", "entities", "results", "creatives"):
                value = result.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
            return []

        def next_cursor(result: Any) -> str | None:
            if not isinstance(result, dict):
                return None
            for key in ("next_cursor", "cursor_next"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    return value
            paging = result.get("paging")
            if isinstance(paging, dict):
                for key in ("next_cursor", "after"):
                    value = paging.get(key)
                    if isinstance(value, str) and value:
                        return value
                cursors = paging.get("cursors")
                if isinstance(cursors, dict) and isinstance(cursors.get("after"), str):
                    return cursors["after"]
            return None

        def lookup_entity(level: str, name: str) -> str | None:
            matching = []
            cursor = None
            for _ in range(100):
                arguments = {
                    "ad_account_id": ad_account_id,
                    "level": level,
                    "limit": 100,
                    "client_conversation_id": conversation_id,
                    "advertiser_request": advertiser_request,
                    "advertiser_job": advertiser_job,
                }
                if cursor:
                    arguments["cursor"] = cursor
                response = self.call_tool(platform, "ads_get_ad_entities", arguments)
                if not response.success:
                    raise RuntimeError(response.error or f"Could not reconcile Meta {level} objects.")
                matching.extend(
                    str(row.get("id") or row.get(f"{level}_id") or row.get("ad_object_id"))
                    for row in rows_from(response.result)
                    if row.get("name") == name and (row.get("id") or row.get(f"{level}_id") or row.get("ad_object_id"))
                )
                cursor = next_cursor(response.result)
                if not cursor:
                    break
            else:
                raise RuntimeError(f"Meta {level} lookup exceeded 100 pages; refusing a potentially duplicate create.")
            if len(matching) > 1:
                raise RuntimeError(f"Multiple Meta {level} objects match the deterministic launch name; refusing to create duplicates.")
            return matching[0] if matching else None

        def lookup_creative(name: str) -> str | None:
            matching = []
            cursor = None
            for _ in range(100):
                arguments = {
                    "ad_account_id": ad_account_id,
                    "limit": 100,
                    "client_conversation_id": conversation_id,
                    "advertiser_request": advertiser_request,
                    "advertiser_job": advertiser_job,
                }
                if cursor:
                    arguments["cursor"] = cursor
                response = self.call_tool(platform, "ads_get_creatives", arguments)
                if not response.success:
                    raise RuntimeError(response.error or "Could not reconcile Meta creative objects.")
                matching.extend(str(row.get("id")) for row in rows_from(response.result) if row.get("name") == name and row.get("id"))
                cursor = next_cursor(response.result)
                if not cursor:
                    break
            else:
                raise RuntimeError("Meta creative lookup exceeded 100 pages; refusing a potentially duplicate create.")
            if len(matching) > 1:
                raise RuntimeError("Multiple Meta creatives match the deterministic launch name; refusing to create duplicates.")
            return matching[0] if matching else None

        def object_id(result: Any, keys: tuple[str, ...]) -> str | None:
            queue = [result]
            while queue:
                current = queue.pop(0)
                if isinstance(current, dict):
                    for key in keys:
                        value = current.get(key)
                        if isinstance(value, (str, int)) and str(value).strip():
                            return str(value)
                    queue.extend(value for value in current.values() if isinstance(value, (dict, list)))
                elif isinstance(current, list):
                    queue.extend(current)
            return None

        def create_or_resume(stage: str, level: str, tool_name: str, arguments: dict[str, Any], keys: tuple[str, ...]) -> str:
            if ids.get(stage):
                return ids[stage]
            found = lookup_creative(names[stage]) if stage == "creative" else lookup_entity(level, names[stage])
            if found:
                ids[stage] = found
                if persist_stage:
                    persist_stage(stage, found)
                return found
            response = self.call_tool(platform, tool_name, arguments)
            if not response.success:
                raise RuntimeError(response.error or f"Meta {tool_name} failed.")
            created_id = object_id(response.result, keys)
            if not created_id:
                raise RuntimeError(f"Meta {tool_name} returned no object ID.")
            ids[stage] = created_id
            if persist_stage:
                persist_stage(stage, created_id)
            return created_id

        context = {
            "client_conversation_id": conversation_id,
            "advertiser_request": advertiser_request,
            "advertiser_job": advertiser_job,
        }
        try:
            campaign_id = create_or_resume("campaign", "campaign", "ads_create_campaign", {
                **context,
                "ad_account_id": ad_account_id,
                "campaign_name": names["campaign"],
                "objective": "OUTCOME_TRAFFIC",
                "buying_type": "AUCTION",
                "special_ad_categories": json.dumps([] if special_ad_category == "NONE" else [special_ad_category]),
                "campaign_daily_budget": daily_budget_minor_units,
                "campaign_bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                **({"special_ad_category_country": json.dumps([targeting_country.upper()])} if special_ad_category != "NONE" else {}),
            }, ("campaign_id", "id"))

            ad_set_id = create_or_resume("ad_set", "adset", "ads_create_ad_set", {
                **context,
                "ad_account_id": ad_account_id,
                "campaign_id": campaign_id,
                "ad_set_name": names["ad_set"],
                "billing_event": "IMPRESSIONS",
                "optimization_goal": "LINK_CLICKS",
                "targeting": json.dumps({"geo_locations": {"countries": [targeting_country.upper()]}, "publisher_platforms": [platform]}),
                "destination_type": "WEBSITE",
            }, ("ad_set_id", "adset_id", "id"))

            creative_arguments = {
                **context,
                "ad_account_id": ad_account_id,
                "page_id": page_id,
                "image_url": image_url,
                "link_url": destination_url,
                "message": ad_text,
                "headline": headline,
                "name": names["creative"],
            }
            if platform == "instagram":
                creative_arguments["instagram_user_id"] = instagram_user_id
            creative_id = create_or_resume("creative", "ad", "ads_create_creative", creative_arguments, ("creative_id", "id"))

            meta_ad_id = create_or_resume("ad", "ad", "ads_create_ad", {
                **context,
                "ad_account_id": ad_account_id,
                "ad_set_id": ad_set_id,
                "ad_name": names["ad"],
                "creative": json.dumps({"creative_id": creative_id}),
            }, ("ad_id", "id"))
            activation_targets = (
                ("campaign", campaign_id, [campaign_id, ad_set_id, meta_ad_id]),
                ("ad_set", ad_set_id, None),
                ("ad", meta_ad_id, None),
            )
            for entity_type, entity_id, object_ids in activation_targets:
                activation_arguments = {
                    **context,
                    "ad_account_id": ad_account_id,
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "publish_as_active": True,
                }
                if object_ids:
                    activation_arguments["object_ids"] = object_ids
                activation = self.call_tool(platform, "ads_activate_entity", activation_arguments)
                if not activation.success:
                    return MCPResponse(False, platform, activation.tool_name, {
                        "campaign_id": campaign_id, "ad_set_id": ad_set_id,
                        "creative_id": creative_id, "platform_ad_id": meta_ad_id,
                        "status": "paused" if entity_type == "campaign" else "activation_incomplete",
                    }, f"meta_chain_incomplete: {activation.error or f'Meta {entity_type} activation failed.'}")
            return MCPResponse(True, platform, "ads_create_ad", {
                "platform_ad_id": meta_ad_id,
                "campaign_id": campaign_id,
                "ad_set_id": ad_set_id,
                "creative_id": creative_id,
                "status": "active_live",
                "client_conversation_id": conversation_id,
            })
        except Exception as exc:
            logger.exception("Meta paused creation chain failed for ad %s on %s", ad_id, platform)
            return MCPResponse(False, platform, "ads_create_ad", {
                "campaign_id": ids.get("campaign"),
                "ad_set_id": ids.get("ad_set"),
                "creative_id": ids.get("creative"),
                "platform_ad_id": ids.get("ad"),
                "status": "paused",
            }, f"meta_chain_incomplete: {exc}")
