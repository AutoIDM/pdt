import io
import json
import time
import urllib.error
import urllib.request

import pytest

from pdt import deploy_common
from pdt.deploy_common import (
    FatalAPIError, RetriableAPIError, fetch_json, validate_response)

URL = "https://prices.example.com/api/prices"


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "boom", None, io.BytesIO(b"{}"))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def install(monkeypatch, outcomes):
    """urlopen pops one outcome per call: an exception to raise or a payload."""
    calls = []
    waits = []

    def fake_urlopen(request, timeout=0):
        calls.append(request)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(json.dumps(outcome).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", waits.append)
    return calls, waits


def test_validate_response_splits_retriable_from_fatal():
    validate_response(http_error(302))
    with pytest.raises(RetriableAPIError):
        validate_response(http_error(429))
    with pytest.raises(RetriableAPIError):
        validate_response(http_error(503))
    with pytest.raises(FatalAPIError) as raised:
        validate_response(http_error(404))
    assert "404 Client Error" in str(raised.value)
    assert raised.value.response.status == 404


def test_returns_parsed_json(monkeypatch):
    calls, waits = install(monkeypatch, [{"Items": [1]}])
    assert fetch_json(URL) == {"Items": [1]}
    assert len(calls) == 1
    assert waits == []


def test_retries_rate_limit_then_succeeds(monkeypatch):
    calls, waits = install(
        monkeypatch, [http_error(429), http_error(429), {"ok": True}])
    assert fetch_json(URL) == {"ok": True}
    assert len(calls) == 3
    assert len(waits) == 2


def test_retries_server_error(monkeypatch):
    calls, waits = install(monkeypatch, [http_error(500), {"ok": True}])
    assert fetch_json(URL) == {"ok": True}
    assert len(calls) == 2


@pytest.mark.parametrize("error", [
    urllib.error.URLError(ConnectionRefusedError(111)),
    TimeoutError("timed out"),
    ConnectionResetError(104),
])
def test_retries_connection_errors_and_timeouts(monkeypatch, error):
    calls, waits = install(monkeypatch, [error, {"ok": True}])
    assert fetch_json(URL) == {"ok": True}
    assert len(calls) == 2
    assert len(waits) == 1


def test_client_error_is_fatal_at_once(monkeypatch):
    calls, waits = install(monkeypatch, [http_error(404)])
    with pytest.raises(FatalAPIError):
        fetch_json(URL)
    assert len(calls) == 1
    assert waits == []


def test_gives_up_after_max_tries(monkeypatch):
    attempts = deploy_common.BACKOFF_MAX_TRIES
    calls, waits = install(monkeypatch, [http_error(429) for _ in range(attempts)])
    with pytest.raises(RetriableAPIError):
        fetch_json(URL)
    assert len(calls) == attempts
    assert len(waits) == attempts - 1


def test_accepts_request_object(monkeypatch):
    calls, waits = install(monkeypatch, [http_error(503), {"ok": True}])
    req = urllib.request.Request(URL, headers={"Authorization": "Bearer x"})
    assert fetch_json(req) == {"ok": True}
    assert calls == [req, req]
