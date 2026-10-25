"""Cloudinary cloud asset storage adapter for production document, image, and creative uploads."""

from __future__ import annotations

import io
import logging
import os
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

try:
    import cloudinary
    import cloudinary.uploader
    import cloudinary.api
    _CLOUDINARY_AVAILABLE = True
except ImportError:
    _CLOUDINARY_AVAILABLE = False


def is_cloudinary_configured() -> bool:
    """Check whether Cloudinary credentials are provided in environment."""
    load_dotenv()
    if os.environ.get("CLOUDINARY_URL"):
        return True
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key = os.environ.get("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.environ.get("CLOUDINARY_API_SECRET", "").strip()
    return bool(cloud_name and api_key and api_secret)


def is_cloudinary_asset_url(file_ref: str) -> bool:
    load_dotenv()
    try:
        parsed = urlsplit(file_ref)
        configured_url = os.environ.get("CLOUDINARY_URL", "").strip()
        configured_cloud_name = (
            urlsplit(configured_url).hostname
            if configured_url
            else os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
        )
        path = [part for part in parsed.path.split("/") if part]
        return (
            parsed.scheme == "https"
            and parsed.hostname == "res.cloudinary.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and bool(configured_cloud_name)
            and len(path) >= 4
            and path[0] == configured_cloud_name
            and path[1] in {"image", "raw", "video"}
            and path[2] == "upload"
        )
    except (TypeError, ValueError):
        return False


class CloudinaryFileStore:
    """Production cloud store uploading ad creatives, documents, and evidence to Cloudinary CDN."""

    def __init__(self) -> None:
        if not _CLOUDINARY_AVAILABLE:
            raise RuntimeError("The 'cloudinary' package is not installed. Run 'pip install cloudinary'.")
        
        load_dotenv()
        cloudinary_url = os.environ.get("CLOUDINARY_URL")
        if cloudinary_url:
            cloudinary.config(cloudinary_url=cloudinary_url)
        else:
            cloudinary.config(
                cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip(),
                api_key=os.environ.get("CLOUDINARY_API_KEY", "").strip(),
                api_secret=os.environ.get("CLOUDINARY_API_SECRET", "").strip(),
                secure=True,
            )
        self.folder = os.environ.get("CLOUDINARY_FOLDER", "launchgate_campaigns").strip()

    def put(self, content: bytes, resource_type: str = "auto", filename: str | None = None) -> str:
        """Upload raw bytes to Cloudinary and return the persistent secure HTTPS URL.
        
        Properly handles image formats (png, jpg, webp) as resource_type="image"
        and documents (pdf, csv, xlsx) as resource_type="raw" or "auto".
        """
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")

        effective_resource_type = resource_type
        if resource_type == "auto":
            ext = os.path.splitext(filename or "")[1].lower()
            if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg"}:
                effective_resource_type = "image"
            elif ext in {".pdf", ".csv", ".xlsx", ".xls", ".txt", ".json", ".md", ".zip"}:
                effective_resource_type = "raw"
            elif ext in {".mp4", ".mov", ".webm", ".avi"}:
                effective_resource_type = "video"
            elif content.startswith(b"\x89PNG\r\n\x1a\n") or content.startswith(b"\xff\xd8\xff") or (content.startswith(b"RIFF") and b"WEBP" in content[:16]):
                effective_resource_type = "image"
            elif content.startswith(b"%PDF"):
                effective_resource_type = "raw"
            else:
                effective_resource_type = "auto"

        public_id = f"{self.folder}/{uuid4().hex}"
        try:
            upload_result = cloudinary.uploader.upload(
                io.BytesIO(content),
                public_id=public_id,
                resource_type=effective_resource_type,
                overwrite=True,
            )
            secure_url = upload_result.get("secure_url") or upload_result.get("url")
            if not secure_url:
                raise RuntimeError(f"Cloudinary upload succeeded but returned no URL (result={upload_result})")
            logger.info("Successfully uploaded asset to Cloudinary (%s): %s", effective_resource_type, secure_url)
            return str(secure_url)
        except Exception as exc:
            logger.error("Cloudinary upload failed for %s (resource_type=%s): %s", filename or public_id, effective_resource_type, exc)
            raise RuntimeError(f"Cloudinary upload failed: {exc}") from exc

    def get(self, file_ref: str, timeout_seconds: float = 15.0) -> bytes:
        """Download asset content from Cloudinary CDN URL."""
        if not is_cloudinary_asset_url(file_ref):
            raise ValueError("Invalid Cloudinary URL reference")
        try:
            import httpx
            resp = httpx.get(
                file_ref,
                headers={"User-Agent": "LaunchGate/1.0"},
                timeout=timeout_seconds,
                follow_redirects=False,
            )
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            logger.error("Failed downloading Cloudinary asset: %s", exc)
            raise FileNotFoundError("Could not download asset from storage") from exc

    def delete(self, file_ref: str) -> None:
        """Delete an asset from Cloudinary by its public ID or URL."""
        try:
            # Extract public_id from URL if necessary
            public_id = file_ref.split("/")[-1].split(".")[0]
            cloudinary.uploader.destroy(f"{self.folder}/{public_id}")
        except Exception as exc:
            logger.warning("Cloudinary delete failed for %s: %s", file_ref, exc)
