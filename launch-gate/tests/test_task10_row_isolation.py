from io import BytesIO

import pytest
from openpyxl import Workbook

from apps.api.ingest import UploadValidationError, parse_csv_upload, parse_xlsx_upload


MAX_BYTES = 1_000_000


def csv_upload(text: str):
    return parse_csv_upload("ads.csv", text.encode("utf-8"), max_file_bytes=MAX_BYTES)


def make_xlsx(rows: list[list[object]], *, second_sheet: bool = False) -> bytes:
    workbook = Workbook()
    first = workbook.active
    first.title = "First"
    for row in rows:
        first.append(row)
    if second_sheet:
        second = workbook.create_sheet("Second")
        second.append(["Platform", "Headline", "URL"])
        second.append(["ignored", "this sheet", "/ignored"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def xlsx_upload(content: bytes):
    return parse_xlsx_upload("ads.xlsx", content, max_file_bytes=MAX_BYTES)


def test_aliases_ignore_case_spaces_and_punctuation_and_allow_missing_row_number():
    result = csv_upload(
        "cHaN nEl, L i n k , Text / Headline\n"
        "mystery-platform,javascript:alert(1),=still data"
    )

    assert result.rows == [
        {
            "platform": "mystery-platform",
            "destination_url": "javascript:alert(1)",
            "ad_text": "'=still data",
            "row_number": "1",
        }
    ]
    assert result.columns[0] == "row_number"


def test_integral_float_row_number_is_normalized_and_invalid_row_isolated():
    result = csv_upload(
        "No.,Platform,Headline,URL\n"
        "7.0,Meta,Good,/good\n"
        "7.5,Meta,Bad number,/bad\n"
        " ,Meta,Blank number,/blank\n"
    )

    assert [row["row_number"] for row in result.rows] == ["7"]
    assert [item.data_row_index for item in result.rejected_rows] == [2, 3]
    assert [item.reason_code for item in result.rejected_rows] == [
        "invalid_row_number",
        "invalid_row_number",
    ]
    assert result.rejected_rows[0].row_number == "7.5"


def test_rejects_only_bad_rows_and_counts_middle_empty_record():
    result = csv_upload(
        "Row,Platform,Headline,URL\n"
        "1,Meta,Good,/good\n"
        ",,,\n"
        "2,   , ,/empty-required\n"
        "3,Meta,Good\n"
        "4,Meta,Also good,/good\n"
        " , , , \n"
    )

    assert [row["row_number"] for row in result.rows] == ["1", "4"]
    assert [(item.data_row_index, item.reason_code) for item in result.rejected_rows] == [
        (2, "empty_record"),
        (3, "missing_required_values"),
        (4, "wrong_width"),
    ]
    assert result.rejected_rows[1].reason == "required values are empty: platform, text or headline"
    # The final fully empty record is ignored and does not advance the index.


def test_wrong_width_short_and_long_csv_records_do_not_drop_valid_records():
    result = csv_upload(
        "id,Platform,Headline,URL\n"
        "1,Meta,Short\n"
        "2,Meta,Valid,/valid\n"
        "3,Meta,Long,/long,extra\n"
    )

    assert [row["row_number"] for row in result.rows] == ["2"]
    assert [item.reason_code for item in result.rejected_rows] == [
        "wrong_width",
        "wrong_width",
    ]


def test_missing_required_headers_is_a_file_level_error_with_list():
    with pytest.raises(UploadValidationError) as exc_info:
        csv_upload("Row,Channel,Headline\n1,Meta,Hello")

    assert exc_info.value.reason_code == "missing_required_columns"
    assert "link" in str(exc_info.value)


@pytest.mark.parametrize(
    "content, message",
    [
        (b"Row,Platform,Headline,URL\n1,Meta,\x00x,/page", "NUL"),
        (b"Row,Platform,Headline,URL\n1,Meta,\xff,/page", "UTF-8"),
        (b'Row,Platform,Headline,URL\n1,Meta,"unterminated,/page', "unparseable"),
    ],
)
def test_unparseable_csv_rejects_whole_file(content, message):
    with pytest.raises(UploadValidationError, match=message) as exc_info:
        parse_csv_upload("ads.csv", content, max_file_bytes=MAX_BYTES)
    assert exc_info.value.reason_code == "unparseable_csv"


def test_rejected_raw_cells_are_inert_and_truncated_to_500_characters():
    long_text = "=" + "x" * 600
    result = csv_upload(
        "Row,Platform,Headline,URL\n"
        f"bad,Meta,{long_text},/page"
    )

    rejected = result.rejected_rows[0]
    assert rejected.reason_code == "invalid_row_number"
    assert len(rejected.raw_cells[2]) == 500
    assert rejected.raw_cells[2].startswith("'=")


def test_200_limit_counts_valid_ads_only():
    header = "Row,Platform,Headline,URL"
    valid_rows = [f"{index},Meta,Ad {index},/page" for index in range(1, 201)]
    content = header + "\n" + "\n".join(["bad,Meta,No row number", *valid_rows])
    result = csv_upload(content)
    assert len(result.rows) == 200
    assert len(result.rejected_rows) == 1

    too_many = header + "\n" + "\n".join(
        f"{index},Meta,Ad {index},/page" for index in range(1, 202)
    )
    with pytest.raises(UploadValidationError) as exc_info:
        csv_upload(too_many)
    assert exc_info.value.reason_code == "too_many_ads"


def test_xlsx_isolates_empty_required_values_and_extra_nonempty_cells():
    content = make_xlsx(
        [
            ["Row", "Platform", "Headline", "URL"],
            [1, "Meta", "Good", "/good"],
            [2, "  ", "  ", "/missing"],
            [3, "Meta", "Extra", "/extra", "beyond header"],
            [4, "Meta", "Good too", "/good-too"],
        ],
    )
    result = xlsx_upload(content)

    assert [row["row_number"] for row in result.rows] == ["1", "4"]
    assert [(item.data_row_index, item.reason_code) for item in result.rejected_rows] == [
        (2, "missing_required_values"),
        (3, "wrong_width"),
    ]


def test_xlsx_empty_middle_record_is_isolated_and_trailing_empty_rows_ignored():
    content = make_xlsx(
        [
            ["Row", "Platform", "Headline", "URL"],
            [1, "Meta", "Good", "/good"],
            [None, None, None, None],
            [2, "Meta", "Good too", "/good-too"],
            [None, None, None, None],
        ]
    )
    result = xlsx_upload(content)

    assert len(result.rows) == 2
    assert [(item.data_row_index, item.reason_code) for item in result.rejected_rows] == [
        (2, "empty_record")
    ]
