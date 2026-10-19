"""Tests for Cloudinary and Unified Storage integration.

Covers:
1. is_cloudinary_configured() detection under various environment configurations.
2. CloudinaryFileStore:
   - Resource type inference by extension (.png, .jpg, .webp -> image; .pdf, .csv, .xlsx -> raw; .mp4 -> video)
   - Resource type inference by magic bytes (PNG, JPEG, WEBP, PDF)
   - Upload success returning secure HTTPS CDN URL
   - Upload failure handling and logging
   - Retrieval via httpx and urllib fallback
   - Safe deletion
3. UnifiedFileStore:
   - Default behavior when Cloudinary is unconfigured (delegates to LocalFileStore)
   - Behavior when Cloudinary is configured (uploads to Cloudinary, returns HTTPS URL)
   - Graceful fallback to LocalFileStore when Cloudinary upload fails
   - Retrieval of remote HTTPS URLs and local file references
   - path_for() returning local Path or None for remote URLs
4. apps.api.evidence integration:
   - save_evidence_file() utilizing UnifiedFileStore
   - Evidence endpoint streaming remote HTTPS content
   - Evidence endpoint redirecting (307) when redirect=True
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.worker.cloud_storage import CloudinaryFileStore, is_cloudinary_configured
from apps.worker.file_storage import LocalFileStore, UnifiedFileStore, get_file_store


# ---------------------------------------------------------------------------
# 1. Configuration detection tests
# ---------------------------------------------------------------------------

def test_is_cloudinary_configured_false_when_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "")
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "")

    assert not is_cloudinary_configured()


def test_is_cloudinary_configured_false_when_partial_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "")
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "mycloud")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "123456")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "")

    assert not is_cloudinary_configured()


def test_is_cloudinary_configured_true_with_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")
    assert is_cloudinary_configured()


def test_is_cloudinary_configured_true_with_explicit_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLOUDINARY_URL", raising=False)
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "mycloud")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "key123")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "secret456")

    assert is_cloudinary_configured()


# ---------------------------------------------------------------------------
# 2. CloudinaryFileStore tests
# ---------------------------------------------------------------------------

def test_cloudinary_file_store_rejects_non_bytes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")
    store = CloudinaryFileStore()
    with pytest.raises(TypeError, match="content must be bytes"):
        store.put("not-bytes")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("filename", "content", "expected_resource_type"),
    [
        ("screenshot.png", b"plain_bytes", "image"),
        ("ad_creative.jpg", b"plain_bytes", "image"),
        ("banner.jpeg", b"plain_bytes", "image"),
        ("hero.webp", b"plain_bytes", "image"),
        ("badge.svg", b"plain_bytes", "image"),
        ("report.pdf", b"plain_bytes", "raw"),
        ("data.csv", b"plain_bytes", "raw"),
        ("budget.xlsx", b"plain_bytes", "raw"),
        ("notes.txt", b"plain_bytes", "raw"),
        ("promo.mp4", b"plain_bytes", "video"),
        # Magic byte detection when filename has no extension:
        (None, b"\x89PNG\r\n\x1a\n\x00\x00", "image"),
        (None, b"\xff\xd8\xff\xe0\x00\x10JFIF", "image"),
        (None, b"RIFF\x1e\x00\x00\x00WEBPVP8 ", "image"),
        (None, b"%PDF-1.4\n%\xe2\xe3\xcf\xd3", "raw"),
        (None, b"unknown binary data stream", "auto"),
    ],
)
def test_cloudinary_file_store_resource_type_detection(
    monkeypatch: pytest.MonkeyPatch,
    filename: str | None,
    content: bytes,
    expected_resource_type: str,
):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")
    store = CloudinaryFileStore()

    with patch("cloudinary.uploader.upload") as mock_upload:
        mock_upload.return_value = {
            "secure_url": f"https://res.cloudinary.com/mycloud/image/upload/v1234/file.ext",
            "url": f"http://res.cloudinary.com/mycloud/image/upload/v1234/file.ext",
        }

        result_url = store.put(content, filename=filename)

        assert result_url.startswith("https://res.cloudinary.com/")
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs["resource_type"] == expected_resource_type


def test_cloudinary_file_store_upload_failure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")
    store = CloudinaryFileStore()

    with patch("cloudinary.uploader.upload", side_effect=Exception("Connection timed out")):
        with pytest.raises(RuntimeError, match="Cloudinary upload failed: Connection timed out"):
            store.put(b"test data", filename="upload.png")


def test_cloudinary_file_store_get_via_httpx(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")
    store = CloudinaryFileStore()
    target_url = "https://res.cloudinary.com/mycloud/image/upload/v1/asset.png"

    mock_resp = MagicMock()
    mock_resp.content = b"image-payload"
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp) as mock_httpx:
        data = store.get(target_url)
        assert data == b"image-payload"
        mock_httpx.assert_called_once()


def test_cloudinary_file_store_get_urllib_fallback_on_httpx_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")
    store = CloudinaryFileStore()
    target_url = "https://res.cloudinary.com/mycloud/image/upload/v1/fallback.png"

    with patch("httpx.get", side_effect=Exception("httpx error")), \
         patch("urllib.request.urlopen") as mock_urlopen:
        mock_cm = MagicMock()
        mock_cm.read.return_value = b"urllib-payload"
        mock_urlopen.return_value.__enter__.return_value = mock_cm

        data = store.get(target_url)
        assert data == b"urllib-payload"


def test_cloudinary_file_store_get_invalid_url():
    store = CloudinaryFileStore()
    with pytest.raises(ValueError, match="Invalid Cloudinary URL reference"):
        store.get("not-a-url")


def test_cloudinary_file_store_delete(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")
    store = CloudinaryFileStore()

    with patch("cloudinary.uploader.destroy") as mock_destroy:
        store.delete("https://res.cloudinary.com/mycloud/image/upload/v1/launchgate_campaigns/abc12345.png")
        mock_destroy.assert_called_once()


# ---------------------------------------------------------------------------
# 3. UnifiedFileStore tests
# ---------------------------------------------------------------------------

def test_unified_store_unconfigured_uses_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "")
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "")

    store = UnifiedFileStore(root=tmp_path / "storage")
    payload = b"offline payload"

    ref = store.put(payload)
    # Must be 32-hex string
    assert len(ref) == 32
    assert store.get(ref) == payload
    assert store.path_for(ref) is not None
    assert store.path_for(ref).exists()

    store.delete(ref)
    assert not store.path_for(ref).exists()


def test_unified_store_configured_uses_cloudinary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")

    with patch("cloudinary.uploader.upload") as mock_upload:
        cdn_url = "https://res.cloudinary.com/mycloud/image/upload/v1234/asset.png"
        mock_upload.return_value = {"secure_url": cdn_url}

        store = UnifiedFileStore(root=tmp_path / "storage")
        payload = b"cloud payload"

        ref = store.put(payload, filename="asset.png")
        assert ref == cdn_url
        assert store.path_for(ref) is None


def test_unified_store_falls_back_to_local_on_cloudinary_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")

    with patch("cloudinary.uploader.upload", side_effect=Exception("CDN timeout")):
        store = UnifiedFileStore(root=tmp_path / "storage")
        payload = b"fallback payload"

        # Should not crash; falls back to local disk
        ref = store.put(payload, filename="failed_upload.png")
        assert len(ref) == 32
        assert store.get(ref) == payload
        assert store.path_for(ref) is not None


def test_unified_store_get_handles_remote_and_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUDINARY_URL", "")
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "")

    store = UnifiedFileStore(root=tmp_path / "storage")

    # Local object
    local_ref = store.put(b"local data")
    assert store.get(local_ref) == b"local data"

    # Remote URL object
    mock_resp = MagicMock()
    mock_resp.content = b"remote data"
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp):
        remote_data = store.get("https://res.cloudinary.com/demo/image/upload/sample.jpg")
        assert remote_data == b"remote data"


# ---------------------------------------------------------------------------
# 4. apps.api.evidence integration tests
# ---------------------------------------------------------------------------

def test_save_evidence_file_with_cloudinary(monkeypatch: pytest.MonkeyPatch):
    from apps.api.evidence import save_evidence_file

    monkeypatch.setenv("CLOUDINARY_URL", "cloudinary://123:abc@mycloud")

    cdn_url = "https://res.cloudinary.com/mycloud/image/upload/v1/evidence_review.png"
    with patch("cloudinary.uploader.upload", return_value={"secure_url": cdn_url}):
        file_ref = save_evidence_file(b"screenshot bytes", file_name="review.png")
        assert file_ref == cdn_url


def test_get_ad_evidence_file_endpoint_streaming_and_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from apps.api import auth, evidence
    from apps.api.evidence import get_ad_evidence_file_endpoint

    client_id = "client-alpha"
    ad_id = "ad-cdn-test"
    cdn_url = "https://res.cloudinary.com/mycloud/image/upload/v1/evidence.png"

    # Mock user with client access
    mock_user = auth.CurrentUser(
        user_id="reviewer-1",
        email="rev@example.com",
        display_name="Reviewer",
        role="buyer",
        assigned_client_ids=(client_id,),
    )

    # Mock ad record and session
    fake_ad = {
        "ad_id": ad_id,
        "client_id": client_id,
        "row_hash": "hash123",
    }
    fake_approval = {
        "ad_id": ad_id,
        "client_id": client_id,
        "evidence_file": cdn_url,
    }

    class MockSession:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, stmt, params=None):
            class Res:
                def fetchone(self):
                    return ("image/png",)
            return Res()

    with patch("apps.api.evidence.get_session", return_value=MockSession()), \
         patch("apps.api.evidence.get_ad_record", return_value=fake_ad), \
         patch("apps.api.evidence.get_or_create_ad_approval", return_value=fake_approval), \
         patch("apps.api.evidence.require_client_access", return_value=None), \
         patch("apps.worker.file_storage.UnifiedFileStore.get", return_value=b"png-bytes-streamed"):

        # 1. Default streaming mode (redirect=False)
        response = get_ad_evidence_file_endpoint(
            ad_id=ad_id,
            redirect=False,
            user=mock_user,
        )
        assert response.status_code == 200
        assert response.body == b"png-bytes-streamed"
        assert response.media_type == "image/png"

        # 2. Redirect mode (redirect=True)
        redirect_response = get_ad_evidence_file_endpoint(
            ad_id=ad_id,
            redirect=True,
            user=mock_user,
        )
        assert redirect_response.status_code == 307
        assert redirect_response.headers["location"] == cdn_url
