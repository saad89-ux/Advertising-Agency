from scripts import meta_token_diagnostics as diagnostics


class MockResponse:
    status_code = 200

    def __init__(self, status_code, payload=None, json_error=False):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("not JSON")
        return self.payload


def run_with_response(monkeypatch, response):
    monkeypatch.setenv("META_SYSTEM_USER_TOKEN", "test-token")
    monkeypatch.setattr(diagnostics.httpx, "get", lambda *_args, **_kwargs: response)
    return diagnostics.run_diagnostics()


def test_meta_diagnostics_classify_invalid_token(monkeypatch, capsys):
    result = run_with_response(monkeypatch, MockResponse(401, {"error": {"code": 190, "message": "Invalid OAuth access token"}}))

    assert result["status"] == "invalid_token"
    assert result["http_status"] == 401
    assert "META_SYSTEM_USER_TOKEN" in " ".join(result["setup_checklist"])
    assert capsys.readouterr().out.find("test-token") == -1


def test_meta_diagnostics_classify_missing_permission(monkeypatch):
    result = run_with_response(monkeypatch, MockResponse(403, {"error": {"code": 200, "message": "Missing ads_read permission"}}))

    assert result["status"] == "missing_permission"
    assert result["http_status"] == 403


def test_meta_diagnostics_classify_malformed_response(monkeypatch):
    result = run_with_response(monkeypatch, MockResponse(502, json_error=True))

    assert result["status"] == "malformed_response"
    assert result["http_status"] == 502


def test_meta_diagnostics_valid_token(monkeypatch):
    result = run_with_response(monkeypatch, MockResponse(200, {"data": {
        "is_valid": True,
        "expires_at": 0,
        "scopes": ["ads_read"],
    }}))

    assert result["status"] == "valid"
    assert result["scopes"] == ["ads_read"]
