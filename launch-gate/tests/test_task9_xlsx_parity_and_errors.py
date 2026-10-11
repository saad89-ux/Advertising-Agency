from io import BytesIO
import zipfile

import pytest
from openpyxl import Workbook

from apps.api.ingest import (
    UploadValidationError,
    parse_csv_upload,
    parse_xlsx_upload,
)


MAX_BYTES = 1_000_000


def make_xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Row", "Platform", "Ad text", "Headline", "URL", "Creative file"])
    sheet.append([1, "Meta", "Primary copy", "Title", "https://example.test", None])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def make_macro_xlsx() -> bytes:
    source_bytes = make_xlsx()
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(source_bytes)) as source, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr("xl/vbaProject.bin", b"macro marker")
    return output.getvalue()


def test_csv_and_xlsx_normalization_and_hashes_match_for_same_sheet():
    csv_result = parse_csv_upload(
        "ads.csv",
        (
            "Row,Platform,Ad text,Headline,URL,Creative file\n"
            "1,Meta,Primary copy,Title,https://example.test,\n"
        ).encode("utf-8"),
        max_file_bytes=MAX_BYTES,
    )
    xlsx_result = parse_xlsx_upload(
        "ads.xlsx", make_xlsx(), max_file_bytes=MAX_BYTES
    )

    assert xlsx_result.rows == csv_result.rows
    assert xlsx_result.rows[0]["creative_file"] == ""
    assert xlsx_result.row_hashes == csv_result.row_hashes
    assert xlsx_result.sheet_hash == csv_result.sheet_hash


def test_macro_enabled_xlsx_error_has_actionable_message_and_reason_code():
    for filename, content in (
        ("ads.xlsm", make_xlsx()),
        ("ads.xlsx", make_macro_xlsx()),
    ):
        with pytest.raises(UploadValidationError) as exc_info:
            parse_xlsx_upload(filename, content, max_file_bytes=MAX_BYTES)

        assert "non-macro XLSX" in str(exc_info.value)
        assert exc_info.value.reason_code == "macro_enabled_xlsx"


def test_corrupt_xlsx_error_has_actionable_message_and_reason_code():
    with pytest.raises(UploadValidationError) as exc_info:
        parse_xlsx_upload("ads.xlsx", b"not a workbook", max_file_bytes=MAX_BYTES)

    assert "valid XLSX" in str(exc_info.value)
    assert exc_info.value.reason_code == "unreadable_xlsx"


def test_non_xlsx_extension_error_recommends_valid_xlsx():
    with pytest.raises(UploadValidationError) as exc_info:
        parse_xlsx_upload("ads.csv", b"not a workbook", max_file_bytes=MAX_BYTES)

    assert "valid XLSX" in str(exc_info.value)
    assert exc_info.value.reason_code == "unsupported_file_type"
