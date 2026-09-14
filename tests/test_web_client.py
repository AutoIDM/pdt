"""pdt.utils.web.Client: retry, back off, and report the response.

httpx.MockTransport stands in for the network; time.sleep is patched so
the waits are recorded, not slept.
"""

from __future__ import annotations

import time

import httpx
import pytest

from pdt.utils import web
from pdt.utils.web import (
    Client, FatalAPIError, RetriableAPIError, http_json, retry_after_seconds,
    validate_response)

BASE = "https://api.example.com"


def response(status: int, body=None, headers=None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {},
                          headers=headers, request=httpx.Request("GET", BASE))


def install(monkeypatch, outcomes, **client_kwargs):
    """Each request pops one outcome: an exception to raise or a Response."""
    calls, waits, logs = [], [], []

    def handler(request):
        calls.append(request)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(time, "sleep", waits.append)
    client = Client(BASE, transport=httpx.MockTransport(handler),
                    log=lambda level, msg, **kv: logs.append((level, msg, kv)),
                    **client_kwargs)
    return client, calls, waits, logs


def test_validate_response_splits_retriable_from_fatal():
    validate_response(response(302))
    with pytest.raises(RetriableAPIError):
        validate_response(response(429))
    with pytest.raises(RetriableAPIError):
        validate_response(response(503))
    with pytest.raises(FatalAPIError) as raised:
        validate_response(response(404))
    assert "404 Client Error" in str(raised.value)
    assert raised.value.response.status_code == 404


def test_returns_response_and_sets_user_agent(monkeypatch):
    client, calls, waits, logs = install(monkeypatch, [response(200, {"ok": 1})])
    assert client.get("/things").json() == {"ok": 1}
    assert str(calls[0].url) == f"{BASE}/things"
    assert calls[0].headers["User-Agent"].startswith("pdt/")
    assert waits == [] and logs == []


def test_retries_rate_limit_then_succeeds(monkeypatch):
    client, calls, waits, logs = install(
        monkeypatch, [response(429), response(429), response(200, {"ok": True})])
    assert client.get("/things").json() == {"ok": True}
    assert len(calls) == 3
    assert len(waits) == 2
    assert [entry[0] for entry in logs] == ["warning", "warning"]
    assert logs[0][2]["try"] == 1 and logs[0][2]["status"] == 429


def test_backoff_is_exponential_from_two_seconds(monkeypatch):
    client, calls, waits, logs = install(
        monkeypatch, [response(500), response(502), response(503), response(200)])
    client.get("/things")
    assert 2 <= waits[0] < 3
    assert 4 <= waits[1] < 5
    assert 8 <= waits[2] < 9


def test_retry_after_header_replaces_the_wait(monkeypatch):
    client, calls, waits, logs = install(
        monkeypatch, [response(429, headers={"Retry-After": "7"}), response(200)])
    client.get("/things")
    assert 7 <= waits[0] < 8


def test_retry_after_is_capped_and_parses_dates():
    capped = response(503, headers={"Retry-After": "99999"})
    assert retry_after_seconds(RetriableAPIError("x", capped)) == web.MAX_RETRY_AFTER
    dated = response(503, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert retry_after_seconds(RetriableAPIError("x", dated)) == 0.0
    assert retry_after_seconds(RetriableAPIError("x", response(503))) is None
    assert retry_after_seconds(httpx.ReadTimeout("slow")) is None


@pytest.mark.parametrize("error", [
    httpx.ConnectTimeout("connect timed out"),
    httpx.ReadTimeout("read timed out"),
    httpx.ConnectError("connection refused"),
    httpx.RemoteProtocolError("server disconnected"),
    ConnectionResetError(104),
])
def test_retries_connection_errors_and_timeouts(monkeypatch, error):
    client, calls, waits, logs = install(monkeypatch, [error, response(200)])
    client.post("/things", json={"a": 1})
    assert len(calls) == 2
    assert len(waits) == 1
    assert type(error).__name__ in logs[0][2]["error"]


def test_client_error_is_fatal_at_once_and_logs_the_body(monkeypatch):
    client, calls, waits, logs = install(
        monkeypatch, [response(404, {"error": "no such thing"})])
    with pytest.raises(FatalAPIError):
        client.get("/things")
    assert len(calls) == 1 and waits == []
    level, msg, fields = logs[-1]
    assert level == "error"
    assert fields["method"] == "GET"
    assert fields["url"] == f"{BASE}/things"
    assert fields["status"] == 404
    assert "no such thing" in fields["body"]


def test_gives_up_after_max_tries_and_logs_the_last_response(monkeypatch):
    client, calls, waits, logs = install(
        monkeypatch, [response(503, {"n": n}) for n in range(web.MAX_TRIES)])
    with pytest.raises(RetriableAPIError):
        client.get("/things")
    assert len(calls) == web.MAX_TRIES
    assert len(waits) == web.MAX_TRIES - 1
    assert logs[-1][0] == "error"
    assert logs[-1][2]["body"] == f'{{"n":{web.MAX_TRIES - 1}}}'


def test_max_tries_is_configurable(monkeypatch):
    client, calls, waits, logs = install(
        monkeypatch, [response(503), response(503), response(200)], max_tries=2)
    with pytest.raises(RetriableAPIError):
        client.get("/things")
    assert len(calls) == 2


def test_exit_code_exits_after_logging(monkeypatch):
    client, calls, waits, logs = install(monkeypatch, [response(401)], exit_code=7)
    with pytest.raises(SystemExit) as raised:
        client.get("/things")
    assert raised.value.code == 7
    assert logs[-1][0] == "error" and logs[-1][2]["status"] == 401


def test_exit_code_covers_exhausted_timeouts(monkeypatch):
    client, calls, waits, logs = install(
        monkeypatch, [httpx.ReadTimeout("slow")] * web.MAX_TRIES, exit_code=3)
    with pytest.raises(SystemExit) as raised:
        client.get("/things")
    assert raised.value.code == 3
    assert "ReadTimeout" in logs[-1][2]["error"]


def test_body_excerpt_truncates():
    long = response(500, {"text": "x" * (web.BODY_LIMIT * 2)})
    excerpt = web.body_excerpt(long)
    assert len(excerpt) < web.BODY_LIMIT + 60
    assert "characters in all" in excerpt


def test_default_timeouts():
    client = Client(BASE)
    assert client.timeout.connect == web.CONNECT_TIMEOUT
    assert client.timeout.read == web.READ_TIMEOUT
    assert Client(BASE, timeout=5).timeout.read == 5


def install_transport(monkeypatch, outcomes):
    """Patch the real transport so code that builds its own Client is offline."""
    calls, waits = [], []

    def handle_request(self, request):
        calls.append(request)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(time, "sleep", waits.append)
    return calls, waits


def test_http_json_retries_then_returns_dict(monkeypatch):
    calls, waits = install_transport(
        monkeypatch, [response(503), response(200, {"data": 1})])
    assert http_json(f"{BASE}/x", 4, method="POST", data=b"{}",
                     headers={"Authorization": "Bearer t"}) == {"data": 1}
    assert len(calls) == 2
    assert calls[0].headers["Authorization"] == "Bearer t"
    assert calls[0].method == "POST"


def test_http_json_dies_with_exit_code(monkeypatch):
    install_transport(monkeypatch, [response(400, {"bad": True})])
    with pytest.raises(SystemExit) as raised:
        http_json(f"{BASE}/x", 4)
    assert raised.value.code == 4


def test_http_json_empty_body_is_empty_dict(monkeypatch):
    empty = httpx.Response(204, request=httpx.Request("DELETE", BASE))
    install_transport(monkeypatch, [empty])
    assert http_json(f"{BASE}/x", 4, method="DELETE") == {}
