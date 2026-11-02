import hashlib
import http.client
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


def test_the_environment_wins_over_the_files_only_when_all_four_are_set(folder, monkeypatch):
    registered("https://files.example")
    env_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(autoidm.ID_ENV, "ins_0000000000")
    monkeypatch.setenv(autoidm.KEY_ENV, pem(env_key).decode())
    monkeypatch.setenv(autoidm.URL_ENV, "https://env.example/")
    assert autoidm.registration(CUSTOMER).installation_id == "ins_4f8a1c2e9b"
    monkeypatch.setenv(autoidm.CUSTOMER_ENV, CUSTOMER)
    found = autoidm.registration(CUSTOMER)
    assert (found.installation_id, found.api_url, found.source) == (
        "ins_0000000000", "https://env.example", autoidm.ID_ENV)
    assert found.key.public_key() == env_key.public_key()


def test_a_cloud_job_finds_the_registration_in_its_env_json_secret(folder, monkeypatch):
    env_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({
        "OTHER": "x", autoidm.ID_ENV: "ins_0000000000", autoidm.KEY_ENV: pem(env_key).decode(),
        autoidm.URL_ENV: "https://env.example", autoidm.CUSTOMER_ENV: CUSTOMER}))
    assert autoidm.registration(CUSTOMER).installation_id == "ins_0000000000"


def test_a_cloud_registration_for_another_customer_is_an_error(folder, monkeypatch):
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({
        autoidm.ID_ENV: "ins_0000000000", autoidm.KEY_ENV: pem(Ed25519PrivateKey.generate()).decode(),
        autoidm.URL_ENV: "https://env.example", autoidm.CUSTOMER_ENV: "cus_0000000002"}))
    with pytest.raises(autoidm.AutoidmError, match="is for cus_0000000002, and pdt.yml names"):
        autoidm.registration(CUSTOMER)


@pytest.mark.parametrize(("path", "answer", "said"), [
    ("/v1/events", b"{not json", "with JSON that does not parse"),
    pytest.param("/v1/events", b'{"a": ' + b"[" * 100000, "with JSON that does not parse",
                 id="nested too deep"),
    ("/v1/uploads", b'{"url": "x"}', "answered /v1/uploads without key"),
    ("/v1/uploads", b'{"url": "x", "key": "k", "fields": []}', "/v1/uploads without fields"),
    ("/v1/uploads", b'{"url": "x", "key": "k", "fields": {}}', "'x', which is not https"),
    ("/v1/uploads", b'{"url": "http://example.com/b", "key": "k", "fields": {}}',
     "which is not https"),
    ("/v1/uploads", b'{"url": "https://[broken", "key": "k", "fields": {}}',
     "which does not parse"),
    ("/v1/events", http.client.IncompleteRead(b'{"key"', 10), "could not be reached"),
])
def test_a_malformed_answer_is_an_autoidm_error(folder, monkeypatch, path, answer, said):
    registered("https://api.example")
    found = autoidm.registration(CUSTOMER)

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(autoidm.urllib.request, "urlopen", lambda request, timeout: Answer())
    with pytest.raises(autoidm.AutoidmError, match=said):
        if path == "/v1/events":
            autoidm.post(found, path, {})
        else:
            autoidm.upload(found, {"filename": "a.log"}, b"x")


def test_register_with_an_answer_missing_the_customer_writes_nothing(backend, capsys):
    url, _seen, answers = backend
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b"}))
    assert register_cli.register("KQ7M-2RTX", url) == 1
    assert "AutoIDM answered /v1/register without customer_id" in capsys.readouterr().out
    assert [p.name.startswith("pending-") for p in autoidm.folder().iterdir()] == [True]


def test_a_retry_after_a_crash_before_the_metadata_sends_the_same_key(backend, monkeypatch):
    url, seen, answers = backend
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))

    write = config.write_text_atomically
    crashes = [KeyboardInterrupt]

    def crash_once(path, text, newline=None):
        if crashes and path.suffix == ".json":
            raise crashes.pop()
        write(path, text, newline)

    monkeypatch.setattr(config, "write_text_atomically", crash_once)
    with pytest.raises(KeyboardInterrupt):
        register_cli.register("KQ7M-2RTX", url)
    assert not autoidm.files(CUSTOMER)[0].exists()
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    assert register_cli.register("KQ7M-2RTX", url) == 0
    assert seen[0]["body"]["public_key"] == seen[1]["body"]["public_key"]
    assert autoidm.public_key(autoidm.registration(CUSTOMER).key) == seen[0]["body"]["public_key"]
    assert not list(autoidm.folder().glob("pending-*.key"))


def test_a_retry_after_a_crash_before_pdt_yml_is_saved_finishes_with_the_same_key(
        backend, tmp_path, monkeypatch):
    url, seen, answers = backend
    project = tmp_path / "project"
    project.mkdir()
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.chdir(project)
    save = config.save_managed_customer
    crashes = [KeyboardInterrupt]

    def crash_once(customer):
        if crashes:
            raise crashes.pop()
        save(customer)

    monkeypatch.setattr(config, "save_managed_customer", crash_once)
    for _ in range(2):
        answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    with pytest.raises(KeyboardInterrupt):
        register_cli.register("KQ7M-2RTX", url)
    assert list(autoidm.folder().glob("pending-*.key"))
    key = autoidm.files(CUSTOMER)[1].read_bytes()
    assert register_cli.register("KQ7M-2RTX", url) == 0
    assert seen[0]["body"]["public_key"] == seen[1]["body"]["public_key"]
    assert autoidm.files(CUSTOMER)[1].read_bytes() == key
    assert f"customer: {CUSTOMER}" in (project / "pdt.yml").read_text()
    assert not list(autoidm.folder().glob("pending-*.key"))


USER_SDDL = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FR;;;S-1-5-21-1-2-3-1001)"


SYSTEM_SDDL = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"


@pytest.mark.parametrize(("user", "sddl"), [
    ("S-1-5-21-1-2-3-1001", USER_SDDL),
    ("S-1-5-18", SYSTEM_SDDL),
])
def test_the_registering_account_gets_read_unless_it_is_system(user, sddl):
    rule = (register_cli.FOLDER_SDDL.replace("{user}", user)
            .replace(register_cli.SYSTEM_READ, ""))
    assert rule == sddl
    assert (f"$sddl = '{register_cli.FOLDER_SDDL}'.Replace('{{user}}', "
            "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value)"
            f".Replace('{register_cli.SYSTEM_READ}', '');" in register_cli.SET_FOLDER_SDDL)


@pytest.mark.skipif(os.name == "nt", reason="the access list is real on Windows")
@pytest.mark.parametrize(("found", "error"), [
    (f"{USER_SDDL}\r\n{USER_SDDL}\r\n", None),
    (f"{SYSTEM_SDDL}\r\n{SYSTEM_SDDL}\r\n", None),
    ("O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FR;;;LA)\r\n"
     "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FR;;;LA)\r\n", None),
    (f"{USER_SDDL}\r\n{USER_SDDL}(A;OICI;FR;;;BU)\r\n", f"BU\\), not {re.escape(USER_SDDL)}$"),
    (f"{USER_SDDL}\r\nO:S-1-5-21-1-2-3-1001D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)\r\n",
     f"not {re.escape(USER_SDDL)}$"),
    (f"{USER_SDDL}\r\n", "PowerShell printed O:BAD:PAI.*1001\\), not the access list"),
    ("", "PowerShell printed nothing"),
])
def test_the_folder_gets_system_administrators_and_the_user_and_is_read_back(
        tmp_path, monkeypatch, found, error):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["env"][register_cli.FOLDER_ENV]))
        return register_cli.subprocess.CompletedProcess(command, 0, found, "")

    monkeypatch.setattr(register_cli.os, "name", "nt")
    monkeypatch.setattr(register_cli.subprocess, "run", run)
    if error is None:
        register_cli.restrict_folder(tmp_path)
    else:
        with pytest.raises(RuntimeError, match=error):
            register_cli.restrict_folder(tmp_path)
    [(command, folder)] = calls
    assert command[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert ("$acl.SetSecurityDescriptorSddlForm($sddl); "
            "$acl.GetSecurityDescriptorSddlForm('Owner, Access'); "
            "$folder.SetAccessControl($acl); " in command[4])
    assert "New-Object" not in command[4]
    assert command[4].startswith("$ErrorActionPreference = 'Stop'; ")
    assert folder == str(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="reads a Windows access list")
def test_after_register_the_registering_account_can_read_the_key(backend):
    url, _seen, answers = backend
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    assert register_cli.register("KQ7M-2RTX", url) == 0
    key = autoidm.files(CUSTOMER)[1]
    shown = register_cli.subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
         f"([IO.FileInfo]'{key}').GetAccessControl().GetAccessRules($true, $true, "
         "[Security.Principal.SecurityIdentifier]) | "
         "ForEach-Object { \"$($_.IdentityReference) $($_.FileSystemRights.value__) "
         "$($_.AccessControlType) $($_.IsInherited)\" }"],
        stdin=register_cli.subprocess.DEVNULL, capture_output=True, text=True,
        check=True).stdout.splitlines()
    user = shown[0]
    assert sorted(shown[1:]) == sorted([
        f"S-1-5-18 {0x1F01FF} Allow True", f"S-1-5-32-544 {0x1F01FF} Allow True",
        f"{user} {0x120089} Allow True"])
    assert autoidm.registration(CUSTOMER).installation_id == "ins_4f8a1c2e9b"


def test_register_limits_the_folder_before_it_makes_any_key(backend, monkeypatch):
    url, _seen, answers = backend
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    found = []
    monkeypatch.setattr(register_cli, "restrict_folder",
                        lambda folder: found.append(sorted(p.name for p in folder.iterdir())))
    assert register_cli.register("KQ7M-2RTX", url) == 0
    assert found == [[]]


@pytest.mark.parametrize("error", [register_cli.subprocess.CalledProcessError(5, ["powershell"]),
                                   RuntimeError("its owner and access list are O:BAD:PAI")])
def test_a_folder_pdt_cannot_limit_gets_no_key(backend, monkeypatch, capsys, error):
    url, seen, _answers = backend

    def refused(folder):
        raise error

    monkeypatch.setattr(register_cli, "restrict_folder", refused)
    assert register_cli.register("KQ7M-2RTX", url) == 1
    assert seen == [] and list(autoidm.folder().iterdir()) == []
    assert "so it wrote no key" in " ".join(capsys.readouterr().out.split())


def test_an_interrupted_key_write_leaves_no_file(tmp_path, monkeypatch):
    def interrupted(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(config.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        register_cli.write_key(tmp_path / "k.key", Ed25519PrivateKey.generate())
    assert list(tmp_path.iterdir()) == []


def test_a_pending_key_pdt_cannot_read_is_replaced_by_a_new_one(backend):
    url, seen, answers = backend
    autoidm.folder().mkdir(parents=True)
    digest = hashlib.sha256(b"KQ7M2RTX").hexdigest()
    (autoidm.folder() / f"pending-{digest[:16]}.key").write_bytes(b"-----BEGIN PRIV")
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    assert register_cli.register("KQ7M-2RTX", url) == 0
    assert autoidm.public_key(autoidm.registration(CUSTOMER).key) == seen[0]["body"]["public_key"]


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


def test_register_in_a_project_adds_managed_services_to_pdt_yml(backend, tmp_path, monkeypatch,
                                                                capsys):
    url, _seen, answers = backend
    project = tmp_path / "project"
    project.mkdir()
    (project / "pdt.yml").write_text("# keep\nplatform:\n  provider: windows\n")
    monkeypatch.chdir(project)
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    assert register_cli.register("KQ7M-2RTX", url) == 0
    assert (project / "pdt.yml").read_text() == (
        f"# keep\nplatform:\n  provider: windows\n\nmanaged_services:\n  customer: {CUSTOMER}\n")
    assert "Saved managed_services: with customer: " in " ".join(capsys.readouterr().out.split())
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    assert register_cli.register("KQ7M-2RTX", url) == 1
    assert (project / "pdt.yml").read_text().count("managed_services:") == 1


def test_register_keeps_a_pdt_yml_that_names_another_customer(backend, tmp_path, monkeypatch,
                                                              capsys):
    url, _seen, answers = backend
    project = tmp_path / "project"
    project.mkdir()
    text = "managed_services:\n  customer: cus_0000000002\n"
    (project / "pdt.yml").write_text(text)
    monkeypatch.chdir(project)
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    assert register_cli.register("KQ7M-2RTX", url) == 0
    assert (project / "pdt.yml").read_text() == text
    assert "names another customer" in " ".join(capsys.readouterr().out.split())


def test_register_outside_a_project_says_what_to_add(backend, tmp_path, monkeypatch, capsys):
    url, _seen, answers = backend
    monkeypatch.chdir(tmp_path)
    answers.append((200, {"installation_id": "ins_4f8a1c2e9b", "customer_id": CUSTOMER}))
    assert register_cli.register("KQ7M-2RTX", url) == 0
    assert f"customer: {CUSTOMER}" in capsys.readouterr().out


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
