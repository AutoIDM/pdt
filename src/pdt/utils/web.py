"""One urllib helper shared by the apps and the CLI in this repo."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

from pdt.utils.log import die


def trust_os_certificates() -> None:
    """Verify every HTTPS call of this process against the operating system's certificate
    store, as pip does: a company TLS proxy's root and a root Windows has not fetched yet
    are there and not in Python's or certifi's file. truststore replaces ssl.SSLContext,
    which urllib, urllib3, requests, botocore, msal, and google-auth all build on.

    Only a process pdt starts calls this, first: botocore keeps its own reference to
    ssl.SSLContext, and once that is replaced a botocore imported earlier fails with a
    RecursionError. Code in an app's process uses os_ssl_context or
    os_requests_session instead."""
    import truststore
    truststore.inject_into_ssl()


def os_ssl_context() -> ssl.SSLContext:
    """A context that trusts the operating system's certificate store, for one call in an
    app's process; Python's default where the app's environment lacks truststore."""
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def os_requests_session():
    """A requests session whose HTTPS calls use os_ssl_context, for msal and google-auth."""
    import requests
    from requests.adapters import HTTPAdapter

    class Adapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["ssl_context"] = os_ssl_context()
            super().init_poolmanager(*args, **kwargs)

    session = requests.Session()
    session.mount("https://", Adapter())
    return session


def http_json(url: str, exit_code: int, method: str = "GET",
              headers: dict | None = None, data: bytes | None = None) -> dict:
    """Request url and return the decoded json body; die with exit_code on failure."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60, context=os_ssl_context()) as resp:
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
