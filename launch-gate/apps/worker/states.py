from enum import Enum


class MetaEntityStatus(str, Enum):
    PENDING = "pending"
    CREATING = "creating"
    CREATED_PAUSED = "created_paused"
    FAILED = "failed"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    ACTIVE = "active"


RUN_STATUS_TO_ENTITY_STATUS = {
    "PENDING": MetaEntityStatus.PENDING,
    "CREATING": MetaEntityStatus.CREATING,
    "CREATED": MetaEntityStatus.CREATED_PAUSED,
    "FAILED": MetaEntityStatus.FAILED,
    "RECONCILING": MetaEntityStatus.NEEDS_RECONCILIATION,
    "ACTIVE": MetaEntityStatus.ACTIVE,
}


def entity_status_for_run(run_status: str) -> MetaEntityStatus:
    try:
        return RUN_STATUS_TO_ENTITY_STATUS[run_status]
    except KeyError as exc:
        raise ValueError(f"Unsupported Meta entity run status: {run_status}") from exc
