"""Authenticated local upload API for CSV and XLSX launch sheets."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePath, PureWindowsPath
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from apps.api import ads, batches, deduplication, rejected_rows
from apps.api.access import require_client_access
from apps.api.auth import CurrentUser, require_role
from apps.api.client_inference import infer_client_id, load_domain_map
from apps.api.database import get_session
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


router = APIRouter()
_CSV_CONTENT_TYPES = {"text/csv", "application/csv"}
_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLSM_CONTENT_TYPE = "application/vnd.ms-excel.sheet.macroenabled.12"


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


def _content_type(file: UploadFile) -> str:
    return (file.content_type or "").split(";", 1)[0].strip().casefold()


def _validate_file_type(file: UploadFile) -> str:
    filename = file.filename or ""
    suffix = PurePath(filename.replace("\\", "/")).suffix.casefold()
    content_type = _content_type(file)
    if suffix == ".csv" and content_type in _CSV_CONTENT_TYPES:
        return "csv"
    if suffix == ".xlsx" and content_type == _XLSX_CONTENT_TYPE:
        return "xlsx"
    if suffix == ".xlsm" and content_type == _XLSM_CONTENT_TYPE:
        return "xlsm"
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


def _insert_upload_schema() -> None:
    from apps.api.clients import create_client_tables

    create_client_tables()
    batches.create_batches_table()
    ads.create_ads_table()
    deduplication.create_deduplication_constraints()
    rejected_rows.create_rejected_rows_table()


def _persist_upload(
    *,
    parsed: ParsedCsvUpload,
    user: CurrentUser,
    filename: str,
    domain_map: dict[str, str | None],
) -> UploadResult:
    known_rows_by_client: dict[str, list[tuple[int, dict[str, str]]]] = {}
    rejected = list(parsed.rejected_rows)
    for index, row in zip(parsed.valid_row_indices, parsed.rows):
        client_id = infer_client_id(row.get("destination_url"), domain_map)
        if client_id is None:
            rejected.append(
                RejectedRow(
                    data_row_index=index,
                    row_number=row.get("row_number"),
                    reason_code="unknown_client",
                    reason="no client rules on file",
                    raw_cells=_row_as_cells(row, parsed.columns),
                )
            )
            continue
        known_rows_by_client.setdefault(client_id, []).append((index, row))
    rejected.sort(key=lambda row: row.data_row_index)

    if user.role == "buyer":
        if not user.assigned_client_ids:
            raise HTTPException(status_code=403, detail="forbidden")
        if any(client_id not in user.assigned_client_ids for client_id in known_rows_by_client):
            raise HTTPException(status_code=403, detail="forbidden")
    else:
        for client_id in known_rows_by_client:
            require_client_access(user, client_id)

    batch_id = str(uuid4())
    base_name = _base_filename(filename)
    duplicate_clients: list[str] = []
    accepted_count = 0
    all_duplicate_batch_ids: list[str] = []
    jobs_to_enqueue: list[AdJob] = []
    _insert_upload_schema()
    try:
        with get_session() as connection:
            batches.create_batch_on_connection(
                connection,
                batch_id=batch_id,
                uploaded_by=user.user_id,
                filename=base_name,
                sheet_hash=parsed.sheet_hash,
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
                            "platform": row["platform"],
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
            rejected_rows.persist_rejected_rows_on_connection(
                connection, batch_id, rejected
            )
            accepted_count = len(rows_to_save)
    except _DuplicateUpload as duplicate:
        return UploadResult(
            status_code=200,
            response={
                "batch_id": duplicate.original_batch_id,
                "accepted_count": 0,
                "rejected_count": 0,
                "rejected_rows": [],
                "duplicate_sheet": True,
            },
        )

    if jobs_to_enqueue:
        try:
            enqueue_ad_jobs(jobs_to_enqueue)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="ad queue unavailable") from exc

    response: dict[str, Any] = {
        "batch_id": batch_id,
        "accepted_count": accepted_count,
        "rejected_count": len(rejected),
        "rejected_rows": [_rejected_payload(row) for row in rejected],
        "duplicate_sheet": False,
    }
    if duplicate_clients:
        visible_clients = set(user.assigned_client_ids)
        response["skipped_duplicate_clients"] = sorted(
            client_id for client_id in duplicate_clients if client_id in visible_clients
        )
    return UploadResult(status_code=201, response=response)


@router.post("/batches/upload")
async def upload_batch(
    request: Request,
    user: CurrentUser = Depends(require_role("buyer")),
):
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
    file_items = [item for item in items if isinstance(item[1], UploadFile)]
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

    result = _persist_upload(
        parsed=parsed,
        user=user,
        filename=filename,
        domain_map=load_domain_map(),
    )
    return JSONResponse(status_code=result.status_code, content=result.response)


class UploadBodyLimitMiddleware:
    """Buffer at most the configured request limit plus one byte for the upload route."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != "/batches/upload" or scope.get("method") != "POST":
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
