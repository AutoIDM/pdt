"""One urllib helper shared by the apps and the CLI in this repo."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from pdt.utils.log import die


def http_bytes(url: str, exit_code: int, method: str = "GET",
               headers: dict | None = None, data: bytes | None = None,
               allow_statuses: tuple[int, ...] = ()) -> tuple[int, bytes]:
    """Request url and return status plus raw body; die with exit_code on failure."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        if e.code in allow_statuses:
            return e.code, body
        detail = body.decode("utf-8", errors="replace")[:800]
        die(exit_code, "http error", url=url, status=e.code, detail=detail)
    except urllib.error.URLError as e:
        die(exit_code, "http connection failed", url=url, error=str(e.reason))


def http_json(url: str, exit_code: int, method: str = "GET",
              headers: dict | None = None, data: bytes | None = None) -> dict:
    """Request url and return the decoded json body; die with exit_code on failure."""
    _status, raw = http_bytes(url, exit_code, method=method, headers=headers, data=data)
    if raw == b"":
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        die(exit_code, "http response was not json", url=url)
