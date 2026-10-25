"""Model Context Protocol (MCP) Client & Tool Dispatcher for Meta Ads.

Implements JSON-RPC 2.0 tool execution protocol for:
- Meta Ads Graph MCP

Standardizes Meta campaign deployment, live status polling, and policy violation
alerts via standard MCP tool contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
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
    status_code: int | None = None


class MCPPlatformDispatcher:
    """Manages Meta MCP tool execution for Facebook and Instagram placements."""

    def __init__(self, *, access_token: str | None = None) -> None:
        self.meta_mcp_url = os.environ.get("MCP_META_SERVER_URL", "").strip()
        self.access_token = access_token.strip() if isinstance(access_token, str) else ""

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

        if tool_name in META_CREATE_TOOLS and {"status", "publish_as_active"} & arguments.keys():
            return MCPResponse(
                success=False,
                platform=platform,
                tool_name=tool_name,
                result={},
                error="Meta create tools are paused-only and do not accept status or activation overrides.",
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
        account_id = str(arguments.get("ad_account_id") or "").strip().removeprefix("act_")
        access_token = self.access_token or os.environ.get("META_SYSTEM_USER_TOKEN", "").strip()
        if is_write and not account_id:
            return MCPResponse(False, platform, tool_name, {}, "ad_account_id is required from the authorized client workspace.", rpc_id)
        if live_mcp_enabled and self.meta_mcp_url and access_token:
            try:
                from apps.worker.meta_mcp_client import MetaMCPClient

                response = MetaMCPClient(
                    access_token=access_token,
                    enable_live=live_mcp_enabled,
                    allow_writes=writes_allowed,
                ).call_tool_sync(
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
                    status_code=response.status_code,
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
        campaign_objective: str = "OUTCOME_TRAFFIC",
        campaign_budget_strategy: str = "DAILY",
        campaign_lifetime_budget_minor_units: int | None = None,
        campaign_key: str | None = None,
        ad_set_key: str | None = None,
        ad_set_name: str | None = None,
        targeting_spec: dict[str, Any] | None = None,
        placements: list[str] | None = None,
        optimization_goal: str = "LINK_CLICKS",
        billing_event: str = "IMPRESSIONS",
        ad_set_daily_budget_minor_units: int | None = None,
        ad_set_lifetime_budget_minor_units: int | None = None,
        adset_schedule: dict[str, Any] | None = None,
        targeting_countries: list[str] | None = None,
        special_ad_category_countries: list[str] | None = None,
        existing_ids: dict[str, str] | None = None,
        persist_stage=None,
        persist_stage_status=None,
    ) -> MCPResponse:
        """Create or resume one deterministic chain using Meta's paused-only create tools.

        The discovered create contracts do not accept a status field and guarantee
        campaigns, ad sets, and ads are created paused. Publishing is a separate,
        explicitly authorized activation operation.
        """
        if platform not in {"facebook", "instagram"}:
            return MCPResponse(False, platform, "ads_create_campaign", {}, "unsupported_platform_meta_only")
        if not meta_live_writes_enabled():
            return MCPResponse(False, platform, "ads_create_campaign", {}, "real_platform_calls_disabled")
        budget_strategy = campaign_budget_strategy.upper()
        budget_is_valid = (
            budget_strategy == "DAILY" and daily_budget_minor_units > 0 and campaign_lifetime_budget_minor_units is None
        ) or (
            budget_strategy == "LIFETIME" and bool(campaign_lifetime_budget_minor_units) and daily_budget_minor_units in (None, 0)
        ) or (
            budget_strategy == "AD_SET_DAILY" and bool(ad_set_daily_budget_minor_units) and daily_budget_minor_units in (None, 0)
        ) or (
            budget_strategy == "AD_SET_LIFETIME" and bool(ad_set_lifetime_budget_minor_units) and daily_budget_minor_units in (None, 0)
        )
        if not all((ad_account_id, page_id, targeting_country, image_url)) or not budget_is_valid:
            return MCPResponse(False, platform, "ads_create_campaign", {}, "Required Meta launch details are missing.")
        if platform == "instagram" and not instagram_user_id:
            return MCPResponse(False, platform, "ads_create_creative", {}, "Instagram account ID is required for Instagram placement.")
        if not image_url.startswith("https://"):
            return MCPResponse(False, platform, "ads_create_creative", {}, "Meta requires a publicly accessible HTTPS creative image URL.")
        country_codes = [str(country).strip().upper() for country in (targeting_countries or [targeting_country])]
        if not country_codes or any(len(country) != 2 or not country.isalpha() for country in country_codes):
            return MCPResponse(False, platform, "ads_create_ad_set", {}, "Targeting requires valid two-letter country codes.")
        category_countries = [str(country).strip().upper() for country in (special_ad_category_countries or country_codes)]

        safe_name = re.sub(r"[^A-Za-z0-9 _-]+", "", campaign_name).strip()[:80] or "Launch Gate Campaign"
        suffix = f"{ad_id}-{platform}"
        campaign_key_digest = hashlib.sha256((campaign_key or suffix).encode("utf-8")).hexdigest()[:10]
        ad_set_key_digest = hashlib.sha256((ad_set_key or suffix).encode("utf-8")).hexdigest()[:10]
        names = {
            "campaign": f"{safe_name[:40]}-{campaign_key_digest}",
            "ad_set": (
                f"{(ad_set_name or 'Ad Set')[:45]}-{ad_set_key_digest}"
                if ad_set_key
                else f"{suffix}-adset"
            ),
            "creative": f"{suffix}-creative",
            "ad": f"{suffix}-ad",
        }
        ids = dict(existing_ids or {})
        active_stage: str | None = None
        active_lifecycle_status = "PENDING"
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

        def set_stage_status(stage: str, state: str, error: str | None = None) -> None:
            nonlocal active_stage, active_lifecycle_status
            if stage not in {"campaign", "ad_set"}:
                return
            active_stage = stage
            active_lifecycle_status = state
            if persist_stage_status:
                persist_stage_status(stage, state, error)

        def create_or_resume(stage: str, level: str, tool_name: str, arguments: dict[str, Any], keys: tuple[str, ...]) -> str:
            if ids.get(stage):
                set_stage_status(stage, "CREATED")
                return ids[stage]
            set_stage_status(stage, "RECONCILING")
            found = lookup_creative(names[stage]) if stage == "creative" else lookup_entity(level, names[stage])
            if found:
                ids[stage] = found
                if persist_stage:
                    persist_stage(stage, found)
                set_stage_status(stage, "CREATED")
                return found
            set_stage_status(stage, "CREATING")
            response = self.call_tool(platform, tool_name, arguments)
            if not response.success:
                error_message = response.error or f"Meta {tool_name} failed."
                uncertain = response.status_code in {408, 425, 500, 502, 503, 504} or any(
                    marker in error_message.casefold()
                    for marker in ("timeout", "timed out", "connection reset", "transport error")
                )
                set_stage_status(stage, "RECONCILING" if uncertain else "FAILED", error_message)
                raise RuntimeError(error_message)
            created_id = object_id(response.result, keys)
            if not created_id:
                set_stage_status(stage, "RECONCILING", "Meta returned success without an object ID.")
                raise RuntimeError(f"Meta {tool_name} returned no object ID.")
            ids[stage] = created_id
            if persist_stage:
                persist_stage(stage, created_id)
            set_stage_status(stage, "CREATED")
            return created_id

        context = {
            "client_conversation_id": conversation_id,
            "advertiser_request": advertiser_request,
            "advertiser_job": advertiser_job,
        }
        try:
            campaign_arguments = {
                **context,
                "ad_account_id": ad_account_id,
                "campaign_name": names["campaign"],
                "objective": campaign_objective,
                "buying_type": "AUCTION",
                "special_ad_categories": json.dumps([] if special_ad_category == "NONE" else [special_ad_category]),
                **({"special_ad_category_country": json.dumps(category_countries)} if special_ad_category != "NONE" else {}),
            }
            if budget_strategy == "DAILY":
                campaign_arguments["campaign_daily_budget"] = daily_budget_minor_units
                campaign_arguments["campaign_bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
            elif budget_strategy == "LIFETIME":
                campaign_arguments["campaign_lifetime_budget"] = campaign_lifetime_budget_minor_units
                campaign_arguments["campaign_bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
            campaign_id = create_or_resume("campaign", "campaign", "ads_create_campaign", campaign_arguments, ("campaign_id", "id"))

            targeting = dict(targeting_spec or {"geo_locations": {"countries": country_codes}})
            targeting.setdefault("geo_locations", {"countries": country_codes})
            if placements:
                allowed_placements = {"facebook", "instagram", "messenger", "audience_network"}
                normalized_placements = [str(value).strip().casefold() for value in placements]
                if any(value not in allowed_placements for value in normalized_placements):
                    raise ValueError("Ad-set placements contain an unsupported Meta placement.")
                targeting["publisher_platforms"] = normalized_placements
            if platform not in targeting.get("publisher_platforms", []) and targeting_spec is None:
                targeting["publisher_platforms"] = [platform]
            ad_set_arguments = {
                **context,
                "ad_account_id": ad_account_id,
                "campaign_id": campaign_id,
                "ad_set_name": names["ad_set"],
                "billing_event": billing_event,
                "optimization_goal": optimization_goal,
                "targeting": json.dumps(targeting),
                "destination_type": "WEBSITE",
            }
            if budget_strategy == "AD_SET_DAILY":
                ad_set_arguments["daily_budget"] = ad_set_daily_budget_minor_units
            elif budget_strategy == "AD_SET_LIFETIME":
                ad_set_arguments["lifetime_budget"] = ad_set_lifetime_budget_minor_units
            schedule_specs = (adset_schedule or {}).get("adset_schedule")
            if isinstance(schedule_specs, list) and schedule_specs:
                ad_set_arguments["adset_schedule"] = json.dumps(schedule_specs)
            if (adset_schedule or {}).get("start_time"):
                ad_set_arguments["start_time"] = adset_schedule["start_time"]
            if (adset_schedule or {}).get("end_time"):
                ad_set_arguments["end_time"] = adset_schedule["end_time"]
            ad_set_id = create_or_resume("ad_set", "adset", "ads_create_ad_set", ad_set_arguments, ("ad_set_id", "adset_id", "id"))

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
            return MCPResponse(True, platform, "ads_create_ad", {
                "platform_ad_id": meta_ad_id,
                "campaign_id": campaign_id,
                "ad_set_id": ad_set_id,
                "creative_id": creative_id,
                "status": "created_paused",
                "lifecycle_status": "CREATED",
                "client_conversation_id": conversation_id,
            })
        except Exception as exc:
            logger.exception("Meta paused creation chain failed for ad %s on %s", ad_id, platform)
            safe_error = " ".join(str(exc).split())[:500]
            if persist_stage_status and active_stage and active_lifecycle_status in {"CREATING", "RECONCILING"}:
                persist_stage_status(active_stage, active_lifecycle_status, safe_error)
            return MCPResponse(False, platform, "ads_create_ad", {
                "campaign_id": ids.get("campaign"),
                "ad_set_id": ids.get("ad_set"),
                "creative_id": ids.get("creative"),
                "platform_ad_id": ids.get("ad"),
                "status": "paused",
                "lifecycle_status": active_lifecycle_status,
                "failed_stage": active_stage,
            }, f"meta_chain_incomplete: {safe_error}")
