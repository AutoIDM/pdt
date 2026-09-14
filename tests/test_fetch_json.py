from __future__ import annotations

import time

import httpx
import pytest

from pdt import deploy_common
from pdt.deploy_common import FatalAPIError, RetriableAPIError, fetch_json

URL = "https://prices.example.com/api/prices"


def response(status: int, body=None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {},
                          request=httpx.Request("GET", URL))


def install(monkeypatch, outcomes):
    calls, waits = [], []

    def handle_request(self, request):
        calls.append(request)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(time, "sleep", waits.append)
    monkeypatch.setattr(deploy_common, "_client", None)
    return calls, waits


def test_returns_parsed_json_with_headers(monkeypatch):
    calls, waits = install(monkeypatch, [response(200, {"Items": [1]})])
    assert fetch_json(URL, timeout=30, headers={"Authorization": "Bearer x"}) == {"Items": [1]}
    assert calls[0].headers["Authorization"] == "Bearer x"
    assert waits == []


def test_retries_server_error_and_prints_backoff(monkeypatch, capsys):
    calls, waits = install(monkeypatch, [response(503), response(200, {"ok": True})])
    assert fetch_json(URL) == {"ok": True}
    assert len(calls) == 2
    assert "try 1 of" in capsys.readouterr().out


def test_client_error_is_fatal_and_prints_the_body(monkeypatch, capsys):
    calls, waits = install(monkeypatch, [response(404, {"error": "gone"})])
    with pytest.raises(FatalAPIError):
        fetch_json(URL)
    assert len(calls) == 1 and waits == []
    out = capsys.readouterr().out
    assert "404" in out and "gone" in out


def test_gives_up_after_max_tries(monkeypatch):
    calls, waits = install(monkeypatch, [response(429)] * 5)
    with pytest.raises(RetriableAPIError):
        fetch_json(URL)
    assert len(calls) == 5
    assert len(waits) == 4
