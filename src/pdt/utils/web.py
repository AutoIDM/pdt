"""One urllib helper shared by the apps and the CLI in this repo."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from pdt.utils.log import die


def http_json(url: str, exit_code: int, method: str = "GET",
              headers: dict | None = None, data: bytes | None = None) -> dict:
    """Request url and return the decoded json body; die with exit_code on failure."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if raw == b"":
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        die(exit_code, "http error", url=url, status=e.code, detail=detail)
    except urllib.error.URLError as e:
        die(exit_code, "http connection failed", url=url, error=str(e.reason))
    except json.JSONDecodeError:
        die(exit_code, "http response was not json", url=url)
