import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from pdt import autoidm_alerts, cli, config, lifecycle, register_cli
from pdt.utils import autoidm

AT = datetime(2026, 10, 13, 14, 2, 7, 123456, tzinfo=timezone.utc)
PARAMS = {"project": "contoso-jobs", "app": "sync-users", "provider": "windows",
          "run_id": "20261013T140200Z", "deployed": True, "trigger": "schedule", "exit_code": 1}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    for name in (autoidm.ID_ENV, autoidm.KEY_ENV, autoidm.URL_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "pdt"))
    return tmp_path / "pdt"


@pytest.fixture
def backend(folder):
    """A local backend that records each request with whether installation.key existed."""
    seen = []
    answers = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append({"path": self.path, "raw": raw, "body": json.loads(raw),
                         "authorization": self.headers.get("Authorization"),
                         "key_existed": (folder / autoidm.KEY_FILE).is_file()})
            status, body = answers.pop(0) if answers else (202, {"key": "events/x.json"})
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen, answers
    server.shutdown()


def pem(key):
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def registered(folder, url, installation_id="ins_4f8a1c2e9b"):
    key = Ed25519PrivateKey.generate()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / autoidm.KEY_FILE).write_bytes(pem(key))
    (folder / autoidm.INFO_FILE).write_text(json.dumps(
        {"installation_id": installation_id, "api_url": url}))
    return key


def test_a_signed_request_carries_the_contract_claims_and_verifies_with_the_public_key(backend):
    url, seen, _answers = backend
    key = registered(autoidm.folder(), url)
    assert autoidm.post("/v1/events", {"version": 1, "event": {"name": "x"}}) == {
        "key": "events/x.json"}
    request = seen[0]
    token = request["authorization"].removeprefix("Bearer ")
    assert jwt.get_unverified_header(token) == {"alg": "EdDSA", "typ": "JWT"}
    claims = jwt.decode(token, key.public_key(), algorithms=["EdDSA"], audience="pdt-backend")
    assert claims["iss"] == "ins_4f8a1c2e9b"
    assert claims["exp"] - claims["iat"] == 300
    assert re.fullmatch(r"[0-9a-f]{32}", claims["jti"])
    assert claims["body_sha256"] == hashlib.sha256(request["raw"]).hexdigest()
    assert request["path"] == "/v1/events"


def test_each_token_has_a_new_jti(backend):
    url, seen, _answers = backend
    registered(autoidm.folder(), url)
    autoidm.post("/v1/events", {})
    autoidm.post("/v1/events", {})
    jtis = {jwt.decode(r["authorization"].removeprefix("Bearer "),
                       options={"verify_signature": False})["jti"] for r in seen}
    assert len(jtis) == 2


def test_the_environment_wins_over_the_files_only_when_all_three_are_set(folder, monkeypatch):
    registered(folder, "https://files.example")
    env_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(autoidm.ID_ENV, "ins_0000000000")
    monkeypatch.setenv(autoidm.KEY_ENV, pem(env_key).decode())
    assert autoidm.installation().installation_id == "ins_4f8a1c2e9b"
    monkeypatch.setenv(autoidm.URL_ENV, "https://env.example/")
    found = autoidm.installation()
    assert (found.installation_id, found.api_url) == ("ins_0000000000", "https://env.example")
    assert found.key.public_key() == env_key.public_key()


def test_with_no_installation_a_post_says_to_run_pdt_register(folder):
    assert autoidm.installation() is None
    with pytest.raises(autoidm.AutoidmError, match="run pdt register --code CODE"):
        autoidm.post("/v1/events", {})


def test_register_writes_the_key_before_the_request_and_then_installation_json(backend, capsys):
    url, seen, answers = backend
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": "cus_7a1c09e4b2"}))
    assert register_cli.register(" kq7m-2rtx ", url + "/") == 0
    request = seen[0]
    assert request["path"] == "/v1/register" and request["key_existed"]
    assert request["authorization"] is None
    assert request["body"]["code"] == "KQ7M-2RTX"
    assert set(request["body"]) == {"code", "public_key", "hostname", "pdt_version"}
    folder = autoidm.folder()
    assert json.loads((folder / autoidm.INFO_FILE).read_text()) == {
        "installation_id": "ins_4f8a1c2e9b", "api_url": url}
    assert os.name == "nt" or (folder / autoidm.KEY_FILE).stat().st_mode & 0o777 == 0o600
    assert autoidm.installation().installation_id == "ins_4f8a1c2e9b"
    out = capsys.readouterr().out
    assert "Registered this computer to AutoIDM as ins_4f8a1c2e9b." in out
    assert f"Saved in: {folder}" in out


def test_a_retry_after_a_failed_register_sends_the_same_public_key(backend):
    url, seen, answers = backend
    answers.append((500, {"error": "internal", "message": "try again"}))
    assert register_cli.register("KQ7M-2RTX", url) == 1
    assert not (autoidm.folder() / autoidm.INFO_FILE).exists()
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": "cus_7a1c09e4b2"}))
    assert register_cli.register("KQ7M-2RTX", url) == 0
    assert seen[0]["body"]["public_key"] == seen[1]["body"]["public_key"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", seen[0]["body"]["public_key"])


@pytest.mark.parametrize(("status", "code", "said"), [
    (404, "unknown_code", "AutoIDM does not know the code KQ7M-2RTX, or it expired."),
    (409, "code_used", "Another computer already used the code KQ7M-2RTX."),
    (400, "bad_request", "AutoIDM answered HTTP 400: code has the wrong format"),
])
def test_register_names_each_refusal_and_writes_no_installation(backend, capsys, status, code,
                                                                  said):
    url, _seen, answers = backend
    answers.append((status, {"error": code, "message": "code has the wrong format"}))
    assert register_cli.register("KQ7M-2RTX", url) == 1
    assert said in capsys.readouterr().out
    assert not (autoidm.folder() / autoidm.INFO_FILE).exists()


def test_register_on_a_registered_computer_stops_and_sends_nothing(backend, capsys):
    url, seen, _answers = backend
    registered(autoidm.folder(), url)
    assert register_cli.register("KQ7M-2RTX", url) == 1
    assert seen == []
    assert "already registered to AutoIDM as ins_4f8a1c2e9b" in capsys.readouterr().out


def test_pdt_register_needs_a_code_and_defaults_the_api_url():
    args = cli.build_parser().parse_args(["register", "--code", "KQ7M-2RTX"])
    assert (args.code, args.api_url) == ("KQ7M-2RTX", config.AUTOIDM_API_URL)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["register"])


def test_the_autoidm_type_posts_the_event_as_the_contract_body(backend):
    url, seen, _answers = backend
    registered(autoidm.folder(), url)
    event = lifecycle.Event("job:run-failed", AT, PARAMS)
    autoidm_alerts.deliver(event, {"type": "autoidm"}, False)
    assert seen[0]["path"] == "/v1/events"
    assert seen[0]["body"] == {"version": 1, "event": {
        "name": "job:run-failed", "at": "2026-10-13T14:02:07Z", "params": PARAMS}}


def test_an_autoidm_entry_takes_no_key_of_its_own():
    assert lifecycle.parse_entry({"type": "autoidm", "events": ["all"]}, "pdt.yml")["type"] == (
        "autoidm")
    with pytest.raises(config.ConfigError, match="unknown key 'webhook'"):
        lifecycle.parse_entry({"type": "autoidm", "webhook": "x", "events": ["all"]}, "pdt.yml")


def test_an_unregistered_computer_prints_the_event_as_not_delivered(project, folder, capsys):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: windows\nalerts:\n  - type: autoidm\n    events: [all]\n")
    lifecycle.emit("job:run-failed", **PARAMS)
    lifecycle.flush()
    captured = capsys.readouterr()
    assert "not delivered to autoidm" in captured.out
    assert "run pdt register --code CODE" in captured.err
