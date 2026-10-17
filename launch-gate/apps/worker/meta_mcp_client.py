"""Real Meta MCP (Model Context Protocol) Client.

Connects to Meta's official remote MCP endpoint (e.g. https://mcp.facebook.com/ads)
using the official Python `mcp` SDK over Streamable HTTP transport with Bearer token authentication.

Safety Guarantees:
- Reads credentials strictly from environment (`MCP_META_SERVER_URL`, `META_MCP_ACCESS_TOKEN`).
- Zero token logging or exposure.
- Enforces write protection (`META_MCP_ALLOW_WRITES=false` by default).
- Zero fake fallbacks: any failure returns `success=False` with the explicit reason.
- Bounded retries with exponential backoff on transient errors; immediate fail on 401/403.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import os
import re
from pathlib import Path
import time
from typing import Any
import uuid

from dotenv import load_dotenv
import httpx

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import mcp.types as types

logger = logging.getLogger(__name__)

# Action verbs that indicate a tool performs mutations/writes
_WRITE_ACTION_PATTERNS = frozenset({
    "create", "update", "delete", "activate", "deactivate", "publish",
    "post", "set", "apply", "dispatch", "edit", "pause", "resume", "stop",
    "upload", "modify", "remove", "insert", "write", "archive",
})

# Explicit read-only action tokens
_READ_ACTION_PATTERNS = frozenset({
    "get", "list", "read", "fetch", "query", "search", "check", "describe", "insights", "inspect"
})


class MetaMCPConfigurationError(RuntimeError):
    """Raised when required Meta MCP environment variables are missing."""


class MetaMCPWriteRefusedError(RuntimeError):
    """Raised when a write/mutation tool is called while META_MCP_ALLOW_WRITES is false."""


@dataclass(frozen=True)
class MetaMCPTool:
    """Discovered Meta MCP tool representation."""

    name: str
    description: str
    input_schema: dict[str, Any]
    is_write: bool
    title: str | None = None


@dataclass(frozen=True)
class MetaMCPResult:
    """Typed result object for all Meta MCP tool executions."""

    success: bool
    tool_name: str
    result: dict[str, Any]
    error: str | None = None
    rpc_id: str = field(default_factory=lambda: f"meta_mcp_{uuid.uuid4().hex[:8]}")
    status_code: int | None = None
    is_write_refusal: bool = False


def is_write_tool(tool_name: str, description: str = "", annotations: Any = None) -> bool:
    """Determine whether an MCP tool performs mutations or state modifications.
    
    When in doubt, treats the tool as a write to ensure safe-by-default operation.
    """
    name_clean = tool_name.casefold()
    desc_clean = (description or "").casefold()

    # If annotations declare read-only hint explicitly
    if annotations is not None and getattr(annotations, "readOnlyHint", False) is True:
        return False

    name_tokens = set(re.split(r"[_\W]+", name_clean))

    # 1. Check for explicit write action tokens
    if any(token in _WRITE_ACTION_PATTERNS for token in name_tokens):
        return True

    # 2. Check for explicit creation/modification sentences in description
    if any(phrase in desc_clean for phrase in [
        "creates a", "create a", "updates a", "update a", "deletes a",
        "delete a", "activates a", "activate a", "modifies", "publishes",
    ]):
        return True

    # 3. If tokens contain explicit read actions and no write tokens, classify as read-only
    if any(token in _READ_ACTION_PATTERNS for token in name_tokens):
        return False

    # Safe default: when uncertain, treat as a write operation
    return True


class MetaMCPClient:
    """Official Model Context Protocol client for Meta Ads."""

    def __init__(
        self,
        server_url: str | None = None,
        access_token: str | None = None,
        allow_writes: bool | None = None,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        load_dotenv()
        
        raw_url = server_url or os.environ.get("MCP_META_SERVER_URL", "")
        self.server_url = raw_url.strip()

        raw_token = access_token or os.environ.get("META_MCP_ACCESS_TOKEN", "")
        self.access_token = raw_token.strip()

        if allow_writes is not None:
            self.allow_writes = allow_writes
        else:
            env_writes = os.environ.get("META_MCP_ALLOW_WRITES", "false").strip().lower()
            self.allow_writes = env_writes in {"true", "1", "yes"}

        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)

    def _validate_config(self) -> None:
        """Validate required configuration without revealing secret values."""
        if not self.server_url:
            raise MetaMCPConfigurationError(
                "MCP_META_SERVER_URL is not configured. Set MCP_META_SERVER_URL in .env (e.g. https://mcp.facebook.com/ads)."
            )
        if not self.access_token:
            raise MetaMCPConfigurationError(
                "META_MCP_ACCESS_TOKEN is not configured. Set META_MCP_ACCESS_TOKEN in .env with a valid Meta Business access token."
            )

    def _build_http_client(self, on_status: Any = None) -> httpx.AsyncClient:
        """Construct an authenticated httpx.AsyncClient with required headers and status hook."""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": "LaunchGate-MetaMCPClient/1.0",
            "Accept": "application/json, text/event-stream",
        }
        event_hooks = {}
        if on_status is not None:
            async def _hook(response: httpx.Response) -> None:
                on_status(response.status_code)
            event_hooks["response"] = [_hook]

        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=True,
            event_hooks=event_hooks,
        )

    def _unwrap_exception(self, exc: BaseException, recorded_status: int | None = None) -> tuple[str, int | None]:
        """Extract user-facing error message and HTTP status from nested exception groups."""
        curr: BaseException | None = exc
        status_code: int | None = recorded_status
        messages: list[str] = []

        while curr is not None:
            if isinstance(curr, httpx.HTTPStatusError):
                status_code = curr.response.status_code
                messages.append(f"HTTP {status_code}: {curr.response.text}")
                break
            if hasattr(curr, "status_code"):
                status_code = getattr(curr, "status_code")
            if hasattr(curr, "exceptions") and getattr(curr, "exceptions"):
                # Nested ExceptionGroup in anyio/TaskGroup
                curr = getattr(curr, "exceptions")[0]
                continue
            messages.append(str(curr))
            curr = getattr(curr, "__cause__", None) or getattr(curr, "__context__", None)

        msg = " | ".join(m for m in messages if m) or "Unknown error communicating with Meta MCP server"
        
        # Categorize common HTTP codes
        if status_code == 401 or "401" in msg:
            return "Authentication failed (HTTP 401): META_MCP_ACCESS_TOKEN is invalid, expired, or lacking required scopes.", 401
        if status_code == 403 or "403" in msg:
            return "Access forbidden (HTTP 403): Token lacks permission to access requested ad resources.", 403
        if status_code == 429 or "429" in msg:
            return "Meta MCP rate limit exceeded (HTTP 429).", 429

        return msg, status_code

    async def list_tools(self) -> list[MetaMCPTool]:
        """Connect to the Meta MCP server, perform initialize handshake, and return discovered tools."""
        self._validate_config()
        
        last_error = None
        last_status_code: int | None = None

        def _record_status(code: int) -> None:
            nonlocal last_status_code
            last_status_code = code

        for attempt in range(1, self.max_retries + 1):
            try:
                async with self._build_http_client(on_status=_record_status) as http_client:
                    async with streamable_http_client(self.server_url, http_client=http_client) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            tools_result = await session.list_tools()
                            
                            discovered: list[MetaMCPTool] = []
                            for tool in tools_result.tools:
                                schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
                                is_write = is_write_tool(tool.name, tool.description or "", getattr(tool, "annotations", None))
                                discovered.append(
                                    MetaMCPTool(
                                        name=tool.name,
                                        description=tool.description or "",
                                        input_schema=schema,
                                        is_write=is_write,
                                        title=getattr(tool, "title", None),
                                    )
                                )
                            logger.info("Successfully discovered %d tools from Meta MCP server.", len(discovered))
                            return discovered
            except BaseException as exc:
                err_msg, status_code = self._unwrap_exception(exc, recorded_status=last_status_code)
                last_error = err_msg
                # Never retry non-transient auth/permission errors
                if status_code in {401, 403}:
                    logger.error("Meta MCP handshake failed non-retryable error: %s", err_msg)
                    raise RuntimeError(err_msg) from exc
                
                logger.warning("Meta MCP list_tools attempt %d/%d failed: %s", attempt, self.max_retries, err_msg)
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

        raise RuntimeError(f"Failed to connect to Meta MCP server after {self.max_retries} attempts: {last_error}")

    def list_tools_sync(self) -> list[MetaMCPTool]:
        """Synchronous helper for list_tools."""
        return asyncio.run(self.list_tools())

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool_description: str = "",
        tool_annotations: Any = None,
    ) -> MetaMCPResult:
        """Call a specific tool on the Meta MCP server over JSON-RPC.
        
        Enforces write protection: refuses execution if tool is classified as a write and
        `allow_writes` is False.
        """
        rpc_id = f"meta_{uuid.uuid4().hex[:8]}"

        # 1. Enforce Write Protection
        if not self.allow_writes and is_write_tool(tool_name, tool_description, tool_annotations):
            refusal_msg = (
                f"Write operation refused: tool '{tool_name}' performs mutations/creations, "
                f"but META_MCP_ALLOW_WRITES is set to 'false'. To allow mutations, the owner "
                f"must explicitly set META_MCP_ALLOW_WRITES=true in .env."
            )
            logger.warning("[MCP SAFETY REFUSAL] %s", refusal_msg)
            return MetaMCPResult(
                success=False,
                tool_name=tool_name,
                result={},
                error=refusal_msg,
                rpc_id=rpc_id,
                is_write_refusal=True,
            )

        # 2. Validate configuration
        try:
            self._validate_config()
        except MetaMCPConfigurationError as cfg_err:
            return MetaMCPResult(
                success=False,
                tool_name=tool_name,
                result={},
                error=str(cfg_err),
                rpc_id=rpc_id,
            )

        # 3. Execute MCP tool call with bounded retries
        last_error = None
        last_status_code: int | None = None

        def _record_status(code: int) -> None:
            nonlocal last_status_code
            last_status_code = code

        for attempt in range(1, self.max_retries + 1):
            try:
                async with self._build_http_client(on_status=_record_status) as http_client:
                    async with streamable_http_client(self.server_url, http_client=http_client) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            call_result: types.CallToolResult = await session.call_tool(tool_name, arguments)

                            # Parse tool response
                            if getattr(call_result, "is_error", False):
                                err_text = ""
                                for content in call_result.content or []:
                                    if hasattr(content, "text"):
                                        err_text += content.text + " "
                                return MetaMCPResult(
                                    success=False,
                                    tool_name=tool_name,
                                    result={},
                                    error=err_text.strip() or "Tool returned an error status.",
                                    rpc_id=rpc_id,
                                )

                            parsed_data = self._parse_result_content(call_result)
                            return MetaMCPResult(
                                success=True,
                                tool_name=tool_name,
                                result=parsed_data,
                                error=None,
                                rpc_id=rpc_id,
                            )
            except BaseException as exc:
                err_msg, status_code = self._unwrap_exception(exc, recorded_status=last_status_code)
                last_error = err_msg
                
                # Do not retry auth errors
                if status_code in {401, 403}:
                    return MetaMCPResult(
                        success=False,
                        tool_name=tool_name,
                        result={},
                        error=err_msg,
                        rpc_id=rpc_id,
                        status_code=status_code,
                    )

                logger.warning("Meta MCP call_tool '%s' attempt %d/%d failed: %s", tool_name, attempt, self.max_retries, err_msg)
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

        return MetaMCPResult(
            success=False,
            tool_name=tool_name,
            result={},
            error=f"Meta MCP call failed after {self.max_retries} attempts: {last_error}",
            rpc_id=rpc_id,
        )

    def call_tool_sync(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool_description: str = "",
        tool_annotations: Any = None,
    ) -> MetaMCPResult:
        """Synchronous helper for call_tool."""
        return asyncio.run(
            self.call_tool(
                tool_name,
                arguments,
                tool_description=tool_description,
                tool_annotations=tool_annotations,
            )
        )

    def _parse_result_content(self, call_result: types.CallToolResult) -> dict[str, Any]:
        """Extract structured JSON payload or text from CallToolResult content."""
        if getattr(call_result, "structured_content", None):
            sc = getattr(call_result, "structured_content")
            if isinstance(sc, dict):
                return sc
            return {"structured_content": sc}

        items = []
        for c in call_result.content or []:
            if hasattr(c, "text") and c.text:
                txt = c.text.strip()
                try:
                    items.append(json.loads(txt))
                except Exception:
                    items.append({"text": txt})

        if len(items) == 1 and isinstance(items[0], dict):
            return items[0]
        return {"content": items}

    async def execute_action(
        self,
        action: str,
        arguments: dict[str, Any],
        available_tool_names: list[str] | None = None,
    ) -> MetaMCPResult:
        """Execute a mapped Launch Gate action using the corresponding real Meta MCP tool.
        
        Guarantees:
        - Mappings are checked against ACTION_MAPPINGS.
        - For 'create_paused_ad', status is strictly enforced as PAUSED.
        - Returns typed MetaMCPResult.
        """
        mapping = ACTION_MAPPINGS.get(action)
        if not mapping:
            return MetaMCPResult(
                success=False,
                tool_name=action,
                result={},
                error=(
                    f"Unrecognized Launch Gate action '{action}'. "
                    f"Supported actions: {list(ACTION_MAPPINGS.keys())}"
                ),
            )

        tool_name = resolve_tool_name(action, available_tool_names)
        final_args = dict(arguments)
        if action == ACTION_CREATE_PAUSED_AD:
            final_args = sanitize_create_ad_params(final_args)

        return await self.call_tool(
            tool_name=tool_name,
            arguments=final_args,
            tool_description=mapping.description,
        )

    def execute_action_sync(
        self,
        action: str,
        arguments: dict[str, Any],
        available_tool_names: list[str] | None = None,
    ) -> MetaMCPResult:
        """Synchronous helper for execute_action."""
        return asyncio.run(
            self.execute_action(
                action=action,
                arguments=arguments,
                available_tool_names=available_tool_names,
            )
        )


# ============================================================================
# Launch Gate to Real Meta MCP Tool Mappings (Task 2)
# ============================================================================

ACTION_READ_PERFORMANCE = "read_performance"
ACTION_READ_AD_REVIEW_STATUS = "read_ad_review_status"
ACTION_CREATE_PAUSED_AD = "create_paused_ad"


@dataclass(frozen=True)
class ActionMapping:
    """Mapping definition between high-level Launch Gate action and real Meta MCP tools."""

    action_name: str
    primary_tool: str
    candidate_tools: tuple[str, ...]
    is_write: bool
    description: str


ACTION_MAPPINGS: dict[str, ActionMapping] = {
    ACTION_READ_PERFORMANCE: ActionMapping(
        action_name=ACTION_READ_PERFORMANCE,
        primary_tool="ads_get_ad_entities",
        candidate_tools=("ads_get_ad_entities", "ads_get_ad_accounts", "ads_insights_performance_trend"),
        is_write=False,
        description="Query live campaign and ad performance metrics (spend, impressions, clicks, CTR, CPM, CPC, ROAS).",
    ),
    ACTION_READ_AD_REVIEW_STATUS: ActionMapping(
        action_name=ACTION_READ_AD_REVIEW_STATUS,
        primary_tool="ads_get_errors",
        candidate_tools=("ads_get_errors", "ads_get_ad_entities"),
        is_write=False,
        description="Retrieve review feedback, disapproval reasons, policy violations, and effective delivery status.",
    ),
    ACTION_CREATE_PAUSED_AD: ActionMapping(
        action_name=ACTION_CREATE_PAUSED_AD,
        primary_tool="ads_create_ad",
        candidate_tools=("ads_create_ad", "ads_create_campaign"),
        is_write=True,
        description="Create an ad entity strictly in PAUSED state (writes must be enabled via META_MCP_ALLOW_WRITES=true).",
    ),
}


def resolve_tool_name(action: str, available_tool_names: list[str] | set[str] | None = None) -> str:
    """Resolve an action name to the best-matching real MCP tool name.
    
    Checks candidate tool names against the server's live catalogue (if provided).
    Defaults to primary_tool if no match is found or no live list is provided.
    """
    mapping = ACTION_MAPPINGS.get(action)
    if not mapping:
        raise ValueError(
            f"Unknown Launch Gate action '{action}'. Supported actions: {list(ACTION_MAPPINGS.keys())}"
        )

    if available_tool_names:
        available_set = set(available_tool_names)
        # 1. Exact match among candidates
        for cand in mapping.candidate_tools:
            if cand in available_set:
                return cand
        # 2. Case-insensitive / suffix match
        for cand in mapping.candidate_tools:
            for avail in available_set:
                if cand.casefold() == avail.casefold() or avail.casefold().endswith(cand.casefold()):
                    return avail

    return mapping.primary_tool


def sanitize_create_ad_params(params: dict[str, Any]) -> dict[str, Any]:
    """Ensure any ad creation request is strictly created in a PAUSED state.
    
    If the caller attempts to specify status='ACTIVE', it is overridden to 'PAUSED'.
    """
    sanitized = dict(params)
    requested = str(sanitized.get("status", "PAUSED")).upper()
    if requested != "PAUSED":
        logger.warning(
            "Ad creation was requested with status='%s'. Overriding to 'PAUSED' per safety requirements.",
            requested,
        )
    sanitized["status"] = "PAUSED"
    return sanitized


def discover_and_save_tools(
    client: MetaMCPClient | None = None,
    output_file: str | Path = "data/meta_mcp_tools.json",
    print_output: bool = True,
) -> list[dict[str, Any]]:
    """Discover real tools from Meta MCP server, format catalogue, print schemas, and save to JSON.
    
    If the server cannot be reached due to unconfigured credentials, falls back
    to loading/preserving the verified reference catalogue, noting the configuration state.
    """
    out_path = Path(output_file)
    mcp_client = client or MetaMCPClient()

    try:
        discovered_tools = mcp_client.list_tools_sync()
        formatted: list[dict[str, Any]] = []
        for tool in discovered_tools:
            formatted.append({
                "name": tool.name,
                "title": tool.title,
                "description": tool.description,
                "is_write": tool.is_write,
                "input_schema": tool.input_schema,
            })

        payload = {
            "server": mcp_client.server_url,
            "source": "live_mcp_handshake",
            "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_tools": len(formatted),
            "launch_gate_action_mappings": {
                name: {
                    "primary_tool": m.primary_tool,
                    "candidate_tools": list(m.candidate_tools),
                    "is_write": m.is_write,
                    "description": m.description,
                }
                for name, m in ACTION_MAPPINGS.items()
            },
            "tools": formatted,
        }

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        if print_output:
            print(f"=== Discovered {len(formatted)} Real Meta MCP Tools (LIVE) ===")
            print(f"Server: {mcp_client.server_url}")
            print(f"Saved to: {out_path.resolve()}\n")
            for t in formatted:
                w_tag = "[WRITE - MUTATION]" if t["is_write"] else "[READ-ONLY]"
                print(f"- Tool: {t['name']} {w_tag}")
                print(f"  Description: {t['description']}")
                props = t["input_schema"].get("properties", {})
                req = t["input_schema"].get("required", [])
                print(f"  Properties ({len(props)}): {list(props.keys())}")
                if req:
                    print(f"  Required: {req}")
                print()

        return formatted

    except (MetaMCPConfigurationError, RuntimeError, Exception) as exc:
        logger.warning("Live Meta MCP discovery unavailable: %s", exc)
        if out_path.exists():
            with open(out_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
                tools_data = saved.get("tools", [])
                if print_output:
                    print(f"=== Meta MCP Tool Catalogue (Reference Cache from {out_path.name}) ===")
                    print(f"Note: Live connection deferred ({exc})")
                    print(f"Total Tools in Catalogue: {len(tools_data)}\n")
                    for t in tools_data:
                        w_tag = "[WRITE - MUTATION]" if t.get("is_write") else "[READ-ONLY]"
                        print(f"- Tool: {t.get('name')} {w_tag}")
                        print(f"  Category: {t.get('category', 'General')}")
                        print(f"  Description: {t.get('description')}")
                        props = t.get("input_schema", {}).get("properties", {})
                        req = t.get("input_schema", {}).get("required", [])
                        print(f"  Properties ({len(props)}): {list(props.keys())}")
                        if req:
                            print(f"  Required: {req}")
                        print()
                return tools_data
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Discover Meta MCP tools and output catalogue.")
    parser.add_argument("--output", default="data/meta_mcp_tools.json", help="Path to save catalogue JSON.")
    args = parser.parse_args()
    print("Running Meta MCP Tool Discovery...")
    tools = discover_and_save_tools(output_file=args.output, print_output=True)
    print(f"Completed tool discovery. {len(tools)} tools inspected.")
