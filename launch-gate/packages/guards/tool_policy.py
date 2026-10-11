"""Code-owned tool allowlists and client-scoped rule selection."""

from __future__ import annotations

from typing import Any


STEP_TOOL_ALLOWLIST = {
    "mechanical": frozenset({"read_ad"}),
    "page_check": frozenset({"open_url_readonly", "save_screenshot"}),
    "wording_check": frozenset({"retrieve_client_rules"}),
    "verdict": frozenset(),
}
FORBIDDEN_TOOLS = frozenset({"approve", "send", "edit_ad", "change_verdict"})


class ToolNotAllowed(PermissionError):
    """Raised when code attempts a tool outside the current step's allowlist."""


def assert_tool_allowed(step: str, tool: str) -> None:
    if tool in FORBIDDEN_TOOLS or tool not in STEP_TOOL_ALLOWLIST.get(step, frozenset()):
        raise ToolNotAllowed("tool is not allowed for this step")


def _collect_rule_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        output: set[str] = set()
        for nested in value.values():
            output.update(_collect_rule_ids(nested))
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        output = set()
        for nested in value:
            output.update(_collect_rule_ids(nested))
        return output
    return set()


def restrict_rules_to_client(rule_index: dict[str, Any], client_id: str) -> frozenset[str]:
    if client_id not in rule_index:
        raise ValueError("unknown client")
    return frozenset(_collect_rule_ids(rule_index[client_id]))
