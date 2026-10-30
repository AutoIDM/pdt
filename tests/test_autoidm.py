import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from pdt import cli, config, register_cli
from pdt.utils import autoidm

CUSTOMER = "cus_7a1c09e4b2"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    for name in (*autoidm.SECRET_NAMES, "PDT_ENV_JSON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path / "pdt"))
    return tmp_path / "pdt" / "registrations"


@pytest.fixture
def backend(folder):
    """A local backend that records each request with the key files that existed. It answers
    from `answers` first; then /v1/uploads gets a place at /bucket, and any other path 202."""
    seen = []
    answers = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append({"path": self.path, "raw": raw,
                         "body": json.loads(raw) if self.path.startswith("/v1/") else None,
                         "content_type": self.headers.get("Content-Type"),
                         "authorization": self.headers.get("Authorization"),
                         "keys": sorted(p.name for p in folder.glob("*.key"))})
            if answers:
                status, body = answers.pop(0)
            elif self.path == "/v1/uploads":
                url = f"http://127.0.0.1:{self.server.server_port}/bucket"
                status, body = 200, {"url": url, "fields": {"key": "uploads/x"},
                                     "key": "uploads/x"}
            else:
                status, body = 202, {"key": "events/x.json"}
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


def registered(url, customer=CUSTOMER, installation_id="ins_4f8a1c2e9b"):
    key = Ed25519PrivateKey.generate()
    info, key_path = autoidm.files(customer)
    info.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(pem(key))
    info.write_text(json.dumps(
        {"installation_id": installation_id, "customer_id": customer, "api_url": url}))
    return key


def test_a_signed_request_carries_the_contract_claims_and_verifies_with_the_public_key(backend):
    url, seen, _answers = backend
    key = registered(url)
    found = autoidm.registration(CUSTOMER)
    assert autoidm.post(found, "/v1/events", {"version": 1, "event": {"name": "x"}}) == {
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
    registered(url)
    found = autoidm.registration(CUSTOMER)
    autoidm.post(found, "/v1/events", {})
    autoidm.post(found, "/v1/events", {})
    jtis = {jwt.decode(r["authorization"].removeprefix("Bearer "),
                       options={"verify_signature": False})["jti"] for r in seen}
    assert len(jtis) == 2


def test_each_customer_has_its_own_files_in_the_registrations_folder(folder):
    registered("https://one.example", CUSTOMER, "ins_0000000001")
    registered("https://two.example", "cus_0000000002", "ins_0000000002")
    assert sorted(p.name for p in folder.iterdir()) == [
        "cus_0000000002.json", "cus_0000000002.key", f"{CUSTOMER}.json", f"{CUSTOMER}.key"]
    assert autoidm.registration(CUSTOMER).installation_id == "ins_0000000001"
    assert autoidm.registration("cus_0000000002").source == str(folder / "cus_0000000002.json")
    assert autoidm.registration("cus_0000000003") is None


def test_the_environment_wins_over_the_files_only_when_all_three_are_set(folder, monkeypatch):
    registered("https://files.example")
    env_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(autoidm.ID_ENV, "ins_0000000000")
    monkeypatch.setenv(autoidm.KEY_ENV, pem(env_key).decode())
    assert autoidm.registration(CUSTOMER).installation_id == "ins_4f8a1c2e9b"
    monkeypatch.setenv(autoidm.URL_ENV, "https://env.example/")
    found = autoidm.registration(CUSTOMER)
    assert (found.installation_id, found.api_url, found.source) == (
        "ins_0000000000", "https://env.example", autoidm.ID_ENV)
    assert found.key.public_key() == env_key.public_key()


def test_a_cloud_job_finds_the_registration_in_its_env_json_secret(folder, monkeypatch):
    env_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({
        "OTHER": "x", autoidm.ID_ENV: "ins_0000000000", autoidm.KEY_ENV: pem(env_key).decode(),
        autoidm.URL_ENV: "https://env.example"}))
    assert autoidm.registration(CUSTOMER).installation_id == "ins_0000000000"


def test_register_writes_a_pending_key_before_the_request_then_the_customer_files(backend,
                                                                                  capsys):
    url, seen, answers = backend
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    assert register_cli.register(" kq7m-2rtx ", url + "/") == 0
    request = seen[0]
    assert request["path"] == "/v1/register"
    assert [name.startswith("pending-") for name in request["keys"]] == [True]
    assert request["authorization"] is None
    assert request["body"]["code"] == "KQ7M-2RTX"
    assert set(request["body"]) == {"code", "public_key", "hostname", "pdt_version"}
    info, key = autoidm.files(CUSTOMER)
    assert json.loads(info.read_text()) == {
        "installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER, "api_url": url}
    assert sorted(p.name for p in info.parent.iterdir()) == [info.name, key.name]
    assert os.name == "nt" or key.stat().st_mode & 0o777 == 0o600
    assert autoidm.registration(CUSTOMER).installation_id == "ins_4f8a1c2e9b"
    out = capsys.readouterr().out
    assert f"Registered this computer to AutoIDM for {CUSTOMER} as ins_4f8a1c2e9b." in out
    assert f"Saved in: {info}" in out


def test_a_retry_after_a_failed_register_sends_the_same_public_key(backend):
    url, seen, answers = backend
    answers.append((500, {"error": "internal", "message": "try again"}))
    assert register_cli.register("KQ7M-2RTX", url) == 1
    assert not autoidm.files(CUSTOMER)[0].exists()
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    assert register_cli.register("KQ7M2RTX", url) == 0
    assert seen[0]["body"]["public_key"] == seen[1]["body"]["public_key"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", seen[0]["body"]["public_key"])


@pytest.mark.parametrize(("status", "code", "said"), [
    (404, "unknown_code", "AutoIDM does not know the code KQ7M-2RTX, or it expired."),
    (409, "code_used", "Another computer already used the code KQ7M-2RTX."),
    (400, "bad_request", "AutoIDM answered HTTP 400: code has the wrong format"),
])
def test_register_names_each_refusal_and_writes_no_registration(backend, capsys, status, code,
                                                                said):
    url, _seen, answers = backend
    answers.append((status, {"error": code, "message": "code has the wrong format"}))
    assert register_cli.register("KQ7M-2RTX", url) == 1
    assert said in capsys.readouterr().out
    assert not autoidm.files(CUSTOMER)[0].exists()
    # A code AutoIDM will never take again leaves no pending key; any other refusal keeps it.
    assert bool(list(autoidm.folder().glob("pending-*.key"))) == (code == "bad_request")


def test_register_for_a_customer_this_computer_has_stops_and_names_the_file(backend, capsys):
    url, _seen, answers = backend
    registered(url)
    before = autoidm.files(CUSTOMER)[1].read_bytes()
    answers.append((200, {"installation_id": "ins_0000000009", "customer_id": CUSTOMER}))
    assert register_cli.register("KQ7M-2RTX", url) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert f"already registered to AutoIDM for {CUSTOMER} as ins_4f8a1c2e9b" in out
    assert str(autoidm.files(CUSTOMER)[0]) in out
    assert autoidm.files(CUSTOMER)[1].read_bytes() == before


def test_register_for_a_second_customer_keeps_the_first(backend):
    url, _seen, answers = backend
    registered(url)
    answers.append((200, {"installation_id": "ins_0000000009", "customer_id": "cus_0000000002"}))
    assert register_cli.register("KQ7M-2RTX", url) == 0
    assert autoidm.registration(CUSTOMER).installation_id == "ins_4f8a1c2e9b"
    assert autoidm.registration("cus_0000000002").installation_id == "ins_0000000009"


def test_pdt_register_needs_a_code_and_defaults_the_api_url():
    args = cli.build_parser().parse_args(["register", "--code", "KQ7M-2RTX"])
    assert (args.code, args.api_url) == ("KQ7M-2RTX", config.AUTOIDM_API_URL)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["register"])


def test_an_upload_sends_the_presigned_fields_and_the_file_as_multipart(backend):
    url, seen, answers = backend
    registered(url)
    answers.append((200, {"url": url + "/bucket", "fields": {"key": "uploads/x/a.log",
                                                             "policy": "p"},
                          "key": "uploads/x/a.log"}))
    answers.append((204, {}))
    found = autoidm.registration(CUSTOMER)
    body = {"kind": "log", "project": "example", "app": "report", "run_id": "r1",
            "filename": "r1.log"}
    assert autoidm.upload(found, body, b"line one\n") == "uploads/x/a.log"
    assert seen[0]["body"] == {"version": 1, **body, "size": 9}
    assert seen[1]["path"] == "/bucket"
    assert seen[1]["content_type"].startswith("multipart/form-data; boundary=")
    raw = seen[1]["raw"]
    assert b'name="key"\r\n\r\nuploads/x/a.log\r\n' in raw
    assert raw.index(b'name="policy"') < raw.index(b'name="file"; filename="r1.log"')
    assert b"\r\n\r\nline one\n\r\n" in raw
