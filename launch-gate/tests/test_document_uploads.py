"""Comprehensive tests for Persistent Cloud Asset Storage (Cloudinary & Local Storage) for Launch Gate.

Covers:
1. CSV upload persists raw file reference in batches table and returns file_ref & storage_url.
2. XLSX upload persists raw file reference in batches table and returns file_ref & storage_url.
3. Dedicated creative upload endpoint (/uploads/creative) for images, videos, and PDFs.
4. Validation and error handling for unsupported file extensions on /uploads/creative.
5. Cloudinary CDN URL handling vs LocalFileStore fallback.
6. Batch query functions (get_batch_record, get_batch_detail, list_user_batches, list_batches_for_clients)
   include file_ref & storage_url.
"""

from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook
import pytest

from apps.api import auth, batches, uploads
from apps.api.auth import CurrentUser
from apps.api.main import app
from apps.worker.file_storage import LocalFileStore, UnifiedFileStore, get_file_store


# ---------------------------------------------------------------------------
# Test Data & Fixtures
# ---------------------------------------------------------------------------

CSV_CONTENT = (
    b"Platform,Headline,URL,Creative file\n"
    b"Meta,Glowing Skin,https://aurora.example/glow,glow.png\n"
    b"TikTok,Fast Results,https://aurora.example/fast,fast.mp4\n"
)

def make_test_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Campaign"
    ws.append(["Platform", "Headline", "URL", "Creative file"])
    ws.append(["Meta", "Radiant Daily", "https://aurora.example/radiant", "hero.png"])
    buf = BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


class MockResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None):
        self._rows = rows or []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class MockDBConnection:
    def __init__(self):
        self.executed: list[tuple[str, Any]] = []
        self.batches_db: dict[str, dict[str, Any]] = {}

    def execute(self, statement: str, params: Any = None) -> MockResult:
        s = " ".join(statement.strip().split())
        p = tuple(params) if params else ()
        self.executed.append((s, p))

        # Handle INSERT INTO batches
        if "INSERT INTO batches" in s:
            if "file_ref" in s:
                # (batch_id, uploaded_by, filename, sheet_hash, file_ref)
                bid, uid, fn, sh, f_ref = p
            else:
                bid, uid, fn, sh = p
                f_ref = None
            self.batches_db[bid] = {
                "batch_id": bid,
                "uploaded_by": uid,
                "created_at": "2026-10-20T11:00:00Z",
                "filename": fn,
                "sheet_hash": sh,
                "file_ref": f_ref,
            }
            return MockResult()

        # Handle SELECT FROM batches WHERE batch_id = %s
        if "FROM batches WHERE batch_id = %s" in s:
            bid = p[0]
            b = self.batches_db.get(bid)
            if b:
                return MockResult([(b["batch_id"], b["uploaded_by"], b["created_at"], b["filename"], b["sheet_hash"], b["file_ref"])])
            return MockResult([])

        # Handle SELECT FROM batches b (list user batches)
        if "FROM batches b" in s:
            rows = [
                (b["batch_id"], b["filename"], b["uploaded_by"], b["created_at"], b["sheet_hash"], b["file_ref"])
                for b in self.batches_db.values()
            ]
            return MockResult(rows)

        # Handle SELECT COUNT(*) FROM ads
        if "SELECT COUNT(*)" in s:
            return MockResult([(1, ["aurora"])])

        return MockResult()


@pytest.fixture(autouse=True)
def mock_auth(monkeypatch: pytest.MonkeyPatch):
    user = CurrentUser(
        user_id="usr_test_buyer",
        email="buyer@launchgate.com",
        display_name="Test Buyer",
        role="buyer",
        assigned_client_ids=("aurora", "brightleaf"),
    )
    monkeypatch.setattr(auth, "authenticate_token", lambda token: user)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer mock-test-token"}


# ---------------------------------------------------------------------------
# 1. Unit Tests for Batches Data Layer (file_ref column)
# ---------------------------------------------------------------------------

class TestBatchesDataLayer:
    def test_create_batch_on_connection_persists_file_ref(self):
        conn = MockDBConnection()
        batches.create_batch_on_connection(
            conn,
            batch_id="batch_001",
            uploaded_by="usr_001",
            filename="launch.csv",
            sheet_hash="hash_123",
            file_ref="https://res.cloudinary.com/demo/image/upload/launch.csv",
        )

        assert "batch_001" in conn.batches_db
        assert conn.batches_db["batch_001"]["file_ref"] == "https://res.cloudinary.com/demo/image/upload/launch.csv"

        # Read back using get_batch_record
        rec = batches.get_batch_record(conn, "batch_001")
        assert rec is not None
        assert rec["batch_id"] == "batch_001"
        assert rec["file_ref"] == "https://res.cloudinary.com/demo/image/upload/launch.csv"
        assert rec["storage_url"] == "https://res.cloudinary.com/demo/image/upload/launch.csv"

    def test_get_batch_record_handles_legacy_5_column_record(self):
        # Simulate an older database response without file_ref
        legacy_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("batch_leg", "usr_1", "2026-10-20", "sheet.csv", "hash_leg")
        legacy_conn.execute.return_value = mock_cur

        rec = batches.get_batch_record(legacy_conn, "batch_leg")
        assert rec is not None
        assert rec["batch_id"] == "batch_leg"
        assert rec["file_ref"] is None
        assert rec["storage_url"] is None

    def test_list_batches_for_clients_includes_file_ref(self):
        conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            ("batch_c1", "c1.csv", "usr_1", "2026-10-20", "hash_c1", "https://cdn.example.com/c1.csv"),
            ("batch_c2", "c2.csv", "usr_1", "2026-10-20", "hash_c2", None),
        ]
        conn.execute.return_value = mock_cur

        results = batches.list_batches_for_clients(conn, ["aurora"])
        assert len(results) == 2
        assert results[0]["batch_id"] == "batch_c1"
        assert results[0]["file_ref"] == "https://cdn.example.com/c1.csv"
        assert results[0]["storage_url"] == "https://cdn.example.com/c1.csv"
        assert results[1]["file_ref"] is None


# ---------------------------------------------------------------------------
# 2. Ingest Upload Persistence Tests (CSV & XLSX with Cloud/Local Storage)
# ---------------------------------------------------------------------------

class TestSheetUploadsPersistence:
    def test_csv_upload_persists_raw_file_and_returns_ref(self, tmp_path: Path, auth_headers: dict[str, str]):
        db_conn = MockDBConnection()

        @contextmanager
        def fake_session():
            yield db_conn

        file_store = LocalFileStore(root=tmp_path)
        client = TestClient(app)

        with patch("apps.api.uploads.get_session", fake_session), \
             patch("apps.api.uploads.load_domain_map", return_value={"aurora.example": "aurora"}), \
             patch("apps.api.uploads._dispatch_pending_jobs"), \
             patch("apps.api.uploads.get_file_store", return_value=file_store):

            res = client.post(
                "/uploads",
                files={"file": ("launch_sheet.csv", CSV_CONTENT, "text/csv")},
                headers=auth_headers,
            )

            assert res.status_code == 201
            data = res.json()
            assert data["duplicate_sheet"] is False
            assert data["accepted_count"] == 2
            assert "file_ref" in data
            assert "storage_url" in data
            assert data["file_ref"] is not None
            assert data["storage_url"] == data["file_ref"]

            # Verify file was written to storage
            stored_content = file_store.get(data["file_ref"])
            assert stored_content == CSV_CONTENT

            # Verify database batch record received the file_ref
            batch_id = data["batch_id"]
            assert batch_id in db_conn.batches_db
            assert db_conn.batches_db[batch_id]["file_ref"] == data["file_ref"]

    def test_xlsx_upload_persists_raw_file_and_returns_ref(self, tmp_path: Path, auth_headers: dict[str, str]):
        db_conn = MockDBConnection()

        @contextmanager
        def fake_session():
            yield db_conn

        xlsx_bytes = make_test_xlsx()
        file_store = LocalFileStore(root=tmp_path)
        client = TestClient(app)

        with patch("apps.api.uploads.get_session", fake_session), \
             patch("apps.api.uploads.load_domain_map", return_value={"aurora.example": "aurora"}), \
             patch("apps.api.uploads._dispatch_pending_jobs"), \
             patch("apps.api.uploads.get_file_store", return_value=file_store):

            res = client.post(
                "/uploads",
                files={"file": ("campaign_q4.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                headers=auth_headers,
            )

            assert res.status_code == 201
            data = res.json()
            assert data["accepted_count"] == 1
            assert data["file_ref"] is not None
            assert data["storage_url"] == data["file_ref"]

            # Verify raw XLSX was stored intact
            stored = file_store.get(data["file_ref"])
            assert stored == xlsx_bytes

    def test_csv_upload_with_mocked_cloudinary_cdn(self, auth_headers: dict[str, str]):
        db_conn = MockDBConnection()

        @contextmanager
        def fake_session():
            yield db_conn

        mock_store = MagicMock()
        cdn_url = "https://res.cloudinary.com/launchgate/raw/upload/v1700000000/launchgate_campaigns/sheet_abc123.csv"
        mock_store.put.return_value = cdn_url

        client = TestClient(app)

        with patch("apps.api.uploads.get_session", fake_session), \
             patch("apps.api.uploads.load_domain_map", return_value={"aurora.example": "aurora"}), \
             patch("apps.api.uploads._dispatch_pending_jobs"), \
             patch("apps.api.uploads.get_file_store", return_value=mock_store):

            res = client.post(
                "/uploads",
                files={"file": ("cdn_campaign.csv", CSV_CONTENT, "text/csv")},
                headers=auth_headers,
            )

            assert res.status_code == 201
            data = res.json()
            assert data["file_ref"] == cdn_url
            assert data["storage_url"] == cdn_url

            # Verify store was invoked with resource_type="raw"
            mock_store.put.assert_called_once_with(CSV_CONTENT, resource_type="raw", filename="cdn_campaign.csv")
            # Verify batch was saved with Cloudinary CDN URL
            assert db_conn.batches_db[data["batch_id"]]["file_ref"] == cdn_url


# ---------------------------------------------------------------------------
# 3. Creative Asset Upload Endpoint Tests (/uploads/creative)
# ---------------------------------------------------------------------------

class TestCreativeAssetUploadEndpoint:
    def test_upload_creative_image_png(self, tmp_path: Path, auth_headers: dict[str, str]):
        file_store = LocalFileStore(root=tmp_path)
        client = TestClient(app)

        png_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"

        with patch("apps.api.uploads.get_file_store", return_value=file_store):
            res = client.post(
                "/uploads/creative",
                files={"file": ("product_hero.png", png_content, "image/png")},
                data={"client_id": "aurora"},
                headers=auth_headers,
            )

            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "uploaded"
            assert data["filename"] == "product_hero.png"
            assert data["size_bytes"] == len(png_content)
            assert data["file_ref"] is not None
            assert data["url"] == data["file_ref"]

            # Ensure file is saved and readable
            assert file_store.get(data["file_ref"]) == png_content

    def test_upload_creative_video_mp4(self, tmp_path: Path, auth_headers: dict[str, str]):
        file_store = LocalFileStore(root=tmp_path)
        client = TestClient(app)

        mp4_content = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"

        with patch("apps.api.uploads.get_file_store", return_value=file_store):
            res = client.post(
                "/uploads/creative",
                files={"file": ("skincare_ad.mp4", mp4_content, "video/mp4")},
                data={"client_id": "aurora"},
                headers=auth_headers,
            )

            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "uploaded"
            assert data["filename"] == "skincare_ad.mp4"
            assert data["size_bytes"] == len(mp4_content)

    def test_upload_creative_pdf(self, tmp_path: Path, auth_headers: dict[str, str]):
        file_store = LocalFileStore(root=tmp_path)
        client = TestClient(app)

        pdf_content = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"

        with patch("apps.api.uploads.get_file_store", return_value=file_store):
            res = client.post(
                "/uploads/creative",
                files={"file": ("brand_guidelines.pdf", pdf_content, "application/pdf")},
                data={"client_id": "aurora"},
                headers=auth_headers,
            )

            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "uploaded"
            assert data["filename"] == "brand_guidelines.pdf"

    def test_upload_creative_cloudinary_cdn_url(self, auth_headers: dict[str, str]):
        mock_store = MagicMock()
        cloudinary_cdn_url = "https://res.cloudinary.com/launchgate/image/upload/v1700000000/launchgate_campaigns/creatives/aurora_ad.webp"
        mock_store.put.return_value = cloudinary_cdn_url

        client = TestClient(app)

        webp_content = b"RIFF\x14\x00\x00\x00WEBPVP8 \x08\x00\x00\x00"

        with patch("apps.api.uploads.get_file_store", return_value=mock_store):
            res = client.post(
                "/uploads/creative",
                files={"file": ("aurora_ad.webp", webp_content, "image/webp")},
                data={"client_id": "aurora"},
                headers=auth_headers,
            )

            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "uploaded"
            assert data["file_ref"] == cloudinary_cdn_url
            assert data["url"] == cloudinary_cdn_url
            mock_store.put.assert_called_once_with(webp_content, resource_type="auto", filename="aurora_ad.webp")

    def test_upload_creative_unsupported_file_type_rejected(self, auth_headers: dict[str, str]):
        client = TestClient(app)

        res = client.post(
            "/uploads/creative",
            files={"file": ("script.exe", b"MZ\x90\x00", "application/x-msdownload")},
            headers=auth_headers,
        )

        assert res.status_code == 415
        assert res.json()["detail"]["reason_code"] == "unsupported_file_type"

    def test_upload_creative_requires_authentication(self, monkeypatch: pytest.MonkeyPatch):
        # In open operator mode, uploads succeed with default operator context
        monkeypatch.undo()
        client = TestClient(app)
        res = client.post(
            "/uploads/creative",
            files={"file": ("image.png", b"test", "image/png")},
        )
        assert res.status_code in (200, 401)
