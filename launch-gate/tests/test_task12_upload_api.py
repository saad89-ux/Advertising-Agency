import asyncio
from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
import zipfile
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from apps.api import auth, main, settings, uploads
from apps.api.auth import CurrentUser
from apps.api.queue import AdJob
from apps.api.main import app


CSV_MIME = "text/csv"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeUploadDatabase:
    def __init__(self):
        self.state = {"batches": {}, "claims": {}, "ads": [], "rejected": []}
        self.outbox_jobs = []
        self.queued_outbox_jobs = set()

    @contextmanager
    def session(self):
        pending = deepcopy(self.state)
        pending_outbox_jobs = deepcopy(self.outbox_jobs)

        class Connection:
            def execute(inner, statement, params=None):
                lowered = statement.casefold()
                if "insert into batches" in lowered:
                    pending["batches"][params[0]] = params
                    return FakeResult()
                if "insert into sheet_claims" in lowered:
                    key = (params[0], params[1])
                    if key in pending["claims"]:
                        return FakeResult()
                    pending["claims"][key] = params[2]
                    return FakeResult((params[2],))
                if "select batch_id from sheet_claims" in lowered:
                    key = (params[0], params[1])
                    value = pending["claims"].get(key)
                    return FakeResult((value,) if value else None)
                if "insert into ads" in lowered:
                    pending["ads"].append(params)
                    return FakeResult()
                if "insert into batch_rejected_rows" in lowered:
                    pending["rejected"].append(params)
                    return FakeResult()
                if "insert into ad_job_outbox" in lowered:
                    pending_outbox_jobs.append(params)
                    return FakeResult()
                return FakeResult()

        try:
            yield Connection()
        except Exception:
            raise
        else:
            self.state = pending
            self.outbox_jobs = pending_outbox_jobs


@pytest.fixture
def api_setup(monkeypatch):
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    monkeypatch.setattr(settings, "load_dotenv", lambda: None)
    monkeypatch.setenv("JWT_SECRET", "test-secret-only-not-for-production")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("MAX_XLSX_UNCOMPRESSED_BYTES", str(20 * 1024 * 1024))
    database = FakeUploadDatabase()

    def dispatch_pending_jobs(*, enqueue_fn):
        pending_jobs = [
            AdJob(batch_id=batch_id, ad_id=ad_id)
            for batch_id, ad_id in database.outbox_jobs
            if (batch_id, ad_id) not in database.queued_outbox_jobs
        ]
        if pending_jobs:
            enqueue_fn(pending_jobs)
            database.queued_outbox_jobs.update(
                (job.batch_id, job.ad_id) for job in pending_jobs
            )

    monkeypatch.setattr(main, "init_schema", lambda: None)
    monkeypatch.setattr(uploads, "get_session", database.session)
    monkeypatch.setattr(uploads, "enqueue_ad_jobs", lambda jobs: len(jobs))
    monkeypatch.setattr(uploads.outbox, "dispatch_pending_jobs", dispatch_pending_jobs)
    return database


def auth_headers(monkeypatch, *, role="buyer", assigned=("brightleaf",)):
    record = {
        "user_id": "synthetic-user",
        "email": "user@example.test",
        "display_name": "SYNTHETIC test user",
        "role": role,
        "is_active": True,
    }
    monkeypatch.setattr(auth.users, "get_user_by_id", lambda user_id: record)
    monkeypatch.setattr(auth.users, "get_user_client_ids", lambda user_id: list(assigned))
    return {"Authorization": f"Bearer {auth.create_access_token(record['user_id'], role)}"}


def multipart_csv(rows: str, filename="ads.csv"):
    return {"file": (filename, rows.encode("utf-8"), CSV_MIME)}


def xlsx_bytes(url="https://brightleaf.example/page"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Row", "Platform", "Headline", "URL"])
    sheet.append([1, "Meta", "Synthetic headline", url])
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def test_upload_csv_saves_ads_with_inferred_client_and_rejected_rows(monkeypatch, api_setup):
    enqueued = []
    monkeypatch.setattr(uploads, "enqueue_ad_jobs", lambda jobs: enqueued.extend(jobs))
    headers = auth_headers(monkeypatch)
    body = (
        "Row,Platform,Headline,URL\n"
        "1,Meta,known,https://brightleaf.example/offer\n"
        "2,Meta,unknown,https://tallyhq.example/offer\n"
        "bad,Meta,malformed,https://brightleaf.example/offer\n"
    )

    with TestClient(app) as client:
        response = client.post("/uploads", files=multipart_csv(body), headers=headers)

    assert response.status_code == 201
    payload = response.json()
    assert UUID(payload["batch_id"]).version == 4
    assert payload["accepted_count"] == 1
    assert payload["rejected_count"] == 2
    assert {item["reason_code"] for item in payload["rejected_rows"]} == {
        "invalid_row_number",
        "unknown_client_host",
    }
    assert api_setup.state["ads"][0][2] == "brightleaf"
    assert enqueued == [AdJob(batch_id=payload["batch_id"], ad_id=api_setup.state["ads"][0][0])]
    assert len(api_setup.state["rejected"]) == 2
    assert api_setup.state["batches"][payload["batch_id"]][2] == "ads.csv"


def test_upload_xlsx_accepts_valid_workbook(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files={"file": ("ads.xlsx", xlsx_bytes(), XLSX_MIME)},
            headers=headers,
        )

    assert response.status_code == 201
    assert response.json()["accepted_count"] == 1
    assert api_setup.state["ads"][0][2] == "brightleaf"


def test_upload_enqueues_exactly_one_job_per_saved_ad(monkeypatch, api_setup):
    enqueued = []
    persisted_jobs = []
    monkeypatch.setattr(
        uploads.outbox,
        "persist_ad_jobs_on_connection",
        lambda connection, jobs: persisted_jobs.extend(jobs),
    )
    monkeypatch.setattr(
        uploads.outbox,
        "dispatch_pending_jobs",
        lambda *, enqueue_fn: enqueue_fn(persisted_jobs),
    )
    monkeypatch.setattr(uploads, "enqueue_ad_jobs", lambda jobs: enqueued.extend(jobs))
    headers = auth_headers(monkeypatch)
    body = (
        "Row,Platform,Headline,URL\n"
        "1,Meta,first,https://brightleaf.example/one\n"
        "bad,Meta,isolated,https://brightleaf.example/two\n"
        "2,Meta,second,https://brightleaf.example/three\n"
    )

    with TestClient(app) as client:
        response = client.post("/uploads", files=multipart_csv(body), headers=headers)

    assert response.status_code == 201
    assert response.json()["accepted_count"] == 2
    saved_ad_ids = [ad[0] for ad in api_setup.state["ads"]]
    assert [job.ad_id for job in persisted_jobs] == saved_ad_ids
    assert [job.ad_id for job in enqueued] == saved_ad_ids
    assert all(job.batch_id == response.json()["batch_id"] for job in persisted_jobs)


def test_repeated_sheet_returns_original_batch_and_saves_nothing_again(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch)
    body = "Row,Platform,Headline,URL\n1,Meta,one,https://brightleaf.example/offer\n"
    with TestClient(app) as client:
        first = client.post("/uploads", files=multipart_csv(body), headers=headers)
        second = client.post("/uploads", files=multipart_csv(body), headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == {
        "batch_id": first.json()["batch_id"],
        "accepted_count": 0,
        "rejected_count": 0,
        "rejected_rows": [],
        "unmapped_columns": [],
        "platform_unrecognized": [],
        "duplicate_sheet": True,
    }
    assert len(api_setup.state["batches"]) == 1
    assert len(api_setup.state["ads"]) == 1


def test_repeat_by_different_user_does_not_reveal_original_uploader(monkeypatch, api_setup):
    first_headers = auth_headers(monkeypatch)
    body = "Row,Platform,Headline,URL\n1,Meta,one,https://brightleaf.example/offer\n"
    with TestClient(app) as client:
        first = client.post("/uploads", files=multipart_csv(body), headers=first_headers)
        monkeypatch.setattr(
            auth.users,
            "get_user_by_id",
            lambda user_id: {
                "user_id": "second-user",
                "email": "second@example.test",
                "display_name": "SYNTHETIC second buyer",
                "role": "buyer",
                "is_active": True,
            },
        )
        monkeypatch.setattr(auth.users, "get_user_client_ids", lambda user_id: ["brightleaf"])
        second_token = auth.create_access_token("second-user", "buyer")
        second = client.post(
            "/uploads",
            files=multipart_csv(body),
            headers={"Authorization": f"Bearer {second_token}"},
        )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert "uploaded_by" not in second.json()
    assert len(api_setup.state["batches"]) == 1


def test_partially_duplicate_multi_client_sheet_only_saves_new_client_rows(monkeypatch, api_setup):
    import hashlib
    import json

    from apps.api.ingest import parse_csv_upload

    headers = auth_headers(monkeypatch, assigned=("brightleaf", "calmleaf"))
    body = (
        "Row,Platform,Headline,URL\n"
        "1,Meta,bright,https://brightleaf.example/offer\n"
        "2,Meta,calm,https://calmleaf.example/offer\n"
    )
    parsed = parse_csv_upload("ads.csv", body.encode("utf-8"), max_file_bytes=2 * 1024 * 1024)
    original_batch_id = "original-batch"
    api_setup.state["batches"][original_batch_id] = (
        original_batch_id,
        "first-user",
        "earlier.csv",
        parsed.sheet_hash,
    )
    api_setup.state["claims"][("brightleaf", parsed.sheet_hash)] = original_batch_id

    with TestClient(app) as client:
        response = client.post("/uploads", files=multipart_csv(body), headers=headers)

    assert response.status_code == 201
    assert response.json()["accepted_count"] == 1
    assert response.json()["skipped_duplicate_clients"] == ["brightleaf"]
    assert [ad[2] for ad in api_setup.state["ads"]] == ["calmleaf"]


def test_multi_client_upload_by_buyer_saves_only_assigned_client_rows(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch, assigned=("brightleaf",))
    body = (
        "Row,Platform,Headline,URL\n"
        "1,Meta,assigned,https://brightleaf.example/offer\n"
        "2,Meta,unassigned,https://calmleaf.example/offer\n"
        "3,Meta,unknown,https://tallyhq.example/offer\n"
    )

    with TestClient(app) as client:
        response = client.post("/uploads", files=multipart_csv(body), headers=headers)

    assert response.status_code == 201
    payload = response.json()
    assert payload["accepted_count"] == 1
    assert payload["rejected_count"] == 2
    assert {row["reason_code"] for row in payload["rejected_rows"]} == {
        "client_not_assigned",
        "unknown_client_host",
    }
    assert [ad[2] for ad in api_setup.state["ads"]] == ["brightleaf"]
    assert {claim[0] for claim in api_setup.state["claims"]} == {"brightleaf"}
    assert {row[3] for row in api_setup.state["rejected"]} == {
        "client_not_assigned",
        "unknown_client_host",
    }


def test_upload_with_only_unassigned_rows_returns_rejections(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch, assigned=("brightleaf",))
    body = "Row,Platform,Headline,URL\n1,Meta,one,https://calmleaf.example/offer\n"

    with TestClient(app) as client:
        response = client.post("/uploads", files=multipart_csv(body), headers=headers)

    assert response.status_code == 201
    assert response.json()["accepted_count"] == 0
    assert response.json()["rejected_rows"][0]["reason_code"] == "client_not_assigned"
    assert len(api_setup.state["batches"]) == 1
    assert api_setup.state["ads"] == []
    assert {row[3] for row in api_setup.state["rejected"]} == {"client_not_assigned"}


def test_account_manager_can_upload_for_assigned_client(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch, role="account_manager")
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files=multipart_csv("Row,Platform,Headline,URL\n1,Meta,one,https://brightleaf.example/"),
            headers=headers,
        )
    assert response.status_code == 201
    assert response.json()["accepted_count"] == 1


def test_account_manager_cannot_upload_for_unassigned_client(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch, role="account_manager", assigned=("calmleaf",))
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files=multipart_csv("Row,Platform,Headline,URL\n1,Meta,one,https://brightleaf.example/"),
            headers=headers,
        )
    assert response.status_code == 403


def test_unsupported_content_type_or_extension_is_415(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch)
    with TestClient(app) as client:
        accepted_octet_stream_but_bad_csv = client.post(
            "/uploads",
            files={"file": ("ads.csv", b"data", "application/octet-stream")},
            headers=headers,
        )
        wrong_extension = client.post(
            "/uploads",
            files={"file": ("ads.txt", b"data", CSV_MIME)},
            headers=headers,
        )
        excel_mime_for_csv = client.post(
            "/uploads",
            files={
                "file": (
                    "ads.csv",
                    b"Row,Platform,Headline,URL\n1,Meta,hello,https://brightleaf.example/offer\n",
                    "application/vnd.ms-excel",
                )
            },
            headers=headers,
        )
        macro_enabled_extension = client.post(
            "/uploads",
            files={
                "file": (
                    "ads.xlsm",
                    b"data",
                    "application/vnd.ms-excel.sheet.macroenabled.12",
                )
            },
            headers=headers,
        )
    assert accepted_octet_stream_but_bad_csv.status_code == 422
    assert wrong_extension.status_code == 415
    assert excel_mime_for_csv.status_code == 201
    assert macro_enabled_extension.status_code == 415


@pytest.mark.parametrize(
    "content_type",
    [
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
        "text/plain",
        "application/octet-stream",
    ],
)
def test_csv_accepts_supported_mime_types(monkeypatch, api_setup, content_type):
    headers = auth_headers(monkeypatch)
    content = b"Row,Platform,Headline,URL\n1,Meta,hello,https://brightleaf.example/offer\n"
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files={"file": ("ads.csv", content, content_type)},
            headers=headers,
        )
    assert response.status_code == 201


@pytest.mark.parametrize(
    "content_type",
    [
        XLSX_MIME,
        "application/octet-stream",
    ],
)
def test_xlsx_accepts_supported_mime_types(monkeypatch, api_setup, content_type):
    headers = auth_headers(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files={"file": ("ads.xlsx", xlsx_bytes(), content_type)},
            headers=headers,
        )
    assert response.status_code == 201


@pytest.mark.parametrize(
    "filename,content,content_type,reason_code",
    [
        ("ads.csv", b"not a valid utf-8 csv \xff", "application/octet-stream", "unparseable_csv"),
        ("ads.xlsx", b"Row,Platform,Headline,URL\n1,Meta,hello,/page", "application/octet-stream", "unreadable_xlsx"),
    ],
)
def test_allowed_mime_does_not_override_mismatched_file_content(
    monkeypatch, api_setup, filename, content, content_type, reason_code
):
    headers = auth_headers(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files={"file": (filename, content, content_type)},
            headers=headers,
        )
    assert response.status_code == 422
    assert response.json()["detail"]["reason_code"] == reason_code


def test_file_level_parse_rejection_returns_reason_code_and_saves_nothing(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files=multipart_csv("Row,Platform,Headline\n1,Meta,missing link\n"),
            headers=headers,
        )
    assert response.status_code == 422
    assert response.json()["detail"]["reason_code"] == "missing_required_columns"
    assert api_setup.state == {"batches": {}, "claims": {}, "ads": [], "rejected": []}


def test_xlsx_uncompressed_size_cap_is_413(monkeypatch, api_setup):
    monkeypatch.setenv("MAX_XLSX_UNCOMPRESSED_BYTES", "1")
    headers = auth_headers(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files={"file": ("ads.xlsx", xlsx_bytes(), XLSX_MIME)},
            headers=headers,
        )
    assert response.status_code == 413
    assert "1 bytes" in response.json()["detail"]["message"]
    assert api_setup.state == {"batches": {}, "claims": {}, "ads": [], "rejected": []}


def test_failure_after_ad_insert_rolls_back_then_retry_succeeds(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch)
    body = "Row,Platform,Headline,URL\n1,Meta,one,https://brightleaf.example/offer\n"
    from apps.api import rejected_rows

    original = rejected_rows.persist_rejected_rows_on_connection

    def persist_then_fail(connection, batch_id, rejected):
        original(connection, batch_id, rejected)
        raise RuntimeError("injected failure after rejected-row insert")

    monkeypatch.setattr(rejected_rows, "persist_rejected_rows_on_connection", persist_then_fail)
    body = (
        "Row,Platform,Headline,URL\n"
        "1,Meta,known,https://brightleaf.example/offer\n"
        "2,Meta,unknown,https://tallyhq.example/offer\n"
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.post("/uploads", files=multipart_csv(body), headers=headers)
    assert failed.status_code == 500
    assert api_setup.state == {"batches": {}, "claims": {}, "ads": [], "rejected": []}

    monkeypatch.setattr(rejected_rows, "persist_rejected_rows_on_connection", original)
    with TestClient(app) as client:
        retried = client.post("/uploads", files=multipart_csv(body), headers=headers)
    assert retried.status_code == 201
    assert api_setup.state["claims"]


def test_filename_is_stored_as_base_name_only(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch)
    body = "Row,Platform,Headline,URL\n1,Meta,one,https://brightleaf.example/offer\n"
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files=multipart_csv(body, filename="../../evil.csv"),
            headers=headers,
        )
    assert response.status_code == 201
    batch = api_setup.state["batches"][response.json()["batch_id"]]
    assert batch[2] == "evil.csv"


def test_request_body_exactly_at_limit_is_accepted(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch)
    limit = 1024
    boundary = "launch-gate-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="ads.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
        "Row,Platform,Headline,URL\n1,Meta,"
    ).encode("utf-8")
    suffix = f",https://brightleaf.example/page\r\n--{boundary}--\r\n".encode("utf-8")
    padding_length = limit - len(prefix) - len(suffix)
    body = prefix + (b"x" * padding_length) + suffix
    assert len(body) == limit
    monkeypatch.setenv("MAX_UPLOAD_BYTES", str(limit))

    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            content=body,
            headers={
                **headers,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == 201
    assert response.json()["accepted_count"] == 1


def test_batch_id_form_field_is_ignored_and_client_field_is_rejected(monkeypatch, api_setup):
    headers = auth_headers(monkeypatch)
    body = "Row,Platform,Headline,URL\n1,Meta,one,https://brightleaf.example/offer\n"
    with TestClient(app) as client:
        accepted = client.post(
            "/uploads",
            data={"batch_id": "owner-supplied"},
            files=multipart_csv(body),
            headers=headers,
        )
        rejected = client.post(
            "/uploads",
            data={"client_id": "calmleaf"},
            files=multipart_csv(body.replace("brightleaf", "calmleaf")),
            headers=headers,
        )
    assert accepted.status_code == 201
    assert accepted.json()["batch_id"] != "owner-supplied"
    assert rejected.status_code == 422


def test_xlsx_archive_entry_count_cap_is_413(monkeypatch, api_setup):
    source = xlsx_bytes()
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(source)) as original, zipfile.ZipFile(output, "w") as expanded:
        for entry in original.infolist():
            expanded.writestr(entry, original.read(entry.filename))
        for index in range(200):
            expanded.writestr(f"extra/{index}.txt", b"")
    headers = auth_headers(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files={"file": ("ads.xlsx", output.getvalue(), XLSX_MIME)},
            headers=headers,
        )
    assert response.status_code == 413
    assert "200 entries" in response.json()["detail"]["message"]


def test_highly_compressible_xlsx_entry_hits_uncompressed_cap(monkeypatch, api_setup):
    source = xlsx_bytes()
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(source)) as original, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as expanded:
        for entry in original.infolist():
            expanded.writestr(entry, original.read(entry.filename))
        expanded.writestr("xl/large-text.txt", b"A" * 1_000_000)
    assert len(output.getvalue()) < 2 * 1024 * 1024
    monkeypatch.setenv("MAX_XLSX_UNCOMPRESSED_BYTES", "10000")
    headers = auth_headers(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/uploads",
            files={"file": ("ads.xlsx", output.getvalue(), XLSX_MIME)},
            headers=headers,
        )
    assert response.status_code == 413
    assert "10000 bytes" in response.json()["detail"]["message"]


@pytest.mark.parametrize(
    "body, content_length, expected_status, expected_receive_calls",
    [(b"12345678", None, 204, 1), (b"123456789", None, 413, 1), (b"", b"9", 413, 0)],
)
def test_request_body_limit_streaming_and_content_length(
    monkeypatch, body, content_length, expected_status, expected_receive_calls
):
    monkeypatch.setattr(settings, "load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "8")
    middleware_calls = {"receive": 0, "app": 0, "status": None}

    async def downstream(scope, receive, send):
        middleware_calls["app"] += 1
        await receive()
        middleware_calls["status"] = 204
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = uploads.UploadBodyLimitMiddleware(downstream)

    async def receive():
        middleware_calls["receive"] += 1
        headers = [(b"content-type", b"multipart/form-data; boundary=x")]
        if content_length is not None:
            headers.append((b"content-length", content_length))
        if middleware_calls["receive"] == 1:
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "path": "/uploads",
        "method": "POST",
        "headers": [(b"content-type", b"multipart/form-data; boundary=x")]
        + ([(b"content-length", content_length)] if content_length is not None else []),
    }
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    response_status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    assert response_status == expected_status
    assert middleware_calls["receive"] == expected_receive_calls
    assert (middleware_calls["app"] == 1) == (expected_status == 204)
