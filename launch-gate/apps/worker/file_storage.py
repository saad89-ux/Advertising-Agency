"""Local file store with opaque, root-confined file references."""

from __future__ import annotations

import os
from pathlib import Path
import re
from uuid import uuid4

from dotenv import load_dotenv


_FILE_REF_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class LocalFileStore:
    """Store binary outputs under STORAGE_DIR and return relative opaque references."""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            load_dotenv()
            configured_root = os.environ.get("STORAGE_DIR", "storage").strip()
            if not configured_root:
                raise ValueError("STORAGE_DIR must not be empty")
            root = configured_root
        self.root = Path(root).expanduser().resolve()

    def put(self, content: bytes) -> str:
        """Persist bytes and return a file_ref relative to the configured storage root."""
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            file_ref = uuid4().hex
            target = self._path_for_ref(file_ref)
            try:
                with target.open("xb") as output:
                    output.write(content)
                return file_ref
            except FileExistsError:
                continue
            except OSError:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        raise FileExistsError("could not allocate a unique storage reference")

    def get(self, file_ref: str) -> bytes:
        """Read a stored object after validating that its resolved path stays in storage."""
        target = self._path_for_ref(file_ref)
        if not target.is_file():
            raise FileNotFoundError("stored file reference does not exist")
        return target.read_bytes()

    def delete(self, file_ref: str) -> None:
        """Delete one stored object; references cannot address outside the store."""
        target = self._path_for_ref(file_ref)
        try:
            target.unlink()
        except FileNotFoundError:
            raise FileNotFoundError("stored file reference does not exist") from None

    def path_for(self, file_ref: str) -> Path:
        """Return a validated resolved path for integrations that need a local path."""
        return self._path_for_ref(file_ref)

    def _path_for_ref(self, file_ref: str) -> Path:
        if not isinstance(file_ref, str) or not _FILE_REF_PATTERN.fullmatch(file_ref):
            raise ValueError("invalid storage file reference")
        root = self.root.resolve()
        target = (root / file_ref).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ValueError("storage file reference escapes the storage directory") from None
        return target
