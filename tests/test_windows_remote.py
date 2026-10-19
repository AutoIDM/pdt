import base64
import subprocess
from types import SimpleNamespace

import pytest

from conftest import add_app
from pdt import config, windows_remote
from pdt.config import validate_app, windows_host_problem
from pdt.windows_remote import Connection, RemoteError, Target

ADMIN = "pdt-probe|True|CORP\\admin|PC01|C:\\ProgramData\n"
NOT_ADMIN = "pdt-probe|False|PC01\\helpdesk|PC01|C:\\ProgramData\n"


@pytest.mark.parametrize("host,user,name", [
    ("pc01.example.com", "", "pc01.example.com"),
    ("admin@pc01", "admin", "pc01"),
    ("CORP\\admin@pc01", "CORP\\admin", "pc01"),
    ("admin@corp.example.com@pc01", "admin@corp.example.com", "pc01"),
])
def test_host_names_an_optional_user_and_the_pc(host, user, name):
    target = Target.parse(host)
    assert (target.user, target.name) == (user, name)


@pytest.mark.parametrize("host", ["pc01", "pc01.example.com", "pc01.example.com.", "admin@pc01",
                                  "CORP\\admin@pc01", "10.0.0.5", "fe80::1", "fe80::1%12"])
def test_a_pc_name_or_address_is_a_valid_host(host):
    assert windows_host_problem(host) == ""


@pytest.mark.parametrize("host", ["https://pc01", "pc01:5986", "pc 01", "@pc01", "admin@", ":",
                                  "::::", "a:b", "pc..example.com", "ad\x07min@pc01"])
def test_a_url_a_port_or_a_space_is_not_a_host(host):
    assert windows_host_problem(host) != ""


def test_an_empty_host_means_this_pc_only_on_windows(monkeypatch):
    monkeypatch.setattr(config, "os", SimpleNamespace(name="nt"))
    assert windows_host_problem("") == ""
    monkeypatch.setattr(config, "os", SimpleNamespace(name="posix"))
    assert "not Windows" in windows_host_problem("")


def test_validate_names_a_bad_host(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n  host: https://pc01\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert any("platform.host" in p for p in validate_app("my-report"))


def test_validate_rejects_a_host_for_a_cloud_provider(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n  host: pc01\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert any("only with provider: windows" in p for p in validate_app("my-report"))


def test_each_app_can_name_its_own_pc(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n  host: pc01\n")
    add_app(project, "first", "schedule: daily\n")
    add_app(project, "second", "schedule: daily\nplatform:\n  host: admin@pc02\n")
    assert config.merged_app("first")["platform"]["host"] == "pc01"
    assert config.merged_app("second")["platform"]["host"] == "admin@pc02"
    assert validate_app("second") == []


def decoded(args):
    return base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")


def test_ssh_runs_the_script_in_powershell_on_the_pc():
    args = windows_remote.ssh_args(Target("admin", "pc01"), "Get-Date", terminal=True)
    assert args[0] == "ssh"
    assert "BatchMode=yes" not in args
    assert "StrictHostKeyChecking=accept-new" in args
    assert args[args.index("-l") + 1] == "admin"
    assert args[args.index("--") + 1:args.index("--") + 3] == ["pc01", "powershell"]
    assert decoded(args) == "Get-Date"


def test_ssh_without_a_terminal_asks_for_nothing():
    args = windows_remote.ssh_args(Target("", "pc01"), "Get-Date", terminal=False)
    assert "BatchMode=yes" in args
    assert "-l" not in args
    assert args[args.index("--") + 1] == "pc01"


def fake_ssh(monkeypatch, returncode, stdout="", stderr=""):
    calls = []
    monkeypatch.setattr(windows_remote.shutil, "which", lambda name: "/usr/bin/ssh")
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: False)

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return calls, run


def test_ssh_probe_signs_in_as_an_administrator(monkeypatch):
    calls, run = fake_ssh(monkeypatch, 0, ADMIN)
    result = windows_remote.probe_ssh(Target("admin", "pc01"), run)
    assert result == Connection(Target("admin", "pc01"), "SSH (port 22)", "CORP\\admin", "PC01",
                                "C:\\ProgramData")
    assert decoded(calls[0]) == windows_remote.PROBE_SCRIPT


def test_ssh_probe_rejects_an_account_without_administrator_rights(monkeypatch):
    _calls, run = fake_ssh(monkeypatch, 0, NOT_ADMIN)
    result = windows_remote.probe_ssh(Target("helpdesk", "pc01"), run)
    assert result.startswith("PC01\\helpdesk signed in, but Windows did not give the session")


@pytest.mark.parametrize("stderr,reason", [
    ("ssh: Could not resolve hostname pc01: nodename nor servname provided",
     "this computer cannot find a PC named pc01"),
    ("ssh: connect to host pc01 port 22: Connection refused",
     "port 22 refused the connection, so OpenSSH Server is off or blocked"),
    ("ssh: connect to host pc01 port 22: Operation timed out",
     "port 22 did not answer within 10 seconds"),
    ("admin@pc01: Permission denied (publickey,password,keyboard-interactive).",
     "the PC refused the sign-in for admin"),
    ("kex_exchange_identification: read: Connection reset by peer",
     "kex_exchange_identification: read: Connection reset by peer"),
])
def test_ssh_probe_says_why_it_failed(monkeypatch, stderr, reason):
    _calls, run = fake_ssh(monkeypatch, 255, stderr=stderr)
    assert windows_remote.probe_ssh(Target("admin", "pc01"), run) == reason


def test_ssh_probe_without_an_ssh_client(monkeypatch):
    monkeypatch.setattr(windows_remote.shutil, "which", lambda name: None)
    assert windows_remote.probe_ssh(Target("", "pc01")) == "this computer has no ssh command"


class FakeClient:
    def __init__(self, outcomes, opened):
        self.outcomes = outcomes
        self.opened = opened

    def __call__(self, name, port, ssl, user, password):
        self.opened.append((port, ssl, password))
        return self

    def execute_ps(self, script):
        assert script == windows_remote.PROBE_SCRIPT
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, None, False


class SSLError(Exception):
    pass


class AuthenticationError(Exception):
    pass


def test_winrm_probe_tries_https_then_http(monkeypatch):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: False)
    opened = []
    client = FakeClient([SSLError("certificate verify failed"), ADMIN], opened)
    result = windows_remote.probe_winrm(Target("", "pc01"), lambda name, port: "", client)
    assert result.transport == "WinRM (HTTP, port 5985, Kerberos, encrypted messages)"
    assert result.notes == ("port 5986 uses an HTTPS certificate that this computer does not trust",)
    assert opened == [(5986, True, None), (5985, False, None)]


def test_winrm_probe_asks_for_a_password_once_when_kerberos_does_not_sign_in(monkeypatch):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)
    opened, asked = [], []
    client = FakeClient([AuthenticationError("401"), ADMIN], opened)
    result = windows_remote.probe_winrm(Target("CORP\\admin", "pc01"),
                                        lambda name, port: "" if port == 5986 else "port 5985 did not answer", client,
                                        lambda prompt: asked.append(prompt) or "secret")
    assert result.transport == "WinRM (HTTPS, port 5986)"
    assert asked == ["Password for CORP\\admin on pc01: "]
    assert opened == [(5986, True, None), (5986, True, "secret")]


def test_winrm_probe_names_each_port_that_failed(monkeypatch):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: False)
    def refused(address, timeout):
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(windows_remote.socket, "create_connection", refused)
    result = windows_remote.probe_winrm(Target("", "pc01"), client=FakeClient([], []))
    assert result == "port 5986 did not answer; port 5985 did not answer"


def test_winrm_probe_says_once_that_the_name_is_unknown(monkeypatch):
    def unknown(address, timeout):
        raise windows_remote.socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(windows_remote.socket, "create_connection", unknown)
    result = windows_remote.probe_winrm(Target("", "pc01"), client=FakeClient([], []))
    assert result == "this computer cannot find a PC named pc01"


def test_winrm_probe_rejects_a_filtered_local_administrator(monkeypatch):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: False)
    result = windows_remote.probe_winrm(Target("helpdesk", "pc01"), lambda name, port: "" if port == 5985 else "port 5986 did not answer",
                                        FakeClient([NOT_ADMIN], []))
    assert "did not give the session administrator rights" in result


def connection(transport):
    return Connection(Target("", "pc01"), transport, "CORP\\admin", "PC01", "C:\\ProgramData")


def test_connect_uses_ssh_and_does_not_try_winrm_when_ssh_works(capsys):
    tried = []
    probes = (("SSH", lambda target: tried.append("SSH") or connection("SSH (port 22)")),
              ("WinRM", lambda target: tried.append("WinRM") or "unused"))
    assert windows_remote.connect("pc01", "my-report", probes).transport == "SSH (port 22)"
    assert tried == ["SSH"]
    assert "Connected to PC01 over SSH (port 22) as CORP\\admin (administrator)." in \
        capsys.readouterr().out


def test_connect_says_why_ssh_failed_when_winrm_works(capsys):
    probes = (("SSH", lambda target: "port 22 did not answer within 10 seconds"),
              ("WinRM", lambda target: connection("WinRM (HTTPS, port 5986)")))
    windows_remote.connect("pc01", "my-report", probes)
    out = capsys.readouterr().out
    assert "SSH did not work: port 22 did not answer within 10 seconds." in out
    assert "over WinRM (HTTPS, port 5986)" in out


def test_connect_names_both_reasons_when_nothing_works():
    probes = (("SSH", lambda target: "port 22 refused the connection"),
              ("WinRM", lambda target: "port 5986 did not answer; port 5985 did not answer"))
    with pytest.raises(RemoteError) as raised:
        windows_remote.connect("admin@pc01", "my-report", probes)
    message = str(raised.value)
    assert "pdt could not sign in to pc01 as an administrator." in message
    assert "  SSH: port 22 refused the connection." in message
    assert "  WinRM: port 5986 did not answer; port 5985 did not answer." in message
    assert "pdt login my-report" in message


def test_winrm_probe_without_a_user_says_where_to_add_one(monkeypatch):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)
    result = windows_remote.probe_winrm(Target("", "pc01"), lambda name, port: "",
                                        FakeClient([AuthenticationError("401")] * 2, []))
    assert result == ("the PC refused a sign-in with this computer's Kerberos ticket; put the user "
                      "name in platform.host, for example admin@pc01")


def test_ssh_probe_that_hangs_gives_a_reason(monkeypatch):
    monkeypatch.setattr(windows_remote.shutil, "which", lambda name: "/usr/bin/ssh")
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: False)

    def hang(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    assert windows_remote.probe_ssh(Target("", "pc01"), hang) == \
        "the PC did not finish the sign-in within 2 minutes"


@pytest.mark.parametrize("output", ["pdt-probe|True\n", "pdt-probe|True||PC01|C:\\ProgramData\n",
                                    "pdt-probe|yes|a|b|c\n", "pdt-probe|True|a|b|c|extra\n"])
def test_a_broken_probe_line_is_a_reason_not_a_connection(monkeypatch, output):
    _calls, run = fake_ssh(monkeypatch, 0, output)
    assert windows_remote.probe_ssh(Target("", "pc01"), run).startswith(
        "the PC answered, but PowerShell did not run")


def test_winrm_probe_never_sends_a_rejected_password_again(monkeypatch):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)
    opened = []
    client = FakeClient([AuthenticationError("401"), AuthenticationError("401")], opened)
    result = windows_remote.probe_winrm(Target("CORP\\admin", "pc01"), lambda name, port: "",
                                        client, lambda prompt: "wrong")
    assert result == "the PC refused the sign-in for CORP\\admin"
    assert opened == [(5986, True, None), (5986, True, "wrong")]


def test_connect_names_the_https_failure_when_http_works(capsys):
    found = Connection(Target("", "pc01"), "WinRM (HTTP, port 5985, Kerberos, encrypted messages)",
                       "CORP\\admin", "PC01", "C:\\ProgramData",
                       ("port 5986 did not answer",))
    probes = (("SSH", lambda target: "port 22 did not answer within 10 seconds"),
              ("WinRM", lambda target: found))
    windows_remote.connect("pc01", "my-report", probes)
    assert "WinRM: port 5986 did not answer." in capsys.readouterr().out


class BadMechanismError(Exception):
    pass


class WinRMTransportError(Exception):
    def __init__(self, code):
        super().__init__(f"Bad HTTP response returned from the server. Code: {code}")
        self.code = code


@pytest.mark.parametrize("failure", [BadMechanismError("no credentials"), WinRMTransportError(401)])
def test_winrm_probe_asks_for_a_password_when_the_user_has_no_ticket_or_is_refused(
        monkeypatch, failure):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)
    opened = []
    result = windows_remote.probe_winrm(
        Target("admin", "pc01"), lambda name, port: "" if port == 5986 else "port 5985 did not answer",
        FakeClient([failure, ADMIN], opened), lambda prompt: "secret")
    assert result.transport == "WinRM (HTTPS, port 5986)"
    assert opened == [(5986, True, None), (5986, True, "secret")]


def test_a_connection_error_that_names_401_is_not_a_refused_sign_in(monkeypatch):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)
    result = windows_remote.probe_winrm(
        Target("admin", "pc401"), lambda name, port: "",
        FakeClient([ConnectionError("pc401 reset"), ConnectionError("pc401 reset")], []),
        lambda prompt: pytest.fail("asked for a password"))
    assert result == "port 5986: pc401 reset; port 5985: pc401 reset"
