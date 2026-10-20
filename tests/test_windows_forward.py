import json
import re
import subprocess
import sys
import types
import zipfile
from pathlib import Path, PureWindowsPath

import pytest
import yaml

from conftest import add_app
from pdt import config, windows_remote, windows_uv
from pdt.deploy import DECLINED
from pdt.windows_remote import Connection, Target


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.delenv("PDT_JSON_OUTPUT", raising=False)
    (tmp_path / "pdt.yml").write_text(
        "platform:\n  provider: windows\n  host: admin@pc01\n"
        "apps:\n  - name: my-report\n    platform:\n      host: admin@pc02\n")
    monkeypatch.chdir(tmp_path)
    add_app(tmp_path, "my-report", "schedule: hourly\ntimezone: local\n"
                                   "platform:\n  host: admin@pc03\nenv:\n  required: [API_KEY]\n")
    (tmp_path / "my-report" / "run.py").write_text("print('hi')\n")
    (tmp_path / "my-report" / ".env").write_text("API_KEY=secret-value\n")
    monkeypatch.setenv("API_KEY", "secret-value")
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: False)
    return config.merged_app("my-report")


def connection(kind="ssh", uv_version=windows_uv.VERSION):
    return Connection(Target("admin", "pc01"), "SSH (port 22)", "PC01\\admin", "PC01",
                      "C:\\ProgramData", "AMD64", uv_version, kind=kind)


class FakeSession:
    def __init__(self, codes=(0,), tmp=None):
        self.calls = []
        self.codes = list(codes)
        self.bundles = []

    def script(self, script):
        self.calls.append(("script", script))
        if "pdt-secret|" in script:
            return 'pdt-secret|{"API_KEY": "deployed-value"}\n'
        return ""

    def upload(self, source, destination):
        self.calls.append(("upload", str(destination)))
        if source.name == "bundle.zip":
            with zipfile.ZipFile(source) as archive:
                self.bundles.append({name: archive.read(name) for name in archive.namelist()})

    def download(self, source, destination):
        self.calls.append(("download", str(source), str(destination)))
        Path(destination).write_text("downloaded")

    def command(self, script):
        self.calls.append(("command", script))
        return self.codes.pop(0)


def incoming(remote) -> PureWindowsPath:
    upload = next(call[1] for call in remote.calls if call[0] == "upload")
    found = PureWindowsPath(upload).parent
    assert re.fullmatch(r"my-report-[0-9a-f]{12}", found.name)
    assert found.parent == PureWindowsPath("C:\\ProgramData\\pdt\\.incoming")
    return found


def test_the_bundle_holds_the_app_without_its_env_file_this_pdt_and_no_host(app):
    archive = windows_remote.bundle(app, {"API_KEY": "secret-value"})
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        project = yaml.safe_load(bundle.read("project/pdt.yml"))
        own = yaml.safe_load(bundle.read("project/my-report/config.yml"))
        secrets = json.loads(bundle.read("secrets.json"))
    assert "project/my-report/run.py" in names
    assert "project/my-report/.env" not in names
    assert "pdt/deploy_windows.py" in names
    assert "pdt/run_windows_task.py" in names
    assert not any("__pycache__" in name or ".pdt-lifecycle" in name for name in names)
    assert "host" not in project["platform"]
    assert "host" not in project["apps"][0]["platform"]
    assert "host" not in own["platform"]
    assert secrets == {"API_KEY": "secret-value"}


def test_a_bundle_for_a_command_without_secrets_holds_none(app):
    with zipfile.ZipFile(windows_remote.bundle(app, None)) as bundle:
        assert "secrets.json" not in bundle.namelist()


def test_deploy_prepares_uploads_runs_and_cleans_up_on_the_pc(app):
    remote = FakeSession()
    assert windows_remote.forward(connection(), app, "deploy", [], True, remote) == 0
    folder = incoming(remote)
    assert [call[0] for call in remote.calls] == ["script", "upload", "command", "script"]
    prepare = remote.calls[0][1]
    assert "Set-PdtAcl $root $false" in prepare
    assert f"$in = '{folder}'; New-Item -ItemType Directory -Path $in" in prepare
    assert "Set-PdtAcl $in $false" in prepare
    assert "/reset" not in prepare
    assert remote.calls[1][1] == str(folder / "bundle.zip")
    assert json.loads(remote.bundles[0]["secrets.json"]) == {"API_KEY": "secret-value"}
    script = remote.calls[2][1]
    assert script.startswith("$ErrorActionPreference = 'Stop'; ")
    assert f"$env:PDT_PROJECT = '{folder / 'project'}'" in script
    assert "$env:UV_CACHE_DIR = 'C:\\ProgramData\\pdt\\uv-cache'" in script
    assert ("& 'C:\\ProgramData\\pdt\\bin\\uv.exe' run --script "
            f"'{folder / 'pdt' / 'deploy_windows.py'}' 'deploy' 'my-report' '--yes'") in script
    assert "PDT_ASK_ON_CLIENT" not in script
    assert script.endswith("exit $LASTEXITCODE")
    assert remote.calls[3][1] == f"$in = '{folder}'; if (Test-Path $in) {{ Remove-Item -Recurse -Force $in }}"


def test_the_pc_cleans_up_even_when_the_command_fails(app):
    remote = FakeSession(codes=[1])
    assert windows_remote.forward(connection(), app, "runs", [], False, remote) == 1
    assert remote.calls[-1][1].endswith("Remove-Item -Recurse -Force $in }")


def test_options_after_the_command_reach_the_pc_after_a_separator(app):
    remote = FakeSession()
    windows_remote.forward(connection(), app, "logs", ["--follow", "3"], False, remote)
    script = next(call[1] for call in remote.calls if call[0] == "command")
    assert "'logs' 'my-report' '--' '--follow' '3'" in script


def test_the_forwarded_arguments_parse_on_the_pc(app, monkeypatch):
    from pdt import deploy_windows
    remote = FakeSession()
    windows_remote.forward(connection(), app, "runs", ["--json", "--limit", "5"], True, remote)
    script = next(call[1] for call in remote.calls if call[0] == "command")
    args = re.findall(r"'((?:[^']|'')*)'", script.split("deploy_windows.py' ", 1)[1].split(";")[0])
    seen = []
    monkeypatch.setattr(deploy_windows.runs_cli, "runs", lambda lister, name, rest: seen.append(rest) or 0)
    monkeypatch.setattr(deploy_windows.sys, "platform", "win32")
    monkeypatch.setattr(deploy_windows.sys, "argv", ["deploy_windows.py", *args])
    app_without_host = Path(app["dir"]).parent / "pdt.yml"
    app_without_host.write_text("platform:\n  provider: windows\n")
    (Path(app["dir"]) / "config.yml").write_text("schedule: hourly\ntimezone: local\n")
    assert deploy_windows.main() == 0
    assert seen == [["--json", "--limit", "5"]]


def test_a_pc_without_the_pinned_uv_gets_it_uploaded_and_installed(app, monkeypatch, tmp_path):
    monkeypatch.setattr(windows_remote.windows_uv, "fetch",
                        lambda processor: tmp_path / f"uv-{processor}.exe")
    remote = FakeSession()
    assert windows_remote.forward(connection(uv_version="-"), app, "runs", [], False, remote) == 0
    folder = incoming(remote)
    assert ("upload", str(folder / "uv.exe")) in remote.calls
    script = next(call[1] for call in remote.calls if call[0] == "command")
    assert "$target = 'C:\\ProgramData\\pdt\\bin\\uv.exe'" in script
    assert "Copy-Item -Force -Path (Join-Path $in 'uv.exe') -Destination $new" in script
    assert "[IO.File]::Replace($new, $target, [NullString]::Value)" in script
    assert script.index("[IO.File]::Replace") < script.index("Set-PdtAcl $target $true")
    assert "Set-PdtAcl $bin $true" in script
    assert "secrets.json" not in remote.bundles[0]


def test_over_winrm_the_question_is_asked_here_and_the_command_runs_again_with_yes(
        app, monkeypatch):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(windows_remote.console, "confirm", lambda: True)
    remote = FakeSession(codes=[DECLINED, 0])
    assert windows_remote.forward(connection("winrm"), app, "destroy", [], False, remote) == 0
    first, second = [call[1] for call in remote.calls if call[0] == "command"]
    assert "$env:PDT_ASK_ON_CLIENT = '1'" in first
    assert "'destroy' 'my-report' 2>&1" in first
    assert "PDT_ASK_ON_CLIENT" not in second
    assert "'destroy' 'my-report' '--yes' 2>&1" in second
    assert second.endswith("'pdt-exit|' + $LASTEXITCODE")
    assert second.index("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)") < \
        second.index("2>&1")
    assert [call[0] for call in remote.calls].count("upload") == 1


def test_secrets_set_sends_the_value_from_stdin_and_no_other_secrets(app, monkeypatch):
    monkeypatch.setattr(windows_remote.sys, "stdin", __import__("io").StringIO("new-value\n"))
    monkeypatch.delenv("API_KEY")
    remote = FakeSession()
    assert windows_remote.forward(connection(), app, "secrets", ["set", "API_KEY"], True, remote) == 0
    assert remote.bundles[0]["stdin.txt"] == b"new-value\n"
    assert "secrets.json" not in remote.bundles[0]
    script = next(call[1] for call in remote.calls if call[0] == "command")
    assert "Get-Content -Raw -Encoding UTF8 (Join-Path $in 'stdin.txt') | & " in script


def test_over_winrm_a_no_changes_nothing(app, monkeypatch, capsys):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(windows_remote.console, "confirm", lambda: False)
    remote = FakeSession(codes=[DECLINED])
    assert windows_remote.forward(connection("winrm"), app, "destroy", [], False, remote) == DECLINED
    assert [call[0] for call in remote.calls].count("command") == 1
    assert "Aborted; nothing was changed." in capsys.readouterr().out


def test_secrets_get_reads_the_pc_secret_and_saves_the_file_on_this_computer(app):
    remote = FakeSession()
    assert windows_remote.forward(connection(), app, "secrets", ["get"], True, remote) == 0
    assert [call[0] for call in remote.calls] == ["script"]
    saved = Path(app["dir"]) / ".env.windows"
    assert saved.read_text() == "API_KEY=deployed-value\n"


@pytest.mark.parametrize("dest,saved", [("copy.csv", "copy.csv"), (".", "a.csv"), (None, "a.csv")])
def test_storage_get_saves_the_file_on_this_computer(app, tmp_path, dest, saved):
    remote = FakeSession()
    rest = ["get", "runs/a.csv"] + ([str(tmp_path / dest)] if dest else [])
    assert windows_remote.forward(connection(), app, "storage", rest, True, remote) == 0
    folder = incoming(remote)
    command = next(call[1] for call in remote.calls if call[0] == "command")
    assert f"'--' 'get' 'runs/a.csv' '{folder / 'download' / 'file'}'" in command
    target = (tmp_path / saved) if dest else Path(saved)
    assert ("download", str(folder / "download" / "file"), str(target)) in remote.calls
    assert target.read_text() == "downloaded"


def test_ssh_gives_the_command_a_terminal_when_there_is_one(monkeypatch):
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 3, "", "")

    session = windows_remote.SshSession(connection(), run)
    assert session.command("Get-Date") == 3
    args, kwargs = calls[0]
    assert args[:2] == ["ssh", "-t"]
    assert kwargs == {}


def test_ssh_with_json_output_passes_only_the_json_to_stdout_even_at_a_terminal(
        monkeypatch, capsys):
    monkeypatch.setenv("PDT_JSON_OUTPUT", "1")
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)

    def run(args, **kwargs):
        assert kwargs["stdout"] is subprocess.PIPE
        assert "-t" not in args
        return subprocess.CompletedProcess(args, 0, '[{"id": "1"}]\n', None)

    assert windows_remote.SshSession(connection(), run).command("x") == 0
    assert capsys.readouterr().out == '[{"id": "1"}]\n'


@pytest.mark.parametrize("name,remote", [("pc01", "pc01:/C:/ProgramData/pdt/x.zip"),
                                         ("fe80::1", "[fe80::1]:/C:/ProgramData/pdt/x.zip")])
def test_scp_names_the_user_and_the_sftp_path(monkeypatch, name, remote):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    found = Connection(Target("CORP\\admin", name), "SSH (port 22)", "CORP\\admin", "PC01",
                       "C:\\ProgramData", "AMD64", "-")
    windows_remote.SshSession(found, run).upload(Path("/tmp/x.zip"),
                                                 PureWindowsPath("C:\\ProgramData\\pdt\\x.zip"))
    (args,) = calls
    assert args[0] == "scp"
    assert "User=CORP\\admin" in args
    assert args[-3:] == ["--", "/tmp/x.zip", remote]


def test_a_failed_scp_names_the_reason():
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "scp: dest open: Permission denied\n")

    with pytest.raises(windows_remote.RemoteError, match="Permission denied"):
        windows_remote.SshSession(connection(), run).upload(
            Path("/tmp/x.zip"), PureWindowsPath("C:\\x.zip"))


def test_tagged_lines_go_to_their_stream_and_the_last_one_is_the_exit_code(monkeypatch, capsys):
    code = None
    for line in ["pdt-out|Plan:", "pdt-err|warning: slow", "pdt-exit|7"]:
        code = windows_remote.show_tagged(line, code)
    assert code == 7
    assert capsys.readouterr().out == "Plan:\nwarning: slow\n"


def test_a_command_run_for_another_computer_stops_at_the_question(monkeypatch):
    from pdt import deploy
    monkeypatch.setenv("PDT_ASK_ON_CLIENT", "1")
    with pytest.raises(SystemExit) as stopped:
        deploy.proceed(False)
    assert stopped.value.code == DECLINED
    assert deploy.proceed(True) is True


def fake_pypsrp(monkeypatch, outputs, state, errors=()):
    states = types.SimpleNamespace(RUNNING="Running", COMPLETED="Completed", FAILED="Failed")

    class Shell:
        def __init__(self, pool):
            self.output, self.state = [], states.RUNNING
            self.streams = types.SimpleNamespace(error=list(errors))
            self.batches = list(outputs)

        def add_script(self, script):
            self.script = script

        def begin_invoke(self):
            pass

        def poll_invoke(self, timeout=None):
            if self.batches:
                self.output.extend(self.batches.pop(0))
            if not self.batches:
                self.state = state

    class Pool:
        def __init__(self, wsman):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    powershell = types.ModuleType("pypsrp.powershell")
    powershell.PowerShell, powershell.RunspacePool = Shell, Pool
    complex_objects = types.ModuleType("pypsrp.complex_objects")
    complex_objects.PSInvocationState = states
    monkeypatch.setitem(sys.modules, "pypsrp", types.ModuleType("pypsrp"))
    monkeypatch.setitem(sys.modules, "pypsrp.powershell", powershell)
    monkeypatch.setitem(sys.modules, "pypsrp.complex_objects", complex_objects)


def winrm(monkeypatch):
    client = types.SimpleNamespace(wsman=object())
    return windows_remote.WinRMSession(connection("winrm"), lambda *args: client)


def test_winrm_streams_each_line_as_it_arrives_and_returns_the_exit_code(monkeypatch, capsys):
    fake_pypsrp(monkeypatch, [["pdt-out|Plan:"], ["pdt-out|done", "pdt-exit|0"]], "Completed")
    assert winrm(monkeypatch).command("x") == 0
    assert capsys.readouterr().out == "Plan:\ndone\n"


def test_winrm_reports_a_failed_bootstrap_and_returns_non_zero(monkeypatch, capsys):
    fake_pypsrp(monkeypatch, [["pdt-out|x"]], "Failed", errors=["Expand-Archive: bad zip"])
    assert winrm(monkeypatch).command("x") == 1
    assert "Expand-Archive: bad zip" in capsys.readouterr().out


def test_a_forwarded_b64_value_satisfies_a_path_variable_on_the_pc(tmp_path, monkeypatch):
    from pdt import deploy_common
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.chdir(tmp_path)
    add_app(tmp_path, "my-report", "schedule: hourly\nenv:\n  required: [CERT_PATH]\n")
    monkeypatch.delenv("CERT_PATH", raising=False)
    monkeypatch.setenv("CERT_B64", "Y2VydA==")
    assert deploy_common.gather_secrets(config.merged_app("my-report")) == {"CERT_B64": "Y2VydA=="}


def test_storage_get_keeps_its_flags_and_does_not_take_one_as_the_destination(app):
    remote = FakeSession()
    windows_remote.forward(connection(), app, "storage", ["get", "runs/a.csv", "--yes"], True, remote)
    folder = incoming(remote)
    command = next(call[1] for call in remote.calls if call[0] == "command")
    assert f"'--' 'get' 'runs/a.csv' '{folder / 'download' / 'file'}' '--yes'" in command
    assert ("download", str(folder / "download" / "file"), "a.csv") in remote.calls


def test_ssh_json_output_at_a_terminal_can_still_ask_for_a_password(monkeypatch):
    monkeypatch.setenv("PDT_JSON_OUTPUT", "1")
    monkeypatch.setattr(windows_remote, "can_prompt", lambda interactive: True)
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "[]\n", None)

    windows_remote.SshSession(connection(), run).command("x")
    assert "BatchMode=yes" not in seen[0]
    assert "-t" not in seen[0]


def test_secrets_set_pipes_the_value_as_utf8(app, monkeypatch):
    monkeypatch.setattr(windows_remote.sys, "stdin", __import__("io").StringIO("päss"))
    remote = FakeSession()
    windows_remote.forward(connection(), app, "secrets", ["set", "API_KEY"], True, remote)
    assert remote.bundles[0]["stdin.txt"] == "päss".encode()
    script = next(call[1] for call in remote.calls if call[0] == "command")
    assert "$OutputEncoding = [Text.UTF8Encoding]::new($false); Get-Content" in script
    assert "$env:PYTHONIOENCODING = 'utf-8'" in script


def test_the_ssh_command_line_fits_the_windows_shell_limit(app, monkeypatch, tmp_path):
    monkeypatch.setattr(windows_remote.windows_uv, "fetch", lambda processor: tmp_path / "uv.exe")
    remote = FakeSession()
    windows_remote.forward(connection(uv_version="-"), app, "deploy", [], True, remote)
    script = next(call[1] for call in remote.calls if call[0] == "command")
    args = windows_remote.ssh_args(Target("CORP\\admin", "pc01.example.com"), script, True)
    assert len(" ".join(args[args.index("--") + 2:])) < 8191
