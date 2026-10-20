"""Local file store with opaque, root-confined file references."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
from uuid import uuid4

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


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

    def put(self, content: bytes, *args: Any, **kwargs: Any) -> str:
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

    def get(self, file_ref: str, *args: Any, **kwargs: Any) -> bytes:
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


class UnifiedFileStore:
    """Cloudinary-backed store with local disk reserved for development/testing."""

    def __init__(self, root: str | Path | None = None) -> None:
        load_dotenv()
        self.cloudinary_required = (
            os.environ.get("REQUIRE_CLOUDINARY", "").strip().lower() in {"1", "true", "yes"}
            or os.environ.get("APP_ENV", "").strip().lower() in {"production", "prod"}
        )
        self.local = LocalFileStore(root=root)
        self.cloud = None
        try:
            from apps.worker.cloud_storage import CloudinaryFileStore, is_cloudinary_configured
            if is_cloudinary_configured():
                self.cloud = CloudinaryFileStore()
        except Exception as exc:
            if self.cloudinary_required:
                raise RuntimeError("Cloudinary is required but could not be initialized") from exc
            logger.debug("Cloudinary initialization skipped or failed: %s", exc)
            self.cloud = None
        if self.cloudinary_required and self.cloud is None:
            raise RuntimeError(
                "Cloudinary is required in production. Configure CLOUDINARY_URL or all Cloudinary credentials."
            )

    def put(self, content: bytes, resource_type: str = "auto", filename: str | None = None) -> str:
        """Persist content to Cloudinary, or local disk only outside production."""
        if self.cloud is not None:
            try:
                return self.cloud.put(content, resource_type=resource_type, filename=filename)
            except Exception as exc:
                if self.cloudinary_required:
                    raise RuntimeError("Cloudinary upload failed; local fallback is disabled") from exc
                logger.warning("Cloudinary upload failed (%s); falling back to local file store: %s", filename or "unnamed", exc)
        if self.cloudinary_required:
            raise RuntimeError("Cloudinary is required; refusing local asset storage")
        return self.local.put(content)

    def get(self, file_ref: str, timeout_seconds: float = 15.0) -> bytes:
        """Retrieve content from Cloudinary CDN URL or local storage."""
        if file_ref.startswith(("http://", "https://")):
            if self.cloud is not None:
                try:
                    return self.cloud.get(file_ref, timeout_seconds=timeout_seconds)
                except Exception as exc:
                    logger.debug("Cloud store get failed for %s: %s", file_ref, exc)
            try:
                import httpx
                resp = httpx.get(
                    file_ref,
                    headers={"User-Agent": "LaunchGate/1.0"},
                    timeout=timeout_seconds,
                    follow_redirects=True,
                )
                resp.raise_for_status()
                return resp.content
            except Exception as exc:
                logger.debug("httpx get failed for %s (%s), attempting urllib", file_ref, exc)
                try:
                    import urllib.request
                    req = urllib.request.Request(file_ref, headers={"User-Agent": "LaunchGate/1.0"})
                    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                        return resp.read()
                except Exception as u_exc:
                    logger.error("Failed downloading remote file %s via both httpx and urllib: %s", file_ref, u_exc)
                    raise FileNotFoundError(f"Could not retrieve remote file {file_ref}: {u_exc}") from u_exc
        return self.local.get(file_ref)

    def delete(self, file_ref: str) -> None:
        """Delete from Cloudinary if URL; otherwise from local storage."""
        if file_ref.startswith(("http://", "https://")):
            if self.cloud is not None:
                try:
                    self.cloud.delete(file_ref)
                except Exception as exc:
                    logger.warning("Cloud store delete failed for %s: %s", file_ref, exc)
        else:
            self.local.delete(file_ref)

    def path_for(self, file_ref: str) -> Path | None:
        """Return local path if stored locally; return None if stored on cloud."""
        if file_ref.startswith(("http://", "https://")):
            return None
        return self.local.path_for(file_ref)


def get_file_store(root: str | Path | None = None) -> UnifiedFileStore:
    """Return a unified file store instance configured for the environment."""
    return UnifiedFileStore(root=root)
