from contextlib import contextmanager
import json
from pathlib import Path

from apps.api import uploads
from apps.api.auth import CurrentUser
from apps.api.ingest import hash_normalized_row, hash_normalized_sheet, parse_csv_upload
from apps.api import schema


ROOT = Path(__file__).resolve().parents[1]
DEV_SHEETS = (1, 2, 3, 4, 6, 7, 9, 11, 12, 14, 16, 18, 19, 21, 23, 24, 26, 27, 28, 29)


def test_all_dev_header_styles_map_platform_text_link_and_creative():
    for number in DEV_SHEETS:
        label_path = ROOT / "data" / "labels" / f"sheet_{number:02d}.labels.json"
        label = json.loads(label_path.read_text(encoding="utf-8"))
        headers = label["header_variant"]
        content = (",".join(headers) + "\n" + ",".join("value" for _ in headers)).encode()
        parsed = parse_csv_upload(
            f"sheet_{number:02d}.csv", content, max_file_bytes=max(1, len(content))
        )
        columns = set(parsed.columns)
        assert "platform" in columns, label["sheet"]
        assert "destination_url" in columns, label["sheet"]
        assert "creative_file" in columns, label["sheet"]
        assert columns.intersection({"ad_text", "headline"}), label["sheet"]


def test_unknown_header_is_reported_in_upload_response(monkeypatch):
    content = (
        b"Platform,Headline,URL,Creative file,Foo\n"
        b"Meta,hello,https://brightleaf.example/page,asset.png,extra\n"
    )
    parsed = parse_csv_upload("ads.csv", content, max_file_bytes=len(content))

    @contextmanager
    def fake_session():
        class Result:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class Connection:
            def execute(self, statement, params=None):
                if "INSERT INTO sheet_claims" in statement:
                    return Result((params[2],))
                return Result()

        yield Connection()

    monkeypatch.setattr(uploads, "get_session", fake_session)
    monkeypatch.setattr(uploads, "_dispatch_pending_jobs", lambda: None)
    result = uploads._persist_upload(
        parsed=parsed,
        user=CurrentUser("buyer", "buyer@example.test", "Buyer", "buyer", ("brightleaf",)),
        filename="ads.csv",
        domain_map={"brightleaf.example": "brightleaf"},
    )

    assert parsed.unmapped_columns == ("Foo",)
    assert result.response["unmapped_columns"] == ["Foo"]


def test_sheet_06_empty_creative_count_matches_dev_labels():
    label_path = ROOT / "data" / "labels" / "sheet_06.labels.json"
    label = json.loads(label_path.read_text(encoding="utf-8"))
    sheet_path = ROOT / "data" / "labeled_sheets" / label["sheet"]
    parsed = parse_csv_upload(
        sheet_path.name,
        sheet_path.read_bytes(),
        max_file_bytes=max(1, sheet_path.stat().st_size),
    )

    actual_empty = sum(not row.get("creative_file", "").strip() for row in parsed.rows)
    expected_empty = sum(
        not row.get("truth", {}).get("creative", "").strip()
        for row in label["rows"]
        if row.get("malformed") is not True
    )
    assert actual_empty == expected_empty


def test_platform_aliases_have_the_same_canonical_hash():
    meta = {"platform": "Meta", "ad_text": "copy", "headline": "title", "destination_url": "/p", "creative_file": "a.png"}
    fb = {**meta, "platform": "FB"}
    assert hash_normalized_row(meta) == hash_normalized_row(fb)
    assert hash_normalized_sheet([meta]) == hash_normalized_sheet([fb])


def test_upload_stores_canonical_and_raw_platforms_and_flags_unknown(monkeypatch):
    content = (
        b"Platform,Headline,URL,Creative file\n"
        b"Meta,first,https://brightleaf.example/one,one.png\n"
        b"Snapchat,second,https://brightleaf.example/two,two.png\n"
    )
    parsed = parse_csv_upload("ads.csv", content, max_file_bytes=len(content))
    inserted_ads = []

    @contextmanager
    def fake_session():
        class Result:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class Connection:
            def execute(self, statement, params=None):
                if "INSERT INTO sheet_claims" in statement:
                    return Result((params[2],))
                if "INSERT INTO ads" in statement:
                    inserted_ads.append(params)
                return Result()

        yield Connection()

    monkeypatch.setattr(uploads, "get_session", fake_session)
    monkeypatch.setattr(uploads, "_dispatch_pending_jobs", lambda: None)
    result = uploads._persist_upload(
        parsed=parsed,
        user=CurrentUser("buyer", "buyer@example.test", "Buyer", "buyer", ("brightleaf",)),
        filename="ads.csv",
        domain_map={"brightleaf.example": "brightleaf"},
    )

    assert [(ad[4], ad[5]) for ad in inserted_ads] == [
        ("facebook", "Meta"),
        ("Snapchat", "Snapchat"),
    ]
    assert result.response["platform_unrecognized"] == [
        {"data_row_index": 2, "platform_raw": "Snapchat", "reason_code": "platform_unrecognized"}
    ]


def test_init_schema_runs_platform_raw_migration(monkeypatch):
    called = []
    monkeypatch.setattr(schema.clients, "create_client_tables", lambda: called.append("clients"))
    monkeypatch.setattr(schema.batches, "create_batches_table", lambda: called.append("batches"))
    monkeypatch.setattr(schema.ads, "create_ads_table", lambda: called.append("ads"))
    monkeypatch.setattr(schema.ads, "ensure_platform_raw_column", lambda: called.append("platform_raw"))
    monkeypatch.setattr(schema.outbox, "create_ad_job_outbox_table", lambda: called.append("outbox"))
    monkeypatch.setattr(schema.deduplication, "create_deduplication_constraints", lambda: called.append("dedup"))
    monkeypatch.setattr(schema.rejected_rows, "create_rejected_rows_table", lambda: called.append("rejected"))
    monkeypatch.setattr(schema.findings, "create_findings_table", lambda: called.append("findings"))

    schema.init_schema()

    assert called.index("platform_raw") == called.index("ads") + 1


def test_mechanical_replay_dev_split_scores_perfect_recall():
    from evals.mechanical_replay import run_mechanical_replay
    import pytest

    results = run_mechanical_replay(split="dev")
    assert results["label"] == "SYNTHETIC"
    assert results["sheets_evaluated"] == 20
    assert results["total_valid_rows"] == 2200
    for etype, data in results["recall_by_type"].items():
        assert data["recall"] == 1.0, f"Recall for {etype} was not 1.0: {data}"
    assert results["false_alarm_rate"] < 0.05

    # held_out without final flag must raise ValueError
    with pytest.raises(ValueError, match="requires --final"):
        run_mechanical_replay(split="held_out", is_final=False)
