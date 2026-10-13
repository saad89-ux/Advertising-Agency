"""Authenticated local upload API for CSV and XLSX launch sheets."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePath, PureWindowsPath
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile as FastAPIUploadFile
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from apps.api import ads, batches, deduplication, rejected_rows
from apps.api import outbox
from apps.api.access import require_client_access
from apps.api.auth import CurrentUser
from apps.api.guest_sessions import get_request_user, require_guest_csrf
from apps.worker.file_storage import get_file_store
from apps.api.client_inference import infer_client_id, load_domain_map
from apps.api.database import get_session
from apps.api.client_ip import get_client_ip
from apps.api.ingest import (
    MAX_REJECTED_CELL_CHARACTERS,
    ParsedCsvUpload,
    RejectedRow,
    UploadValidationError,
    hash_normalized_row,
    parse_csv_upload,
    parse_xlsx_upload,
)
from apps.api.queue import AdJob, enqueue_ad_jobs
from apps.api.settings import (
    MAX_XLSX_ARCHIVE_ENTRIES,
    max_upload_bytes,
    max_xlsx_uncompressed_bytes,
)
from packages.rules.platform_normalization import normalize_platform


router = APIRouter()
_CSV_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "text/plain",
    "application/octet-stream",
}
_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLSX_CONTENT_TYPES = {_XLSX_CONTENT_TYPE, "application/octet-stream"}


class _DuplicateUpload(Exception):
    def __init__(self, original_batch_id: str) -> None:
        self.original_batch_id = original_batch_id


@dataclass(frozen=True)
class UploadResult:
    status_code: int
    response: dict[str, Any]


def _base_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    basename = PurePath(normalized).name or PureWindowsPath(filename).name
    return basename[:255]


def _display_bytes(value: int) -> str:
    if value % (1024 * 1024) == 0:
        return f"{value // (1024 * 1024)} MiB"
    return f"{value} bytes"


def _content_type(file: StarletteUploadFile | FastAPIUploadFile) -> str:
    return (file.content_type or "").split(";", 1)[0].strip().casefold()


def _validate_file_type(file: StarletteUploadFile | FastAPIUploadFile) -> str:
    filename = file.filename or ""
    suffix = PurePath(filename.replace("\\", "/")).suffix.casefold()
    content_type = _content_type(file)
    if suffix == ".csv" and content_type in _CSV_CONTENT_TYPES:
        return "csv"
    if suffix == ".xlsx" and content_type in _XLSX_CONTENT_TYPES:
        return "xlsx"
    raise HTTPException(status_code=415, detail="unsupported file type")


def _row_as_cells(row: dict[str, str], columns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        str(row.get(column, ""))[:MAX_REJECTED_CELL_CHARACTERS]
        for column in columns
    )


def _rejected_payload(row: RejectedRow) -> dict[str, object]:
    return {
        "data_row_index": row.data_row_index,
        "row_number": None if row.row_number is None else str(row.row_number),
        "reason_code": row.reason_code,
        "reason": row.reason,
        "cells": list(row.raw_cells),
    }


def _dispatch_pending_jobs() -> None:
    try:
        outbox.dispatch_pending_jobs(enqueue_fn=enqueue_ad_jobs)
    except Exception:
        # The jobs remain durable in the outbox and can be dispatched later.
        pass


def _persist_upload(
    *,
    parsed: ParsedCsvUpload,
    user: CurrentUser,
    filename: str,
    domain_map: dict[str, str | None],
    file_ref: str | None = None,
    uploader_ip: str | None = None,
) -> UploadResult:
    known_rows_by_client: dict[str, list[tuple[int, dict[str, str]]]] = {}
    rejected = list(parsed.rejected_rows)
    for index, row in zip(parsed.valid_row_indices, parsed.rows):
        client_id = user.assigned_client_ids[0] if user.guest_session_id else infer_client_id(row.get("destination_url"), domain_map)
        if client_id is None:
            rejected.append(
                RejectedRow(
                    data_row_index=index,
                    row_number=row.get("row_number"),
                    reason_code="unknown_client_host",
                    reason="no client rules on file",
                    raw_cells=_row_as_cells(row, parsed.columns),
                )
            )
            continue
        known_rows_by_client.setdefault(client_id, []).append((index, row))

    if user.role == "buyer":
        if not user.assigned_client_ids:
            raise HTTPException(status_code=403, detail="forbidden")
        assigned_clients = set(user.assigned_client_ids)
        for client_id in tuple(known_rows_by_client):
            if client_id in assigned_clients:
                continue
            for index, row in known_rows_by_client.pop(client_id):
                rejected.append(
                    RejectedRow(
                        data_row_index=index,
                        row_number=row.get("row_number"),
                        reason_code="client_not_assigned",
                        reason="client is not assigned to the uploader",
                        raw_cells=_row_as_cells(row, parsed.columns),
                    )
                )
    else:
        for client_id in known_rows_by_client:
            require_client_access(user, client_id)
    rejected.sort(key=lambda row: row.data_row_index)

    batch_id = str(uuid4())
    base_name = _base_filename(filename)
    duplicate_clients: list[str] = []
    accepted_count = 0
    all_duplicate_batch_ids: list[str] = []
    jobs_to_enqueue: list[AdJob] = []
    unrecognized_platforms: list[dict[str, object]] = []
    for index, row in zip(parsed.valid_row_indices, parsed.rows):
        platform = normalize_platform(row.get("platform", ""))
        if platform.flagged or platform.canonical_value not in {"facebook", "instagram"}:
            unrecognized_platforms.append(
                {
                    "data_row_index": index,
                    "platform_raw": platform.raw_value,
                    "reason_code": "platform_unrecognized",
                }
            )
    _dispatch_pending_jobs()
    try:
        with get_session() as connection:
            batches.create_batch_on_connection(
                connection,
                batch_id=batch_id,
                uploaded_by=None if user.guest_session_id else user.user_id,
                filename=base_name,
                sheet_hash=parsed.sheet_hash,
                file_ref=file_ref,
                guest_session_id=user.guest_session_id,
            )
            connection.execute(
                "UPDATE batches SET uploader_ip = %s WHERE batch_id = %s;",
                (uploader_ip, batch_id),
            )
            rows_to_save: list[dict[str, object]] = []
            for client_id, indexed_rows in known_rows_by_client.items():
                prior_batch_id = deduplication.claim_sheet(
                    connection, client_id, parsed.sheet_hash, batch_id
                )
                if prior_batch_id is not None:
                    duplicate_clients.append(client_id)
                    all_duplicate_batch_ids.append(prior_batch_id)
                    continue
                for _, row in indexed_rows:
                    rows_to_save.append(
                        {
                            "ad_id": str(uuid4()),
                            "batch_id": batch_id,
                            "client_id": client_id,
                            "row_number": int(row["row_number"]),
                            "platform": normalize_platform(row["platform"]).canonical_value
                            or row["platform"],
                            "platform_raw": row["platform"],
                            "ad_text": row.get("ad_text") or None,
                            "headline": row.get("headline") or None,
                            "destination_url": row["destination_url"],
                            "creative_file": row.get("creative_file", ""),
                            "row_hash": hash_normalized_row(row),
                            "sheet_hash": parsed.sheet_hash,
                        }
                    )

            if known_rows_by_client and len(duplicate_clients) == len(known_rows_by_client):
                original_batch_id = all_duplicate_batch_ids[0]
                raise _DuplicateUpload(original_batch_id)

            ads.persist_ads_on_connection(connection, rows_to_save)
            jobs_to_enqueue = [
                AdJob(batch_id=str(row["batch_id"]), ad_id=str(row["ad_id"]))
                for row in rows_to_save
            ]
            outbox.persist_ad_jobs_on_connection(connection, jobs_to_enqueue)
            rejected_rows.persist_rejected_rows_on_connection(
                connection, batch_id, rejected
            )
            accepted_count = len(rows_to_save)
            if accepted_count == 0:
                connection.execute("DELETE FROM batches WHERE batch_id = %s;", (batch_id,))
    except _DuplicateUpload as duplicate:
        _dispatch_pending_jobs()
        return UploadResult(
            status_code=200,
            response={
                "batch_id": duplicate.original_batch_id,
                "accepted_count": 0,
                "rejected_count": 0,
                "rejected_rows": [],
                "unmapped_columns": list(parsed.unmapped_columns),
                "platform_unrecognized": unrecognized_platforms,
                "duplicate_sheet": True,
            },
        )

    _dispatch_pending_jobs()

    response: dict[str, Any] = {
        "batch_id": batch_id,
        "accepted_count": accepted_count,
        "rejected_count": len(rejected),
        "rejected_rows": [_rejected_payload(row) for row in rejected],
        "unmapped_columns": list(parsed.unmapped_columns),
        "platform_unrecognized": unrecognized_platforms,
        "duplicate_sheet": False,
        "file_ref": file_ref,
        "storage_url": file_ref,
    }
    if duplicate_clients:
        visible_clients = set(user.assigned_client_ids)
        response["skipped_duplicate_clients"] = sorted(
            client_id for client_id in duplicate_clients if client_id in visible_clients
        )
    return UploadResult(status_code=201, response=response)


@router.post("/uploads")
async def upload_batch(
    request: Request,
    user: CurrentUser = Depends(get_request_user),
):
    if user.guest_session_id:
        require_guest_csrf(request)
    elif user.role not in {"buyer", "account_manager", "admin", "owner"}:
        raise HTTPException(status_code=403, detail="insufficient role")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].casefold()
    if content_type != "multipart/form-data":
        raise HTTPException(status_code=415, detail="upload must use multipart/form-data")
    try:
        form = await request.form(max_files=2, max_fields=2)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail={
                "reason_code": "invalid_multipart",
                "message": "upload must contain exactly one file field named file",
            },
        ) from None
    items = list(form.multi_items())
    file_items = [item for item in items if isinstance(item[1], (StarletteUploadFile, FastAPIUploadFile))]
    allowed_fields = all(name in {"file", "batch_id"} for name, _ in items)
    if (
        len(file_items) != 1
        or file_items[0][0] != "file"
        or not allowed_fields
        or sum(name == "batch_id" for name, _ in items) > 1
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "reason_code": "invalid_multipart",
                "message": "upload must contain exactly one file field named file",
            },
        )
    uploaded_file = file_items[0][1]
    parser_type = _validate_file_type(uploaded_file)
    upload_limit = max_upload_bytes()
    content = await uploaded_file.read(upload_limit + 1)
    await uploaded_file.close()
    if len(content) > upload_limit:
        raise HTTPException(
            status_code=413,
            detail=f"File is larger than {_display_bytes(upload_limit)} (limit: {upload_limit} bytes)",
        )

    filename = uploaded_file.filename or ""
    file_store = get_file_store()
    stored_file_ref = file_store.put(content, resource_type="raw", filename=filename)

    try:
        if parser_type == "csv":
            parsed = parse_csv_upload(filename, content, max_file_bytes=upload_limit)
        else:
            parsed = parse_xlsx_upload(
                filename,
                content,
                max_file_bytes=upload_limit,
                max_uncompressed_bytes=max_xlsx_uncompressed_bytes(),
                max_archive_entries=MAX_XLSX_ARCHIVE_ENTRIES,
            )
    except UploadValidationError as exc:
        if exc.reason_code in {"file_too_large", "xlsx_uncompressed_too_large", "xlsx_too_many_entries"}:
            raise HTTPException(status_code=413, detail={"reason_code": exc.reason_code, "message": str(exc)}) from exc
        if exc.reason_code == "unsupported_file_type":
            raise HTTPException(status_code=415, detail={"reason_code": exc.reason_code, "message": str(exc)}) from exc
        raise HTTPException(status_code=422, detail={"reason_code": exc.reason_code, "message": str(exc)}) from exc

    uploader_ip = get_client_ip(request)
    result = _persist_upload(
        parsed=parsed,
        user=user,
        filename=filename,
        domain_map=load_domain_map(),
        file_ref=stored_file_ref,
        uploader_ip=uploader_ip,
    )
    return JSONResponse(status_code=result.status_code, content=result.response)


class UploadBodyLimitMiddleware:
    """Buffer at most the configured request limit plus one byte for the upload route."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != "/uploads" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        limit = max_upload_bytes()
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > limit:
                    await self._too_large(send, limit)
                    return
            except ValueError:
                await self._bad_request(send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await self._too_large(send, limit)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _too_large(send, limit: int) -> None:
        message = json.dumps(
            {"detail": f"File is larger than {_display_bytes(limit)} (limit: {limit} bytes)"}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(message)).encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": message})

    @staticmethod
    async def _bad_request(send) -> None:
        message = b'{"detail":"invalid content length"}'
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(message)).encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": message})


SUPPORTED_CREATIVE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".pdf"}
SUPPORTED_CREATIVE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "video/mp4",
    "application/pdf",
}


@router.post("/uploads/creative")
async def upload_creative(
    request: Request,
    file: FastAPIUploadFile = File(...),
    client_id: str | None = Form(None),
    user: CurrentUser = Depends(get_request_user),
):
    """Upload a single creative asset (image, video, PDF) to persistent Cloudinary / unified storage."""
    filename = file.filename or "creative_asset"
    ext = PurePath(filename.replace("\\", "/")).suffix.casefold()
    content_type = (file.content_type or "").split(";", 1)[0].strip().casefold()

    if ext not in SUPPORTED_CREATIVE_EXTENSIONS and content_type not in SUPPORTED_CREATIVE_MIMES:
        raise HTTPException(
            status_code=415,
            detail={
                "reason_code": "unsupported_file_type",
                "message": "Unsupported creative file type. Supported formats are: PNG, JPG, WEBP, MP4, PDF.",
            },
        )

    if not client_id:
        raise HTTPException(status_code=422, detail="client_id is required for a creative upload.")
    require_client_access(user, client_id)
    if user.guest_session_id:
        require_guest_csrf(request)

    upload_limit = max_upload_bytes() * 10
    content = await file.read(upload_limit + 1)
    await file.close()

    if len(content) > upload_limit:
        raise HTTPException(
            status_code=413,
            detail=f"File is larger than {_display_bytes(upload_limit)} (limit: {upload_limit} bytes)",
        )

    file_store = get_file_store()
    stored_ref = file_store.put(content, resource_type="auto", filename=filename)

    return {
        "status": "uploaded",
        "file_ref": stored_ref,
        "url": stored_ref,
        "filename": filename,
        "size_bytes": len(content),
    }
