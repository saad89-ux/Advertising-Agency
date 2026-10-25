"""Server-Sent Events (SSE) live status stream and event broadcaster for batches.

PRD §7 / D12: Live status via Server-Sent Events (one-way updates are enough).
Architecture §2: LIVE (Live status SSE) -> REV (Review screen).
Never-break Rule 3: Client scoping on every event.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime, timezone
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from apps.api.auth import CurrentUser, get_current_user_for_sse
from apps.api.batches import (
    get_ads_findings,
    get_ads_verdicts,
    get_batch_ads,
    get_batch_record,
)
from apps.api.database import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sse"])


def format_sse(
    data: Any,
    event: str | None = None,
    id: str | None = None,
    retry: int | None = None,
) -> str:
    """Format data into an SSE (text/event-stream) message."""
    lines: list[str] = []
    if id is not None:
        lines.append(f"id: {id}")
    if event is not None:
        lines.append(f"event: {event}")
    if retry is not None:
        lines.append(f"retry: {retry}")

    if isinstance(data, (dict, list)):
        payload = json.dumps(data, separators=(",", ":"), default=str)
    else:
        payload = str(data)

    for line in payload.splitlines():
        lines.append(f"data: {line}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


class BatchEventBroadcaster:
    """In-memory pub-sub broadcaster for batch events."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[tuple[asyncio.Queue[str], asyncio.AbstractEventLoop]]] = {}
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def subscribe(self, batch_id: str) -> asyncio.Queue[str]:
        """Subscribe an asyncio queue to events for a specific batch."""
        loop = asyncio.get_running_loop()
        lock = self._get_lock()
        async with lock:
            if batch_id not in self._subscribers:
                self._subscribers[batch_id] = set()
            queue: asyncio.Queue[str] = asyncio.Queue()
            self._subscribers[batch_id].add((queue, loop))
            return queue

    async def unsubscribe(self, batch_id: str, queue: asyncio.Queue[str]) -> None:
        """Unsubscribe an asyncio queue from a specific batch."""
        lock = self._get_lock()
        async with lock:
            subscribers = self._subscribers.get(batch_id)
            if subscribers:
                to_remove = [item for item in subscribers if item[0] is queue]
                for item in to_remove:
                    subscribers.remove(item)
                if not subscribers:
                    self._subscribers.pop(batch_id, None)

    async def publish(self, batch_id: str, event: str, data: dict[str, Any]) -> int:
        """Publish an SSE formatted event to all active subscribers of batch_id."""
        return self.publish_sync(batch_id, event, data)

    def publish_sync(self, batch_id: str, event: str, data: dict[str, Any]) -> int:
        """Publish an event synchronously (thread-safe for worker processes or sync routes)."""
        subscribers = list(self._subscribers.get(batch_id, set()))
        if not subscribers:
            return 0
        message = format_sse(data, event=event)
        count = 0
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        for queue, loop in subscribers:
            try:
                if loop.is_closed():
                    continue
                if current_loop is loop:
                    queue.put_nowait(message)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, message)
                count += 1
            except Exception:
                pass
        return count


# Singleton broadcaster instance
broadcaster = BatchEventBroadcaster()


def notify_ad_update(
    batch_id: str,
    ad_id: str,
    *,
    verdict: str | None = None,
    stage: str | None = None,
    row_number: int | None = None,
    client_id: str | None = None,
    approval_state: str = "pending_review",
    findings_count: int = 0,
) -> None:
    """Convenience helper to broadcast an individual ad's verdict and stage updates."""
    payload = {
        "batch_id": batch_id,
        "ad_id": ad_id,
        "row_number": row_number,
        "client_id": client_id,
        "verdict": verdict,
        "stage": stage,
        "approval_state": approval_state,
        "findings_count": findings_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    broadcaster.publish_sync(batch_id, "ad_status", payload)


def notify_batch_status(batch_id: str, summary: dict[str, Any]) -> None:
    """Convenience helper to broadcast an overall batch summary update."""
    payload = {
        "batch_id": batch_id,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    broadcaster.publish_sync(batch_id, "batch_status", payload)


def calculate_batch_live_status(
    connection: Any, batch_id: str, allowed_clients: Sequence[str]
) -> dict[str, Any]:
    """Calculate current live progress and verdict counts for a batch scoped to allowed clients."""
    visible_ads = get_batch_ads(connection, batch_id, allowed_clients)
    ad_ids = [ad["ad_id"] for ad in visible_ads]
    findings_by_ad = get_ads_findings(connection, ad_ids)
    verdicts_by_ad = get_ads_verdicts(connection, batch_id, visible_ads, findings_by_ad)

    # Approvals info
    approvals_by_ad: dict[str, str] = {}
    if ad_ids:
        try:
            cur = connection.execute(
                "SELECT ad_id, state FROM ad_approvals WHERE ad_id = ANY(%s);",
                (ad_ids,),
            )
            for r in cur.fetchall():
                approvals_by_ad[str(r[0])] = str(r[1])
        except Exception:
            pass

    verdict_counts = {
        "ready": 0,
        "fix needed": 0,
        "blocked": 0,
        "needs a person": 0,
        "pending": 0,
    }
    total_findings = 0
    ad_statuses: list[dict[str, Any]] = []

    for ad in visible_ads:
        aid = ad["ad_id"]
        v = verdicts_by_ad.get(aid, "pending")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        ad_finds = findings_by_ad.get(aid, [])
        total_findings += len(ad_finds)
        ad_statuses.append({
            "ad_id": aid,
            "row_number": ad["row_number"],
            "client_id": ad["client_id"],
            "verdict": v,
            "approval_state": approvals_by_ad.get(aid, "pending_review"),
            "findings_count": len(ad_finds),
        })

    total_ads = len(visible_ads)
    pending_ads = verdict_counts.get("pending", 0)
    processed_ads = total_ads - pending_ads
    progress_percent = round((processed_ads / total_ads) * 100.0, 1) if total_ads > 0 else 100.0
    status_str = "completed" if total_ads > 0 and pending_ads == 0 else ("processing" if total_ads > 0 else "empty")

    campaign_statuses: list[dict[str, Any]] = []
    ad_set_statuses: list[dict[str, Any]] = []
    try:
        campaign_rows = connection.execute(
                """SELECT id::text, grouping_key, platform, meta_id, status, last_error, workspace_id
                    FROM meta_campaign_runs WHERE batch_id=%s AND workspace_id=ANY(%s)""",
                (batch_id, list(allowed_clients)),
        ).fetchall()
        campaign_statuses = [
            {"run_id": row[0], "grouping_key": row[1], "platform": row[2], "meta_campaign_id": row[3],
             "lifecycle_status": row[4], "last_error": row[5], "client_id": row[6]}
            for row in campaign_rows
        ]
        ad_set_rows = connection.execute(
            """SELECT ad_set.id::text, campaign.grouping_key, ad_set.grouping_key, campaign.platform,
                      ad_set.meta_id, ad_set.status, ad_set.last_error, campaign.workspace_id
               FROM meta_ad_set_runs ad_set
               JOIN meta_campaign_runs campaign ON campaign.id=ad_set.campaign_run_id
                    WHERE campaign.batch_id=%s AND campaign.workspace_id=ANY(%s)""",
                (batch_id, list(allowed_clients)),
        ).fetchall()
        ad_set_statuses = [
            {"run_id": row[0], "campaign_key": row[1], "grouping_key": row[2], "platform": row[3],
             "meta_ad_set_id": row[4], "lifecycle_status": row[5], "last_error": row[6], "client_id": row[7]}
            for row in ad_set_rows
        ]
    except Exception:
        pass

    return {
        "batch_id": batch_id,
        "status": status_str,
        "total_ads": total_ads,
        "processed_ads": processed_ads,
        "pending_ads": pending_ads,
        "progress_percent": progress_percent,
        "verdict_counts": verdict_counts,
        "total_findings": total_findings,
        "client_ids": sorted({ad["client_id"] for ad in visible_ads}),
        "campaign_statuses": campaign_statuses,
        "ad_set_statuses": ad_set_statuses,
        "ad_statuses": ad_statuses,
    }


def verify_batch_client_access(connection: Any, batch_id: str, user: CurrentUser) -> None:
    """Verify batch existence and multi-tenant client authorization.
    
    Raises 404 if batch not found, 403 if user not assigned to any client in the batch.
    """
    batch_rec = get_batch_record(connection, batch_id)
    if batch_rec is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if user.guest_session_id and batch_rec.get("guest_session_id") != user.guest_session_id:
        raise HTTPException(status_code=404, detail="batch not found")

    cur_total = connection.execute("SELECT COUNT(*) FROM ads WHERE batch_id = %s", (batch_id,))
    total_in_batch = int(cur_total.fetchone()[0])

    allowed_clients = list(user.assigned_client_ids)
    visible_ads = get_batch_ads(connection, batch_id, allowed_clients)

    if total_in_batch > 0 and len(visible_ads) == 0:
        raise HTTPException(status_code=403, detail="forbidden")

    if total_in_batch == 0 and batch_rec["uploaded_by"] != user.user_id:
        raise HTTPException(status_code=403, detail="forbidden")


async def generate_batch_events(
    request: Request,
    batch_id: str,
    user: CurrentUser,
    *,
    once: bool = False,
    poll_interval: float = 1.0,
    max_duration_seconds: float = 300.0,
) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE formatted events for batch live status."""
    allowed_clients = list(user.assigned_client_ids)
    user_clients_set = set(allowed_clients)

    # 1. Verify access upfront before starting stream
    with get_session() as conn:
        verify_batch_client_access(conn, batch_id, user)
        initial_status = calculate_batch_live_status(conn, batch_id, allowed_clients)

    # 2. Yield initial handshake and status
    yield format_sse(
        {
            "batch_id": batch_id,
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "client_ids": initial_status["client_ids"],
        },
        event="connected",
    )
    yield format_sse(initial_status, event="batch_status")

    # If all ads are already processed or client requested a one-shot snapshot
    if once or initial_status["status"] == "completed":
        if initial_status["status"] == "completed":
            yield format_sse(
                {
                    "batch_id": batch_id,
                    "status": "completed",
                    "total_ads": initial_status["total_ads"],
                    "verdict_counts": initial_status["verdict_counts"],
                },
                event="complete",
            )
        return

    # 3. Subscribe to real-time events
    queue = await broadcaster.subscribe(batch_id)
    last_processed = initial_status["processed_ads"]
    start_time = asyncio.get_running_loop().time()

    try:
        while True:
            # Check client disconnection
            if await request.is_disconnected():
                break

            # Timeout after max_duration to prevent indefinite leaked connections
            now = asyncio.get_running_loop().time()
            if now - start_time > max_duration_seconds:
                yield format_sse({"batch_id": batch_id, "reason": "timeout"}, event="complete")
                break

            try:
                # Wait for broadcast event with timeout
                msg = await asyncio.wait_for(queue.get(), timeout=poll_interval)
                should_skip = False
                if '"client_id":' in msg:
                    try:
                        for line in msg.splitlines():
                            if line.startswith("data: "):
                                d = json.loads(line[6:])
                                cid = d.get("client_id")
                                if cid and cid not in user_clients_set:
                                    should_skip = True
                                    break
                    except Exception:
                        pass
                if not should_skip:
                    yield msg
            except asyncio.TimeoutError:
                # Poll DB on timeout to ensure no dropped events or external DB updates
                with get_session() as conn:
                    current_status = calculate_batch_live_status(conn, batch_id, allowed_clients)

                if current_status["processed_ads"] != last_processed:
                    last_processed = current_status["processed_ads"]
                    yield format_sse(current_status, event="batch_status")

                    if current_status["status"] == "completed":
                        yield format_sse(
                            {
                                "batch_id": batch_id,
                                "status": "completed",
                                "total_ads": current_status["total_ads"],
                                "verdict_counts": current_status["verdict_counts"],
                            },
                            event="complete",
                        )
                        break
                else:
                    # Emit heartbeat keep-alive
                    yield ": ping\n\n"
    finally:
        await broadcaster.unsubscribe(batch_id, queue)


@router.get("/batches/{batch_id}/events")
async def get_batch_events_endpoint(
    request: Request,
    batch_id: str,
    user: CurrentUser = Depends(get_current_user_for_sse),
    once: bool = Query(False, description="Return initial events and exit (for testing/snapshots)"),
    poll_interval: float = Query(1.0, description="Polling interval in seconds for DB fallback"),
):
    """Server-Sent Events endpoint streaming live status and verdict updates for a batch."""
    # Pre-verify permissions synchronously so HTTP error status (401/403/404) is returned directly
    with get_session() as conn:
        verify_batch_client_access(conn, batch_id, user)

    return StreamingResponse(
        generate_batch_events(request, batch_id, user, once=once, poll_interval=poll_interval),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/batches/{batch_id}/live")
async def get_batch_live_endpoint(
    request: Request,
    batch_id: str,
    user: CurrentUser = Depends(get_current_user_for_sse),
    once: bool = Query(False),
    poll_interval: float = Query(1.0),
):
    """Alias for /batches/{batch_id}/events providing identical live status streaming."""
    return await get_batch_events_endpoint(request, batch_id, user, once=once, poll_interval=poll_interval)


@router.get("/batches/{batch_id}/status")
def get_batch_status_json_endpoint(
    batch_id: str,
    user: CurrentUser = Depends(get_current_user_for_sse),
) -> dict[str, Any]:
    """JSON snapshot endpoint returning live progress and verdict counts for a batch."""
    with get_session() as conn:
        verify_batch_client_access(conn, batch_id, user)
        allowed_clients = list(user.assigned_client_ids)
        return calculate_batch_live_status(conn, batch_id, allowed_clients)
