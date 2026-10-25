"""Read-only Meta structure and metric synchronization through the MCP connector."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
import re
import secrets
import string
import time
from typing import Any, Callable, Iterator
from uuid import UUID, uuid4

from apps.api.clients import resolve_client_meta_account
from apps.api.database import get_session
from apps.worker.mcp_client import MCPPlatformDispatcher, MCPResponse

logger = logging.getLogger(__name__)

SYNC_TYPES = {"INITIAL_IMPORT", "METRIC_REFRESH"}
LEVELS = (("campaign", "CAMPAIGN"), ("adset", "ADSET"), ("ad", "AD"))
STRUCTURAL_FIELDS = ("id", "name", "effective_status", "objective", "campaign_id", "adset_id")
METRIC_FIELDS = (
    "amount_spent",
    "impressions",
    "clicks",
    "omni_purchase",
    "offsite_conversion_fb_pixel_purchase",
)
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "error code 4",
    "error code 17",
    "error code 32",
    "error code 613",
    "error code 80004",
    "too many requests",
    "request limit",
    "throttl",
    "http 429",
    "429",
)


def _response_rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    for key in ("data", "ad_accounts", "ad_entities", "entities", "results", "campaigns", "adsets", "ads"):
        rows = result.get(key)
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    return []


def _next_cursor(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    pagination = result.get("pagination")
    cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
    if not cursor:
        cursor = result.get("next_cursor")
    return cursor if isinstance(cursor, str) and cursor else None


def _iter_response_pages(
    dispatcher: Any,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    sleep: Callable[[float], None],
    max_attempts: int,
    max_pages: int,
) -> Iterator[dict[str, Any]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        page_arguments = dict(arguments)
        if cursor:
            page_arguments["cursor"] = cursor
        response = call_read_with_backoff(
            dispatcher, tool_name, page_arguments, sleep=sleep, max_attempts=max_attempts
        )
        if not response.success:
            raise RuntimeError(response.error or f"Meta {tool_name} request failed.")
        result = response.result if isinstance(response.result, dict) else {}
        next_cursor = _next_cursor(result)
        if next_cursor and (next_cursor == cursor or next_cursor in seen_cursors):
            raise RuntimeError(f"Meta {tool_name} returned a repeated pagination cursor.")
        yield result
        if not next_cursor:
            return
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise RuntimeError(f"Meta {tool_name} pagination exceeded {max_pages} pages.")


def _is_retryable(response: MCPResponse) -> bool:
    if response.status_code == 429 or response.status_code in {408, 425, 500, 502, 503, 504}:
        return True
    message = (response.error or "").casefold()
    has_rate_limit_code = re.search(r"error code\s*[:=]?\s*(4|17|32|613|80004)\b", message) is not None
    return (
        any(marker in message for marker in _RATE_LIMIT_MARKERS if not marker.startswith("error code"))
        or has_rate_limit_code
        or "timeout" in message
        or "temporarily unavailable" in message
    )


def call_read_with_backoff(
    dispatcher: Any,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    platform: str = "facebook",
    max_attempts: int = 6,
    base_delay_seconds: float = 2.0,
    max_delay_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> MCPResponse:
    """Retry transient/rate-limit read errors with bounded exponential backoff."""
    if max_attempts < 1 or base_delay_seconds < 0 or max_delay_seconds < 0:
        raise ValueError("Meta retry attempts must be positive and delays must be non-negative.")
    for attempt in range(max_attempts):
        response = dispatcher.call_tool(platform, tool_name, arguments)
        if response.success or not _is_retryable(response) or attempt + 1 >= max_attempts:
            return response
        delay = min(base_delay_seconds * (2 ** attempt), max_delay_seconds)
        logger.warning("Meta read rate-limited; retry %s/%s after %.1fs", attempt + 1, max_attempts - 1, delay)
        sleep(delay)
    raise RuntimeError("unreachable Meta read retry state")


def _verified_fields(dispatcher: Any, conversation_id: str, sleep: Callable[[float], None], max_attempts: int) -> dict[str, set[str]]:
    requested = list(dict.fromkeys((*STRUCTURAL_FIELDS, *METRIC_FIELDS)))
    response = call_read_with_backoff(dispatcher, "ads_get_field_context", {
        "field_names": requested,
        "client_conversation_id": conversation_id,
        "advertiser_request": "Verify supported fields for a read-only workspace synchronization",
        "advertiser_job": "report_measure_or_look_up",
    }, sleep=sleep, max_attempts=max_attempts)
    if not response.success:
        raise RuntimeError(response.error or "Could not verify Meta fields for sync.")
    payload = response.result if isinstance(response.result, dict) else {}
    fields = payload.get("fields", payload.get("data", []))
    result = {level: {"id", "name"} for level, _ in LEVELS}
    if isinstance(fields, dict):
        fields = [dict(value, field_name=key) if isinstance(value, dict) else {"field_name": key, "levels": []} for key, value in fields.items()]
    if isinstance(fields, list):
        for item in fields:
            if not isinstance(item, dict):
                continue
            name = item.get("field_name") or item.get("name") or item.get("canonical_name")
            levels = item.get("levels", [])
            if not isinstance(name, str) or not isinstance(levels, list):
                continue
            for level in levels:
                normalized = str(level).casefold()
                if normalized in result:
                    result[normalized].add(name)
    required_parent_fields = {"adset": {"campaign_id"}, "ad": {"campaign_id", "adset_id"}}
    for level, required in required_parent_fields.items():
        missing = required - result[level]
        if missing:
            raise RuntimeError(f"Meta field context did not verify parent linkage fields for {level}: {', '.join(sorted(missing))}.")
    # The MCP catalog's field-context result can omit common system fields. The
    # sync only sends fields listed as supported; IDs/names remain default fields.
    return result


def _available_fields(level: str, supported: dict[str, set[str]]) -> list[str]:
    fields = ["id", "name"]
    aliases = {
        "campaign": ("effective_status", "objective", *METRIC_FIELDS),
        "adset": ("effective_status", "campaign_id", *METRIC_FIELDS),
        "ad": ("effective_status", "campaign_id", "adset_id", *METRIC_FIELDS),
    }
    allowed = supported.get(level, set())
    fields.extend(field for field in aliases[level] if field in allowed)
    return fields


def _call_entity_pages(
    dispatcher: Any,
    *,
    account_id: str,
    level: str,
    fields: list[str],
    conversation_id: str,
    time_range: dict[str, str] | None,
    sleep: Callable[[float], None],
    max_attempts: int,
    max_pages: int,
) -> Iterator[list[dict[str, Any]]]:
    stable_args: dict[str, Any] = {
        "ad_account_id": account_id,
        "level": level,
        "fields": fields,
        "limit": 100,
        "client_conversation_id": conversation_id,
        "advertiser_request": "Synchronize Meta account structure and metrics read-only",
        "advertiser_job": "report_measure_or_look_up",
    }
    if time_range:
        stable_args["time_range"] = json.dumps(time_range)
    for result in _iter_response_pages(
        dispatcher, "ads_get_ad_entities", stable_args, sleep=sleep,
        max_attempts=max_attempts, max_pages=max_pages,
    ):
        yield _response_rows(result)


def _conversions(obj: dict[str, Any]) -> float:
    for field in ("omni_purchase", "offsite_conversion_fb_pixel_purchase", "purchase"):
        if obj.get(field) not in (None, ""):
            try:
                return float(obj[field])
            except (TypeError, ValueError):
                raise ValueError("Meta returned an invalid conversion metric.")
    actions = obj.get("actions")
    if not isinstance(actions, list):
        return 0.0
    for action_type in ("omni_purchase", "offsite_conversion.fb_pixel_purchase", "purchase"):
        total = 0.0
        found = False
        for action in actions:
            if not isinstance(action, dict) or action.get("action_type") != action_type:
                continue
            found = True
            try:
                total += float(action.get("value", 0))
            except (TypeError, ValueError):
                raise ValueError("Meta returned an invalid conversion metric.")
        if found:
            return total
    return 0.0


def _upsert_object(connection: Any, workspace_id: str, sync_id: UUID, object_type: str, obj: dict[str, Any]) -> bool:
    meta_id = str(obj.get("id") or "").strip()
    if not meta_id:
        return False
    parent = obj.get("campaign_id") if object_type == "ADSET" else obj.get("adset_id") if object_type == "AD" else None
    configuration = {key: value for key, value in obj.items() if key not in {"id", "name", "effective_status", "objective", "campaign_id", "adset_id"}}
    connection.execute(
        """INSERT INTO meta_synced_objects
           (workspace_id, meta_object_id, object_type, parent_meta_object_id, name, effective_status,
            objective, configuration, last_sync_id, synced_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,NOW())
           ON CONFLICT (workspace_id, meta_object_id) DO UPDATE SET
           object_type=EXCLUDED.object_type, parent_meta_object_id=EXCLUDED.parent_meta_object_id,
           name=EXCLUDED.name, effective_status=EXCLUDED.effective_status,
           objective=EXCLUDED.objective, configuration=EXCLUDED.configuration,
           last_sync_id=EXCLUDED.last_sync_id, synced_at=NOW()""",
        (workspace_id, meta_id, object_type, parent, obj.get("name"), obj.get("effective_status"),
         obj.get("objective") if object_type == "CAMPAIGN" else None,
         json.dumps(configuration, sort_keys=True), sync_id),
    )
    return True


def _upsert_metrics(
    connection: Any,
    workspace_id: str,
    object_type: str,
    obj: dict[str, Any],
    start: date,
    end: date,
) -> bool:
    meta_id = str(obj.get("id") or "").strip()
    if not meta_id or not any(key in obj for key in (*METRIC_FIELDS, "actions")):
        return False
    try:
        spend = float(obj.get("amount_spent") or obj.get("spend") or 0)
        impressions = int(float(obj.get("impressions") or 0))
        clicks = int(float(obj.get("clicks") or 0))
    except (TypeError, ValueError):
        raise ValueError(f"Meta returned invalid metric values for {object_type} {meta_id}.")
    conversions = _conversions(obj)
    connection.execute(
        """INSERT INTO meta_performance_metrics
           (workspace_id, meta_object_id, object_type, spend, impressions, clicks, conversions,
            reporting_start, reporting_end, source_attribution, synced_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'META_INSIGHTS_API',NOW())
           ON CONFLICT (workspace_id, meta_object_id, object_type, reporting_start, reporting_end, source_attribution)
           DO UPDATE SET spend=EXCLUDED.spend, impressions=EXCLUDED.impressions, clicks=EXCLUDED.clicks,
           conversions=EXCLUDED.conversions, synced_at=NOW()""",
        (workspace_id, meta_id, object_type, spend, impressions, clicks, conversions, start, end),
    )
    return True


def create_sync_log(connection: Any, workspace_id: str, sync_type: str) -> UUID:
    if sync_type not in SYNC_TYPES:
        raise ValueError("sync_type must be INITIAL_IMPORT or METRIC_REFRESH")
    row = connection.execute(
        "INSERT INTO meta_sync_logs(workspace_id,sync_type,status) VALUES (%s,%s,'RUNNING') RETURNING id",
        (workspace_id, sync_type),
    ).fetchone()
    return row[0]


def execute_workspace_sync(
    workspace_id: str,
    sync_id: UUID,
    *,
    sync_type: str = "INITIAL_IMPORT",
    lookback_days: int = 7,
    dispatcher: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 6,
    max_pages: int = 100,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Synchronize one workspace's Meta structure and recent metrics through read-only MCP calls."""
    synced_objects = 0
    metric_snapshots = 0
    try:
        if sync_type not in SYNC_TYPES:
            raise ValueError("sync_type must be INITIAL_IMPORT or METRIC_REFRESH")
        if lookback_days < 1 or lookback_days > 90:
            raise ValueError("lookback_days must be between 1 and 90")
        if max_attempts < 1 or max_pages < 1:
            raise ValueError("Meta retry attempts and page limits must be positive.")
        if os.environ.get("META_MCP_ENABLE_LIVE", "false").strip().casefold() not in {"true", "1", "yes"}:
            raise RuntimeError("META_MCP_ENABLE_LIVE must be enabled for Meta synchronization reads.")
        token = os.environ.get("META_SYSTEM_USER_TOKEN", "").strip()
        if not token:
            raise RuntimeError("META_SYSTEM_USER_TOKEN is not configured.")
        dispatcher = dispatcher or MCPPlatformDispatcher(access_token=token)
        conversation_id = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
        end_date = now().date()
        start_date = end_date - timedelta(days=lookback_days - 1)
        time_range = {"since": start_date.isoformat(), "until": end_date.isoformat()}
        with get_session() as connection:
            account_id = resolve_client_meta_account(connection, workspace_id)
            account_rows: dict[str, dict[str, Any]] = {}
            account_arguments = {
                "limit": 50,
                "client_conversation_id": conversation_id,
                "advertiser_request": "Synchronize this workspace's Meta data read-only",
                "advertiser_job": "report_measure_or_look_up",
            }
            for result in _iter_response_pages(
                dispatcher,
                "ads_get_ad_accounts",
                account_arguments,
                sleep=sleep,
                max_attempts=max_attempts,
                max_pages=max_pages,
            ):
                accounts = _response_rows(result)
                for row in accounts:
                    returned_id = str(row.get("ad_account_id", "")).removeprefix("act_")
                    if returned_id:
                        account_rows[returned_id] = row
                if account_id in account_rows:
                    break
            if account_id not in account_rows:
                raise PermissionError("Configured workspace ad account is not accessible to the agency System User.")
            if account_rows[account_id].get("is_ads_mcp_enabled") is False:
                raise PermissionError("Meta Ads MCP is not enabled for the configured workspace account.")
            if account_rows[account_id].get("is_queryable") is False:
                raise PermissionError(
                    str(account_rows[account_id].get("not_queryable_reason") or "Meta reports that the workspace account is not queryable.")
                )

            supported = _verified_fields(dispatcher, conversation_id, sleep, max_attempts)
            existing_by_level: dict[str, list[str]] = {}
            if sync_type == "METRIC_REFRESH":
                rows = connection.execute(
                    "SELECT meta_object_id, object_type FROM meta_synced_objects WHERE workspace_id=%s ORDER BY object_type, meta_object_id",
                    (workspace_id,),
                ).fetchall()
                for meta_id, object_type in rows:
                    level = {"CAMPAIGN": "campaign", "ADSET": "adset", "AD": "ad"}.get(object_type)
                    if level:
                        existing_by_level.setdefault(level, []).append(str(meta_id))

            for level, object_type in LEVELS:
                fields = _available_fields(level, supported)
                if sync_type == "METRIC_REFRESH":
                    ids = existing_by_level.get(level, [])
                    for offset in range(0, len(ids), 1000):
                        arguments = {
                            "ad_account_id": account_id,
                            "level": level,
                            "object_ids": ids[offset:offset + 1000],
                            "fields": fields,
                            "time_range": json.dumps(time_range),
                            "client_conversation_id": conversation_id,
                            "advertiser_request": "Refresh Meta workspace performance snapshots read-only",
                            "advertiser_job": "report_measure_or_look_up",
                        }
                        for result in _iter_response_pages(
                            dispatcher,
                            "ads_get_ad_entities",
                            arguments,
                            sleep=sleep,
                            max_attempts=max_attempts,
                            max_pages=max_pages,
                        ):
                            page_snapshots = 0
                            for obj in _response_rows(result):
                                if _upsert_metrics(connection, workspace_id, object_type, obj, start_date, end_date):
                                    page_snapshots += 1
                            _persist_sync_progress(
                                connection, sync_id, sync_type, synced_objects, metric_snapshots + page_snapshots
                            )
                            metric_snapshots += page_snapshots
                else:
                    rows = _call_entity_pages(
                        dispatcher,
                        account_id=account_id,
                        level=level,
                        fields=fields,
                        conversation_id=conversation_id,
                        time_range=time_range,
                        sleep=sleep,
                        max_attempts=max_attempts,
                        max_pages=max_pages,
                    )
                    for page in rows:
                        page_objects = 0
                        page_snapshots = 0
                        for obj in page:
                            if _upsert_object(connection, workspace_id, sync_id, object_type, obj):
                                page_objects += 1
                            if _upsert_metrics(connection, workspace_id, object_type, obj, start_date, end_date):
                                page_snapshots += 1
                        _persist_sync_progress(
                            connection,
                            sync_id,
                            sync_type,
                            synced_objects + page_objects,
                            metric_snapshots + page_snapshots,
                        )
                        synced_objects += page_objects
                        metric_snapshots += page_snapshots
            connection.execute(
                "UPDATE meta_sync_logs SET status='COMPLETED', records_synced=%s, completed_at=NOW() WHERE id=%s",
                (synced_objects if sync_type == "INITIAL_IMPORT" else metric_snapshots, sync_id),
            )
        return {"sync_id": str(sync_id), "status": "COMPLETED", "objects_synced": synced_objects, "metric_snapshots": metric_snapshots}
    except Exception as exc:
        safe_error = " ".join(str(exc).split())
        token = os.environ.get("META_SYSTEM_USER_TOKEN", "").strip()
        if token:
            safe_error = safe_error.replace(token, "[REDACTED]")
        safe_error = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", safe_error)[:500]
        logger.error("Meta workspace sync %s failed: %s", sync_id, safe_error)
        try:
            with get_session() as connection:
                connection.execute(
                    "UPDATE meta_sync_logs SET status='FAILED', records_synced=%s, error_message=%s, completed_at=NOW() WHERE id=%s",
                    (synced_objects if sync_type == "INITIAL_IMPORT" else metric_snapshots, safe_error, sync_id),
                )
        except Exception as persist_error:
            logger.exception("Could not persist failed Meta workspace sync state for %s", sync_id)
            raise RuntimeError("Could not persist failed Meta workspace sync state.") from persist_error
        return {
            "sync_id": str(sync_id),
            "status": "FAILED",
            "objects_synced": synced_objects,
            "metric_snapshots": metric_snapshots,
            "error": safe_error,
        }


def _persist_sync_progress(
    connection: Any,
    sync_id: UUID,
    sync_type: str,
    synced_objects: int,
    metric_snapshots: int,
) -> None:
    records_synced = synced_objects if sync_type == "INITIAL_IMPORT" else metric_snapshots
    connection.execute(
        "UPDATE meta_sync_logs SET records_synced=%s WHERE id=%s",
        (records_synced, sync_id),
    )
    connection.commit()


def run_meta_sync_once(*, redis_client: Any | None = None) -> dict[str, Any] | None:
    """Claim and execute one queued Meta sync, retaining the job until it finishes."""
    from apps.api.queue import (
        _redis_client,
        acknowledge_meta_sync_job,
        claim_meta_sync_job,
        retry_meta_sync_job,
    )

    client = redis_client or _redis_client()
    should_close = redis_client is None
    message: str | None = None
    try:
        message = claim_meta_sync_job(redis_client=client)
        if message is None:
            return None
        try:
            payload = json.loads(message)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"sync_id", "workspace_id", "sync_type", "lookback_days"}
                or not isinstance(payload["sync_id"], str)
                or not isinstance(payload["workspace_id"], str)
                or payload["sync_type"] not in SYNC_TYPES
                or isinstance(payload["lookback_days"], bool)
                or not isinstance(payload["lookback_days"], int)
                or not 1 <= payload["lookback_days"] <= 90
            ):
                raise ValueError("invalid Meta sync job payload")
            sync_id = UUID(payload["sync_id"])
            workspace_id = payload["workspace_id"].strip()
            if not workspace_id:
                raise ValueError("invalid Meta sync workspace")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            acknowledge_meta_sync_job(message, redis_client=client)
            logger.error("Discarded malformed Meta sync job: %s", exc)
            return {"status": "FAILED", "error": "Invalid Meta sync job payload."}

        try:
            result = execute_workspace_sync(
                workspace_id,
                sync_id,
                sync_type=payload["sync_type"],
                lookback_days=payload["lookback_days"],
            )
        except Exception:
            retry_meta_sync_job(message, redis_client=client)
            logger.exception("Meta sync worker failed before recording a terminal result")
            return None

        acknowledged = acknowledge_meta_sync_job(message, redis_client=client)
        if acknowledged != 1:
            logger.error("Meta sync job %s completed but was not acknowledged", sync_id)
        return result
    finally:
        if should_close:
            client.close()
