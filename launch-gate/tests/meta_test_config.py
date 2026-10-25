"""Explicit environment gates for optional Meta integration tests."""

from __future__ import annotations

import os


class MetaTestConfig:
    RUN_LIVE = os.environ.get("RUN_LIVE_META_TESTS", "false").strip().lower() == "true"
    TEST_CLIENT_ID = os.environ.get("META_SANDBOX_CLIENT_ID", "").strip()
    TEST_AD_ACCOUNT_ID = os.environ.get("META_SANDBOX_AD_ACCOUNT_ID", "").strip().removeprefix("act_")

    @classmethod
    def require_read_only_live_configuration(cls) -> None:
        if not cls.RUN_LIVE:
            raise RuntimeError("Set RUN_LIVE_META_TESTS=true to run live Meta read-only tests.")
        if not cls.TEST_CLIENT_ID:
            raise RuntimeError("META_SANDBOX_CLIENT_ID must identify the dedicated sandbox client workspace.")
        if not cls.TEST_AD_ACCOUNT_ID:
            raise RuntimeError("META_SANDBOX_AD_ACCOUNT_ID must identify the sandbox ad account mapped to that client.")
        if os.environ.get("META_MCP_ENABLE_LIVE", "false").strip().lower() not in {"true", "1", "yes"}:
            raise RuntimeError("META_MCP_ENABLE_LIVE=true is required for Meta read-only integration tests.")
        if os.environ.get("META_MCP_ALLOW_WRITES", "false").strip().lower() in {"true", "1", "yes"}:
            raise RuntimeError("Meta integration tests are read-only; META_MCP_ALLOW_WRITES must remain false.")
        if not os.environ.get("META_SYSTEM_USER_TOKEN", "").strip():
            raise RuntimeError("META_SYSTEM_USER_TOKEN is required for Meta read-only integration tests.")
