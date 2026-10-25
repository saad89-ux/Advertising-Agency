from datetime import datetime, timezone

from apps.api.sse import broadcaster
from apps.worker.states import MetaEntityStatus


def emit_progress_event(
    batch_id: str,
    entity_type: str,
    grouping_key: str,
    state: MetaEntityStatus,
    error: str | None = None,
    client_id: str | None = None,
) -> None:
    payload = {
        "batch_id": batch_id,
        "entity_type": entity_type,
        "grouping_key": grouping_key,
        "status": state.value,
        "error": error,
        "client_id": client_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    broadcaster.publish_sync(batch_id, "hierarchy_progress", payload)
