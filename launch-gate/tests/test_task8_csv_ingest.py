import pytest

from apps.api.ingest import (
    MAX_CSV_ROWS,
    ParsedCsvUpload,
    UploadValidationError,
    parse_csv_upload,
)


MAX_BYTES = 20_000


def test_normalizes_known_headers_and_preserves_csv_values():
    result = parse_csv_upload(
        "ads.CSV",
        (
            "Row,Platform ,Text / Headline,Destination,File,Extra Column\n"
            '7,Meta,"Text, with comma",https://example.test,asset.png,kept\n'
        ).encode(),
        max_file_bytes=MAX_BYTES,
    )

    assert isinstance(result, ParsedCsvUpload)
    assert result.columns == (
        "row_number",
        "platform",
        "ad_text",
        "destination_url",
        "creative_file",
        "extra_column",
    )
    assert result.rows == [
        {
            "row_number": "7",
            "platform": "Meta",
            "ad_text": "Text, with comma",
            "destination_url": "https://example.test",
            "creative_file": "asset.png",
            "extra_column": "kept",
        }
    ]


def test_supports_split_text_and_headline_headers_and_utf8_bom():
    content = "\ufeff#,Channel,Primary Text,Headline,URL,Asset\n1,Search,hello,title,/page,a.png".encode()
    result = parse_csv_upload("ads.csv", content, max_file_bytes=MAX_BYTES)

    assert result.rows[0]["row_number"] == "1"
    assert result.rows[0]["platform"] == "Search"
    assert result.rows[0]["ad_text"] == "hello"
    assert result.rows[0]["headline"] == "title"
    assert result.rows[0]["destination_url"] == "/page"


@pytest.mark.parametrize("formula", ["=1+1", "+SUM(A1)", "-1+1", "@SUM(A1)", "  =1+1", "\t@SUM(A1)"])
def test_neutralizes_formula_like_cells(formula):
    content = f"Row,Platform,Headline,URL\n1,Meta,{formula},/page".encode()
    result = parse_csv_upload("ads.csv", content, max_file_bytes=MAX_BYTES)

    assert result.rows[0]["headline"] == "'" + formula


def test_accepts_exact_byte_limit_and_rejects_larger_upload():
    content = b"Row,Platform,Headline,URL\n1,Meta,Hello,/page"
    assert parse_csv_upload(
        "ads.csv", content, max_file_bytes=len(content)
    ).rows

    with pytest.raises(UploadValidationError, match="byte limit"):
        parse_csv_upload("ads.csv", content, max_file_bytes=len(content) - 1)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_requires_positive_integer_byte_limit(limit):
    with pytest.raises(UploadValidationError, match="positive integer"):
        parse_csv_upload("ads.csv", b"", max_file_bytes=limit)


def test_rejects_non_csv_extension_and_invalid_utf8():
    with pytest.raises(UploadValidationError, match="only CSV"):
        parse_csv_upload("ads.xlsx", b"", max_file_bytes=MAX_BYTES)
    with pytest.raises(UploadValidationError, match="UTF-8"):
        parse_csv_upload("ads.csv", b"\xff", max_file_bytes=MAX_BYTES)


def test_rejects_missing_or_duplicate_normalized_columns():
    with pytest.raises(UploadValidationError, match="missing required columns"):
        parse_csv_upload(
            "ads.csv",
            b"Row,Platform,Headline\n1,Meta,Hello",
            max_file_bytes=MAX_BYTES,
        )
    with pytest.raises(UploadValidationError, match="duplicate normalized"):
        parse_csv_upload(
            "ads.csv",
            b"Row,Platform,Channel,Headline,URL\n1,Meta,Search,Hello,/page",
            max_file_bytes=MAX_BYTES,
        )


def test_enforces_row_limit_and_ignores_physical_blank_lines():
    header = "Row,Platform,Headline,URL"
    row = "1,Meta,Hello,/page"
    content = (header + "\n" + "\n".join([row] * MAX_CSV_ROWS)).encode()
    result = parse_csv_upload("ads.csv", content, max_file_bytes=MAX_BYTES)
    assert len(result.rows) == MAX_CSV_ROWS

    too_many = (header + "\n" + "\n".join([row] * (MAX_CSV_ROWS + 1))).encode()
    with pytest.raises(UploadValidationError, match="at most 200"):
        parse_csv_upload("ads.csv", too_many, max_file_bytes=MAX_BYTES)

    blank_lines = (header + "\n\n" + row + "\n\n").encode()
    assert len(parse_csv_upload("ads.csv", blank_lines, max_file_bytes=MAX_BYTES).rows) == 1


def test_isolates_records_with_wrong_column_count():
    result = parse_csv_upload(
        "ads.csv",
        b"Row,Platform,Headline,URL\n1,Meta,Hello\n2,Meta,Also hello,/page",
        max_file_bytes=MAX_BYTES,
    )

    assert len(result.rows) == 1
    assert result.rows[0]["row_number"] == "2"
    assert result.rejected_rows[0].reason_code == "wrong_width"
