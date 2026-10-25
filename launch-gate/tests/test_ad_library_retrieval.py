import httpx
import pytest

from apps.worker.ad_library_retrieval import retrieve_ad_library


class MockResponse:
    def __init__(self, status_code, payload=None, json_error=False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not JSON")
        return self._payload


class MockClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def get(self, _url):
        if self.error:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    "client,status,http_status,message",
    [
        (MockClient(MockResponse(403)), "retrieval_forbidden", 403, "denied access"),
        (MockClient(error=httpx.TimeoutException("timeout")), "retrieval_timeout", None, "timed out"),
        (MockClient(MockResponse(200, json_error=True)), "malformed_response", 200, "non-JSON"),
        (MockClient(MockResponse(200, {"ads": [{"id": "ad-1"}]})), "retrieved", 200, None),
    ],
)
def test_ad_library_result_states(client, status, http_status, message):
    result = retrieve_ad_library("https://example.test/ad-library", client=client)

    assert result.status == status
    assert result.http_status == http_status
    if message:
        assert message in result.message
    assert result.user_supplied_fallback_available is True

    payload = result.to_dict()
    assert payload["status"] == status
