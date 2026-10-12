"""Every pdt HTTPS call verifies the server against the operating system's certificate store.

A process pdt starts (the `pdt` command and each `uv run --script` file)
calls `web.trust_os_certificates()` before it imports a library that holds
its own reference to `ssl.SSLContext` (botocore does), so urllib, urllib3,
requests, botocore, msal, and google-auth all use truststore. A module that
only those processes import, such as `download` or `deploy_common`, needs no
call of its own. The job-side utils run in an app's own process, where a
global replacement would break a botocore the app imported first, so each
HTTPS call there passes `os_ssl_context()` or `os_requests_session()`, which
work without truststore.
"""

import ast
import os
import re
import ssl
import sys
from pathlib import Path

import pytest
import truststore
import urllib3.util.ssl_

from pdt import cli
from pdt.utils import web

SRC = Path(__file__).resolve().parent.parent / "src" / "pdt"
CALL = "trust_os_certificates()"
HTTPS = {"urlopen(": "os_ssl_context()", "smtplib.SMTP_SSL(": "os_ssl_context()",
         "starttls(": "os_ssl_context()", "PublicClientApplication(": "os_requests_session()",
         "credentials.refresh(": "os_requests_session()"}


@pytest.fixture
def restore_ssl():
    yield
    truststore.extract_from_ssl()


def scripts():
    return [path for path in sorted(SRC.glob("*.py"))
            if path.read_text().split("\n")[1:2] == ["# /// script"]
            and re.search(r'^#     "', path.read_text(), re.M)]


def test_the_helper_puts_truststore_under_ssl_urllib3_and_urllib(restore_ssl):
    web.trust_os_certificates()
    assert isinstance(ssl.create_default_context(), truststore.SSLContext)
    assert urllib3.util.ssl_.SSLContext is truststore.SSLContext


def test_an_apps_context_uses_truststore_and_falls_back_without_it(monkeypatch):
    assert isinstance(web.os_ssl_context(), truststore.SSLContext)
    session = web.os_requests_session()
    assert isinstance(session.get_adapter("https://example.com").poolmanager
                      .connection_pool_kw["ssl_context"], truststore.SSLContext)
    monkeypatch.setitem(sys.modules, "truststore", None)
    assert type(web.os_ssl_context()) is ssl.SSLContext


def test_the_pdt_command_trusts_the_os_store_first(monkeypatch):
    calls = []
    monkeypatch.setattr(web, "trust_os_certificates", lambda: calls.append(1))
    monkeypatch.setattr(sys, "argv", ["pdt"])
    monkeypatch.setenv("UV_SYSTEM_CERTS", "")
    monkeypatch.delenv("UV_SYSTEM_CERTS")
    cli.main()
    assert calls == [1]
    assert os.environ["UV_SYSTEM_CERTS"] == "1"


@pytest.mark.parametrize("path", scripts(), ids=lambda path: path.name)
def test_each_script_trusts_the_os_store_before_any_third_party_import(path):
    text = path.read_text()
    assert '#     "truststore",' in text
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.Expr) and CALL in ast.unparse(node):
            break
        names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            top = name.split(".")[0]
            assert top in sys.stdlib_module_names or top in ("__future__", "pdt"), (
                f"{path.name} imports {name} before {CALL}")
    else:
        pytest.fail(f"{path.name} never calls {CALL}")


@pytest.mark.parametrize("path", sorted((SRC / "utils").glob("*.py")), ids=lambda path: path.name)
def test_each_https_call_in_the_job_side_utils_trusts_the_os_store(path):
    text = path.read_text()
    assert CALL not in text or path.name == "web.py"
    for call, needs in HTTPS.items():
        for start in [m.start() for m in re.finditer(re.escape(call), text)]:
            # The argument is on the call's line, or within the call's next few lines.
            assert needs in text[start:start + 250], f"{path.name}: {call}"
