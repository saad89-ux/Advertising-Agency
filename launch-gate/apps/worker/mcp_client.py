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
from typing import Any
import uuid

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


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
        """Return available MCP tools defined for a specific platform."""
        return [
            MCPToolDefinition(
                name=f"{platform}_dispatch_ad",
                description=f"Authenticate and publish verified ad creative payload to {platform.title()} Ads API.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "ad_id": {"type": "string"},
                        "headline": {"type": "string"},
                        "ad_text": {"type": "string"},
                        "destination_url": {"type": "string"},
                        "creative_url": {"type": "string"},
                    },
                    "required": ["ad_id", "headline", "destination_url"],
                },
            ),
            MCPToolDefinition(
                name=f"{platform}_track_performance",
                description=f"Query live real-time ROAS, impressions, CTR, and inbound conversions from {platform.title()}.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "platform_ad_id": {"type": "string"},
                    },
                    "required": ["platform_ad_id"],
                },
            ),
            MCPToolDefinition(
                name=f"{platform}_policy_audit",
                description=f"Retrieve live ad review status and platform rejection rationale from {platform.title()}.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "platform_ad_id": {"type": "string"},
                    },
                    "required": ["platform_ad_id"],
                },
            ),
        ]

    def call_tool(
        self,
        platform: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> MCPResponse:
        """Execute an MCP tool call over standard JSON-RPC 2.0 protocol.

        Returns success=False with 'connector_not_configured' when no active connector
        is configured for the platform.
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

        live_mcp_enabled = os.environ.get("META_MCP_ENABLE_LIVE", "false").strip().lower() in {
            "1", "true", "yes"
        }
        if live_mcp_enabled and self.meta_mcp_url and os.environ.get("META_MCP_ACCESS_TOKEN", "").strip():
            try:
                from apps.worker.meta_mcp_client import MetaMCPClient

                response = MetaMCPClient().call_tool_sync(
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
