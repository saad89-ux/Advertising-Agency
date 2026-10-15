"""PostgreSQL-backed LangGraph checkpointer with thread_id = batch_id:ad_id.

Architecture & PRD compliance:
- PRD D18 & Architecture §4: State is checkpointed to PostgreSQL after every step
  with thread_id = batch_id:ad_id, allowing checks to resume after a crash, restart, or deploy.
- Subclasses LangGraph's BaseCheckpointSaver.
- Independent table creation (preserving apps/api/schema.py separation).
- Supports synchronous and asynchronous operations.
- Clean connection lifecycle and transaction commit semantics with psycopg 3.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager
import logging
import random
from typing import Any

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    RunnableConfig,
    SerializerProtocol,
    WRITES_IDX_MAP,
    get_checkpoint_id,
    get_checkpoint_metadata,
    writes_sort_key,
)

logger = logging.getLogger(__name__)

# --- DDL Statements ---

CHECKPOINTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    checkpoint_type TEXT NOT NULL,
    checkpoint BYTEA NOT NULL,
    metadata_type TEXT NOT NULL,
    metadata BYTEA NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
"""

CHECKPOINT_BLOBS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
"""

CHECKPOINT_WRITES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT NOT NULL,
    value BYTEA NOT NULL,
    task_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
"""


# --- Thread ID Helpers ---

def make_thread_id(batch_id: str, ad_id: str) -> str:
    """Format a thread_id strictly as 'batch_id:ad_id'.

    Raises:
        ValueError: If batch_id or ad_id is empty, or if batch_id contains a colon.
    """
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("batch_id must be a non-empty string")
    if not isinstance(ad_id, str) or not ad_id.strip():
        raise ValueError("ad_id must be a non-empty string")
    b = batch_id.strip()
    a = ad_id.strip()
    if ":" in b:
        raise ValueError(f"batch_id cannot contain a colon: {batch_id!r}")
    return f"{b}:{a}"


def parse_thread_id(thread_id: str) -> tuple[str, str]:
    """Parse and validate a thread_id formatted strictly as 'batch_id:ad_id'.

    Returns:
        tuple[str, str]: (batch_id, ad_id)

    Raises:
        ValueError: If thread_id is invalid or missing required components.
    """
    if not isinstance(thread_id, str) or ":" not in thread_id:
        raise ValueError(
            f"thread_id must be formatted as 'batch_id:ad_id', got {thread_id!r}"
        )
    parts = thread_id.split(":", 1)
    batch_id = parts[0].strip()
    ad_id = parts[1].strip()
    if not batch_id or not ad_id:
        raise ValueError(
            f"thread_id must contain non-empty batch_id and ad_id, got {thread_id!r}"
        )
    return batch_id, ad_id


def validate_thread_id(thread_id: str) -> bool:
    """Return True if thread_id strictly conforms to 'batch_id:ad_id', False otherwise."""
    try:
        parse_thread_id(thread_id)
        return True
    except ValueError:
        return False


# --- DDL Execution ---

def _execute_checkpoint_ddl(conn: Any) -> None:
    """Execute table creation DDLs on a connection."""
    with conn.cursor() as cur:
        cur.execute(CHECKPOINTS_TABLE_DDL)
        cur.execute(CHECKPOINT_BLOBS_TABLE_DDL)
        cur.execute(CHECKPOINT_WRITES_TABLE_DDL)
    if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
        conn.commit()


def create_checkpoint_tables(connection: Any = None) -> None:
    """Create checkpointer tables in PostgreSQL independently of API schema."""
    if connection is not None:
        _execute_checkpoint_ddl(connection)
    else:
        from apps.api.database import get_session
        with get_session() as conn:
            _execute_checkpoint_ddl(conn)


# --- PostgreSQL Checkpoint Saver ---

class PostgresCheckpointSaver(BaseCheckpointSaver):
    """PostgreSQL-backed LangGraph CheckpointSaver.

    Persists checkpoints, channel values (blobs), and pending writes to PostgreSQL.
    Enforces thread_id = 'batch_id:ad_id' format according to Architecture §5 & D18.
    """

    def __init__(
        self,
        connection: Any = None,
        *,
        connection_factory: Callable[[], Any] | None = None,
        serde: SerializerProtocol | None = None,
        enforce_thread_format: bool = True,
    ) -> None:
        super().__init__(serde=serde)
        self.connection = connection
        self.connection_factory = connection_factory
        self.enforce_thread_format = enforce_thread_format

    def setup(self) -> None:
        """Create checkpointer tables if they do not exist."""
        with self._get_connection() as conn:
            _execute_checkpoint_ddl(conn)

    @contextmanager
    def _get_connection(self) -> Iterator[Any]:
        """Acquire a connection from instance, factory, or database session."""
        if self.connection is not None:
            yield self.connection
        elif self.connection_factory is not None:
            conn = self.connection_factory()
            try:
                yield conn
                if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
                    conn.commit()
            except Exception:
                if hasattr(conn, "rollback") and not getattr(conn, "autocommit", False):
                    conn.rollback()
                raise
            finally:
                if hasattr(conn, "close"):
                    conn.close()
        else:
            from apps.api.database import get_session
            with get_session() as conn:
                yield conn

    def _commit_if_needed(self, conn: Any) -> None:
        """Commit transaction if connection has commit method and autocommit is disabled."""
        if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
            conn.commit()

    def _validate_thread(self, thread_id: str) -> None:
        """Validate thread_id format if enforce_thread_format is enabled."""
        if self.enforce_thread_format:
            parse_thread_id(thread_id)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Retrieve a checkpoint tuple from PostgreSQL matching config."""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        self._validate_thread(thread_id)
        checkpoint_id = get_checkpoint_id(config)

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                if checkpoint_id:
                    cur.execute(
                        """
                        SELECT checkpoint_id, parent_checkpoint_id, checkpoint_type, checkpoint, metadata_type, metadata
                        FROM checkpoints
                        WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s
                        """,
                        (thread_id, checkpoint_ns, checkpoint_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT checkpoint_id, parent_checkpoint_id, checkpoint_type, checkpoint, metadata_type, metadata
                        FROM checkpoints
                        WHERE thread_id = %s AND checkpoint_ns = %s
                        ORDER BY checkpoint_id DESC
                        LIMIT 1
                        """,
                        (thread_id, checkpoint_ns),
                    )
                row = cur.fetchone()
                if not row:
                    return None

                cid, parent_cid, c_type, c_data, m_type, m_data = row

                raw_c_bytes = c_data if isinstance(c_data, (bytes, bytearray)) else bytes(c_data)
                raw_m_bytes = m_data if isinstance(m_data, (bytes, bytearray)) else bytes(m_data)

                checkpoint_: Checkpoint = self.serde.loads_typed((c_type, raw_c_bytes))
                metadata_ = self.serde.loads_typed((m_type, raw_m_bytes))

                # Load channel values from checkpoint_blobs
                cur.execute(
                    """
                    SELECT channel, version, type, blob
                    FROM checkpoint_blobs
                    WHERE thread_id = %s AND checkpoint_ns = %s
                    """,
                    (thread_id, checkpoint_ns),
                )
                blobs_map: dict[tuple[str, str], tuple[str, Any]] = {
                    (r[0], str(r[1])): (r[2], r[3]) for r in cur.fetchall()
                }

                channel_values: dict[str, Any] = {}
                for k, ver in checkpoint_.get("channel_versions", {}).items():
                    blob_entry = blobs_map.get((k, str(ver)))
                    if blob_entry is not None:
                        b_type, b_data = blob_entry
                        if b_type != "empty":
                            raw_b = b_data if isinstance(b_data, (bytes, bytearray)) else bytes(b_data)
                            channel_values[k] = self.serde.loads_typed((b_type, raw_b))

                # Load pending writes from checkpoint_writes
                cur.execute(
                    """
                    SELECT task_id, idx, channel, type, value, task_path
                    FROM checkpoint_writes
                    WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s
                    """,
                    (thread_id, checkpoint_ns, cid),
                )
                w_rows = cur.fetchall()
                w_rows.sort(key=lambda r: writes_sort_key(r[5], r[0], r[1]))
                pending_writes = [
                    (
                        r[0],
                        r[2],
                        self.serde.loads_typed(
                            (r[3], r[4] if isinstance(r[4], (bytes, bytearray)) else bytes(r[4]))
                        ),
                    )
                    for r in w_rows
                ]

                parent_config = (
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": parent_cid,
                        }
                    }
                    if parent_cid
                    else None
                )

                return CheckpointTuple(
                    config={
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": cid,
                        }
                    },
                    checkpoint={
                        **checkpoint_,
                        "channel_values": channel_values,
                    },
                    metadata=metadata_,
                    pending_writes=pending_writes,
                    parent_config=parent_config,
                )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints from PostgreSQL matching criteria in descending order."""
        thread_id = config["configurable"]["thread_id"] if config else None
        checkpoint_ns = config["configurable"].get("checkpoint_ns") if config else None
        target_cid = get_checkpoint_id(config) if config else None
        before_cid = get_checkpoint_id(before) if before else None

        if thread_id is not None:
            self._validate_thread(thread_id)

        query = (
            "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
            "checkpoint_type, checkpoint, metadata_type, metadata "
            "FROM checkpoints"
        )
        params: list[Any] = []
        where_clauses: list[str] = []

        if thread_id is not None:
            where_clauses.append("thread_id = %s")
            params.append(thread_id)
        if checkpoint_ns is not None:
            where_clauses.append("checkpoint_ns = %s")
            params.append(checkpoint_ns)

        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += " ORDER BY checkpoint_id DESC"

        remaining_limit = limit

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()

            for row in rows:
                t_id, ns, cid, parent_cid, c_type, c_data, m_type, m_data = row

                if target_cid is not None and cid != target_cid:
                    continue
                if before_cid is not None and cid >= before_cid:
                    continue

                raw_m_bytes = m_data if isinstance(m_data, (bytes, bytearray)) else bytes(m_data)
                metadata_ = self.serde.loads_typed((m_type, raw_m_bytes))

                if filter and not all(metadata_.get(k) == v for k, v in filter.items()):
                    continue

                raw_c_bytes = c_data if isinstance(c_data, (bytes, bytearray)) else bytes(c_data)
                checkpoint_: Checkpoint = self.serde.loads_typed((c_type, raw_c_bytes))

                # Load channel values for this checkpoint
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT channel, version, type, blob
                        FROM checkpoint_blobs
                        WHERE thread_id = %s AND checkpoint_ns = %s
                        """,
                        (t_id, ns),
                    )
                    blobs_map: dict[tuple[str, str], tuple[str, Any]] = {
                        (r[0], str(r[1])): (r[2], r[3]) for r in cur.fetchall()
                    }

                    channel_values: dict[str, Any] = {}
                    for k, ver in checkpoint_.get("channel_versions", {}).items():
                        blob_entry = blobs_map.get((k, str(ver)))
                        if blob_entry is not None:
                            b_type, b_data = blob_entry
                            if b_type != "empty":
                                raw_b = b_data if isinstance(b_data, (bytes, bytearray)) else bytes(b_data)
                                channel_values[k] = self.serde.loads_typed((b_type, raw_b))

                    # Load pending writes
                    cur.execute(
                        """
                        SELECT task_id, idx, channel, type, value, task_path
                        FROM checkpoint_writes
                        WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s
                        """,
                        (t_id, ns, cid),
                    )
                    w_rows = cur.fetchall()
                    w_rows.sort(key=lambda r: writes_sort_key(r[5], r[0], r[1]))
                    pending_writes = [
                        (
                            r[0],
                            r[2],
                            self.serde.loads_typed(
                                (r[3], r[4] if isinstance(r[4], (bytes, bytearray)) else bytes(r[4]))
                            ),
                        )
                        for r in w_rows
                    ]

                parent_config = (
                    {
                        "configurable": {
                            "thread_id": t_id,
                            "checkpoint_ns": ns,
                            "checkpoint_id": parent_cid,
                        }
                    }
                    if parent_cid
                    else None
                )

                yield CheckpointTuple(
                    config={
                        "configurable": {
                            "thread_id": t_id,
                            "checkpoint_ns": ns,
                            "checkpoint_id": cid,
                        }
                    },
                    checkpoint={
                        **checkpoint_,
                        "channel_values": channel_values,
                    },
                    metadata=metadata_,
                    pending_writes=pending_writes,
                    parent_config=parent_config,
                )

                if remaining_limit is not None:
                    remaining_limit -= 1
                    if remaining_limit <= 0:
                        break

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Save a checkpoint and channel versions to PostgreSQL."""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        self._validate_thread(thread_id)

        checkpoint_id = checkpoint["id"]
        parent_checkpoint_id = config["configurable"].get("checkpoint_id")

        c = checkpoint.copy()
        channel_values: dict[str, Any] = c.pop("channel_values", {})
        type_c, bytes_c = self.serde.dumps_typed(c)

        metadata_to_save = get_checkpoint_metadata(config, metadata)
        type_m, bytes_m = self.serde.dumps_typed(metadata_to_save)

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                # Upsert checkpoint
                cur.execute(
                    """
                    INSERT INTO checkpoints (
                        thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                        checkpoint_type, checkpoint, metadata_type, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id)
                    DO UPDATE SET
                        parent_checkpoint_id = EXCLUDED.parent_checkpoint_id,
                        checkpoint_type = EXCLUDED.checkpoint_type,
                        checkpoint = EXCLUDED.checkpoint,
                        metadata_type = EXCLUDED.metadata_type,
                        metadata = EXCLUDED.metadata;
                    """,
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        parent_checkpoint_id,
                        type_c,
                        bytes_c,
                        type_m,
                        bytes_m,
                    ),
                )

                # Upsert channel blobs
                for k, v in new_versions.items():
                    if k in channel_values:
                        type_b, bytes_b = self.serde.dumps_typed(channel_values[k])
                    else:
                        type_b, bytes_b = ("empty", b"")

                    cur.execute(
                        """
                        INSERT INTO checkpoint_blobs (
                            thread_id, checkpoint_ns, channel, version, type, blob
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (thread_id, checkpoint_ns, channel, version)
                        DO UPDATE SET
                            type = EXCLUDED.type,
                            blob = EXCLUDED.blob;
                        """,
                        (thread_id, checkpoint_ns, k, str(v), type_b, bytes_b),
                    )

            self._commit_if_needed(conn)

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Save pending task writes to PostgreSQL."""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        self._validate_thread(thread_id)
        checkpoint_id: str = config["configurable"]["checkpoint_id"]

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                for idx, (channel, val) in enumerate(writes):
                    write_idx = WRITES_IDX_MAP.get(channel, idx)
                    type_w, bytes_w = self.serde.dumps_typed(val)

                    cur.execute(
                        """
                        INSERT INTO checkpoint_writes (
                            thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
                            channel, type, value, task_path
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                        DO UPDATE SET
                            channel = EXCLUDED.channel,
                            type = EXCLUDED.type,
                            value = EXCLUDED.value,
                            task_path = EXCLUDED.task_path;
                        """,
                        (
                            thread_id,
                            checkpoint_ns,
                            checkpoint_id,
                            task_id,
                            write_idx,
                            channel,
                            type_w,
                            bytes_w,
                            task_path,
                        ),
                    )

            self._commit_if_needed(conn)

    def delete_thread(self, thread_id: str) -> None:
        """Delete all checkpoints, blobs, and writes for a thread_id."""
        self._validate_thread(thread_id)
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,))
                cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
                cur.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
            self._commit_if_needed(conn)

    def get_next_version(self, current: Any | None, channel: None = None) -> str:
        """Generate monotonically increasing channel version identifiers."""
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        elif isinstance(current, str):
            try:
                current_v = int(current.split(".")[0])
            except ValueError:
                current_v = 0
        else:
            current_v = 0
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"

    # --- Async Delegations ---

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Asynchronous get_tuple."""
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Asynchronous list."""
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Asynchronous put."""
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Asynchronous put_writes."""
        return self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """Asynchronous delete_thread."""
        return self.delete_thread(thread_id)
