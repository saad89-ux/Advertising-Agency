"""Environment-backed limits used by local API requests."""

from __future__ import annotations

import os

from dotenv import load_dotenv


DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_XLSX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_XLSX_ARCHIVE_ENTRIES = 200


def _positive_int(name: str, default: int) -> int:
    load_dotenv()
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def max_upload_bytes() -> int:
    return _positive_int("MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)


def max_xlsx_uncompressed_bytes() -> int:
    return _positive_int(
        "MAX_XLSX_UNCOMPRESSED_BYTES", DEFAULT_MAX_XLSX_UNCOMPRESSED_BYTES
    )
