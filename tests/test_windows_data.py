import base64
import json
import shutil
from pathlib import Path

import pytest

from conftest import add_app, plain
from pdt import config, deploy_windows


def test_machine_data_home_is_pdt_under_program_data(monkeypatch):
    monkeypatch.setenv("ProgramData", r"D:\ProgramData")
    assert config.machine_data_home() == Path(r"D:\ProgramData") / "pdt"


def test_machine_data_home_falls_back_to_the_system_drive(monkeypatch):
    monkeypatch.delenv("ProgramData", raising=False)
    assert config.machine_data_home() == Path(r"C:\ProgramData") / "pdt"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def windows_app(project, config_text="schedule: hourly\ntimezone: local\n"):
    add_app(project, "my-report", config_text)
    return config.merged_app("my-report")


@pytest.mark.parametrize(("command", "rest"), [
    ("deploy", []),
    ("destroy", []),
    ("login", []),
    ("secrets", ["diff"]),
    ("storage", ["ls", "state/"]),
    ("runs", ["--", "--json"]),
    ("logs", ["--", "1"]),
])
def test_local_windows_commands_stop_off_windows(
        project, monkeypatch, capsys, command, rest):
    windows_app(project)
    monkeypatch.setattr(deploy_windows.sys, "platform", "darwin")
    monkeypatch.setattr(deploy_windows.sys, "argv",
                        ["deploy_windows.py", command, "my-report", *rest])

    assert deploy_windows.main() == 1

    assert capsys.readouterr().out == (
        "error: the windows provider targets this computer, but the current operating "
        "system is not Windows. Set host: under platform: in pdt.yml to the name of the "
        "Windows PC, or run this command on the Windows PC.\n")


@pytest.mark.parametrize("command", ["login", "deploy", "destroy", "storage", "runs", "logs",
                                     "pause", "unpause", "start"])
def test_a_host_runs_every_command_on_that_pc(project, monkeypatch, command):
    windows_app(project)
    (project / "my-report" / "config.yml").write_text(
        (project / "my-report" / "config.yml").read_text() + "platform:\n  host: admin@pc01\n")
    reached, forwarded = [], []
    monkeypatch.setattr(deploy_windows.windows_remote, "connect",
                        lambda host, app_name: reached.append((host, app_name)) or "connection")
    monkeypatch.setattr(deploy_windows.windows_remote, "forward",
                        lambda connection, app, command, rest, yes:
                        forwarded.append((connection, command, rest, yes)) or 0)
    monkeypatch.setattr(deploy_windows, "deploy", lambda app, yes: pytest.fail("ran locally"))
    monkeypatch.setattr(deploy_windows.sys, "argv",
                        ["deploy_windows.py", command, "my-report", "--yes"])

    assert deploy_windows.main() == 0

    assert reached == [("admin@pc01", "my-report")]
    assert forwarded == ([] if command == "login" else [("connection", command, [], True)])


def test_login_checks_the_host_before_it_connects(project, monkeypatch, capsys):
    windows_app(project)
    (project / "my-report" / "config.yml").write_text(
        (project / "my-report" / "config.yml").read_text() + "platform:\n  host: https://pc01\n")
    monkeypatch.setattr(deploy_windows.windows_remote, "connect",
                        lambda host, app_name: pytest.fail("connected to a bad host"))
    monkeypatch.setattr(deploy_windows.sys, "argv", ["deploy_windows.py", "login", "my-report"])

    assert deploy_windows.main() == 1
    assert "platform.host in pdt.yml or my-report/config.yml" in capsys.readouterr().out


def test_app_folders_live_under_the_machine_data_home(project):
    folder = project / "ProgramData" / "pdt" / "my-report"
    assert deploy_windows.app_folder("my-report") == folder
    assert deploy_windows.storage_folder("my-report") == folder / "storage"
    assert deploy_windows.logs_folder("my-report") == folder / "logs"


def test_plan_names_the_copy_the_secrets_the_uv_and_the_folders(project):
    app = windows_app(project)
    data = project / "ProgramData" / "pdt"
    folder = data / "my-report"
    actions = deploy_windows.plan(app, "create", "hourly at minute 00", r"PC\jon",
                                  {"API_KEY": "x"}, False)
    assert plain(actions) == [
        "create Windows scheduled task pdt-my-report (runs as SYSTEM)",
        "run my-report hourly at minute 00 (machine local time)",
        f"copy the app to {folder / 'app'} and pdt {deploy_windows.windows_remote.pdt_version()} to "
        f"{folder / 'runtime'}; the task runs this copy, so a change to the project reaches "
        "the job at the next deploy",
        f"store env vars API_KEY in {folder / 'secrets.json'} (SYSTEM and Administrators only)",
        f"run uv {deploy_windows.windows_uv.VERSION} from {data / 'bin' / 'uv.exe'}",
        f"keep the app's run data in {folder} (SYSTEM and Administrators: full control; "
        "PC\\jon: modify in logs and storage)",
        f"write one log per run under {folder / 'logs'} (removed on destroy)",
        f"use folder {folder / 'storage'} for the app's files (kept after destroy)",
    ]


def test_plan_without_storage_has_no_storage_line(project):
    app = windows_app(project, "schedule: hourly\ntimezone: local\nstorage: false\n")
    actions = deploy_windows.plan(app, "create", "hourly at minute 00", r"PC\jon", {}, False)
    assert len(actions) == 7
    assert not any("app's files" in action for action in actions)


def test_the_release_holds_the_app_without_secret_files_this_pdt_and_the_secrets(project):
    app = windows_app(project)
    (project / "my-report" / ".env").write_text("API_KEY=x\n")
    (project / "my-report" / "run.py").write_text("print('hi')\n")
    release = deploy_windows.stage_release(app, {"API_KEY": "x"}, None)
    try:
        assert (release / "app" / "pdt.yml").is_file()
        assert (release / "app" / "my-report" / "run.py").is_file()
        assert not (release / "app" / "my-report" / ".env").exists()
        assert not (release / "app" / ".pdt-lifecycle").exists()
        assert (release / "runtime" / "pdt" / "run_windows_task.py").is_file()
        assert (release / "runtime" / "pdt" / "lifecycle.py").is_file()
        assert not list((release / "runtime").rglob("__pycache__"))
        assert json.loads((release / "secrets.json").read_text()) == {"API_KEY": "x"}
        assert not (release / "uv.exe").exists()
    finally:
        shutil.rmtree(release)


def test_the_release_is_staged_then_swapped_in_and_put_back_on_any_failure(project):
    app = windows_app(project)
    release = project / "release"
    release.mkdir()
    (release / "uv.exe").write_text("")
    folder = project / "ProgramData" / "pdt" / "my-report"
    uv = project / "ProgramData" / "pdt" / "bin" / "uv.exe"
    script = deploy_windows._release_script(app, release, "INSTALL; ", "REGISTER; ")
    body, rollback = script.split("} catch { ", 1)
    assert f"$new = '{folder / 'app'}.new'" in body
    assert f"$new = '{folder / 'runtime'}.new'" in body
    assert "Set-PdtAcl $bin $true" in body
    assert f"$target = '{uv}'" in body
    assert "[IO.File]::Replace($new, $target, [NullString]::Value)" in body
    assert "Set-PdtAcl $target $true" in body
    assert "Set-PdtAcl $cache $false" in body
    assert (body.index("New-Item -ItemType File -Path $placed") < body.index("Set-PdtAcl $placed $false")
            < body.index("[IO.File]::WriteAllBytes($placed") < body.index("INSTALL")
            < body.index("Rename-Item -Path \"$t.new\"")
            < body.index("Move-Item -Path $placed -Destination $secret") < body.index("REGISTER"))
    assert "icacls" not in script
    assert "Rename-Item -Path \"$t.old\" -NewName (Split-Path $t -Leaf)" in rollback
    assert "Remove-Item -Recurse -Force \"$t.new\"" in rollback
    assert "if ($newSecret) { Remove-Item -Force $secret" in rollback
    assert "Rename-Item -Path \"$secret.old\"" in rollback
    assert rollback.index("throw") < rollback.index("Remove-Item -Recurse -Force $old")


def test_the_acl_function_sets_owner_protection_and_the_rules_in_one_write():
    acl = deploy_windows.SET_ACL
    assert "$acl.SetAccessRuleProtection($true, $false)" in acl
    assert "SetOwner([Security.Principal.SecurityIdentifier]'S-1-5-32-544')" in acl
    assert "@('S-1-5-18', 'FullControl'), @('S-1-5-32-544', 'FullControl')" in acl
    assert "$rules += ,@('S-1-5-32-545', 'ReadAndExecute')" in acl
    assert acl.count("Set-Acl ") == 1


def test_folder_script_locks_the_app_folder_and_lets_the_user_change_logs_and_storage(project):
    app = windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report"
    script = deploy_windows._folder_script(app, r"PC\jon")
    assert f"$folder = '{folder}'" in script
    assert "Set-PdtAcl $folder $true" in script
    assert f"foreach ($sub in @('{folder / 'logs'}', '{folder / 'storage'}'))" in script
    assert "icacls $sub /grant 'PC\\jon:(OI)(CI)M'" in script
    assert "$folder /grant 'PC" not in script


def test_deploy_runs_one_elevated_script_that_registers_the_task_inside_the_rollback(
        project, monkeypatch):
    app = windows_app(project)
    scripts = []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append(script) or True)
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    (script,) = scripts
    assert script.startswith("$ErrorActionPreference = 'Stop'; ")
    assert script.index("$folder =") < script.index("try {") < script.index(
        "Register-ScheduledTask") < script.index("} catch {")


def test_an_elevated_step_starts_with_a_short_command_line_and_runs_a_file(tmp_path):
    long_script = tmp_path / "step.ps1"
    args = deploy_windows.run_args("powershell.exe", long_script, elevate=True)
    wrapper = base64.b64decode(args[-1]).decode("utf-16-le")
    assert "-Verb RunAs" in wrapper
    assert f'-File "{long_script}"' in wrapper
    assert len(" ".join(args)) < 2000
    assert deploy_windows.run_args("powershell.exe", long_script, elevate=False)[-2:] == [
        "-File", str(long_script)]


def test_deploy_creates_the_folders_before_registering_the_task(project, monkeypatch):
    app = windows_app(project)
    scripts = []
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append((script, kw)) or True)
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    (script, kw), = scripts
    assert kw == {"elevate": True}
    assert script.index("New-Item") < script.index("icacls") < script.index(
        "Register-ScheduledTask")


def test_deploy_says_what_it_does_before_each_slow_step(project, monkeypatch, capsys):
    app = windows_app(project)
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "managed")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run", lambda powershell, script, **kw: True)
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    out = capsys.readouterr().out
    assert out.index("Checking Windows scheduled task pdt-my-report") < out.index("Plan")
    assert out.index("Plan") < out.index("Updating Windows scheduled task pdt-my-report")

@pytest.fixture
def powershell_deploy(project, monkeypatch):
    from pdt import powershell
    folder = project / "ps-report"
    folder.mkdir()
    (folder / "config.yml").write_text("schedule: hourly\ntimezone: local\n")
    (folder / "report.ps1").write_text("")
    needs = []
    host = []
    checked = []
    scripts = []
    runs = []
    monkeypatch.setattr(deploy_windows, "ensure_pwsh", lambda: r"C:\ProgramData\pdt\pwsh\pwsh.exe")
    monkeypatch.setattr(powershell, "scan", lambda app, provider: powershell.ScriptScan(
        ["report.ps1"], [], list(needs), [], list(host))
        if provider == "windows" else pytest.fail(provider))

    def has_module(command, **kwargs):
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        checked.append((command[0], script))
        missing = "ActiveDirectory\n" if "'ActiveDirectory'" in script else ""
        return deploy_windows.subprocess.CompletedProcess(command, 0, missing, "")

    monkeypatch.setattr(deploy_windows.subprocess, "run", has_module)
    monkeypatch.setattr(deploy_windows, "gather_secrets", lambda app: {})
    monkeypatch.setattr(deploy_windows.regions, "local_currency", lambda: "USD")
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: runs.append(powershell) or scripts.append(script) or True)
    return config.merged_app("ps-report"), needs, host, checked, scripts, runs


def test_powershell_deploy_installs_the_gallery_modules_in_the_elevated_step(
        powershell_deploy, monkeypatch):
    from pdt import powershell
    app, needs, host, checked, scripts, _runs = powershell_deploy
    needs.append(powershell.ModuleNeed("ImportExcel", None, "#Requires in report.ps1"))
    host.append("ScheduledTasks")
    actions = []
    monkeypatch.setattr(deploy_windows, "confirm",
                        lambda lines, assume_yes, cost=None: actions.extend(lines) or True)

    assert deploy_windows.deploy(app, assume_yes=True) == 0

    assert plain(actions[-2:]) == [
        r"run the app's .ps1 files with PowerShell from C:\ProgramData\pdt\pwsh\pwsh.exe",
        "install PowerShell modules ImportExcel (all users)"]
    assert checked == [(r"C:\ProgramData\pdt\pwsh\pwsh.exe",
                        "$have = Get-Module -ListAvailable -Name 'ScheduledTasks' | "
                        "Select-Object -ExpandProperty Name; "
                        "'ScheduledTasks' | Where-Object { $have -notcontains $_ }")]
    (script,) = scripts
    install = powershell.install_command([needs[0]])
    encoded = base64.b64encode(install.encode("utf-16-le")).decode("ascii")
    assert (r"& 'C:\ProgramData\pdt\pwsh\pwsh.exe' -NoProfile -NonInteractive "
            f"-EncodedCommand {encoded}; if ($LASTEXITCODE -ne 0) {{ throw") in script
    assert script.index("icacls") < script.index("-EncodedCommand") < script.index(
        "Register-ScheduledTask")


def test_powershell_deploy_without_modules_installs_nothing(powershell_deploy):
    app, _needs, _host, checked, scripts, _runs = powershell_deploy
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    (script,) = scripts
    assert "EncodedCommand" not in script
    assert checked == []


def test_powershell_deploy_runs_the_elevated_script_through_windows_powershell(powershell_deploy):
    from pdt import powershell
    app, needs, _host, _checked, _scripts, runs = powershell_deploy
    needs.append(powershell.ModuleNeed("ImportExcel", None, "#Requires in report.ps1"))
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    assert runs == ["powershell.exe"]


def test_powershell_deploy_names_the_windows_feature_of_a_missing_module(
        powershell_deploy, capsys):
    app, _needs, host, _checked, scripts, _runs = powershell_deploy
    host.append("ActiveDirectory")
    assert deploy_windows.deploy(app, assume_yes=True) == 1
    assert scripts == []
    out = " ".join(capsys.readouterr().out.split())
    assert "the PowerShell module ActiveDirectory, which this PC does not have" in out
    assert "RSAT: Active Directory Domain Services and Lightweight Directory Services Tools" in out


def test_plan_says_the_windows_provider_ignores_a_dockerfile(project):
    app = windows_app(project)
    (Path(app["dir"]) / "Dockerfile").write_text("FROM python:3.12-slim\n")
    actions = deploy_windows.plan(app, "create", "hourly at minute 00", r"PC\jon", {}, False)
    assert (f"ignore {Path(app['dir']) / 'Dockerfile'} (not used by the windows provider)"
            in plain(actions))


@pytest.mark.parametrize("schedule, description", [
    ("*/15 * * * *", "every 15 minutes"),
    ("hourly", "hourly at minute 00"),
    ("30 6 * * *", "daily at 06:30"),
    ("0 9 * * 1-5", "weekly on Monday, Tuesday, Wednesday, Thursday, Friday at 09:00"),
    ("0 8 1,15 * *", "every month on day 1, 15 at 08:00"),
    ("yearly", "January on day 1 at 00:00"),
])
def test_validate_and_deploy_accept_the_same_schedules(project, schedule, description):
    app = windows_app(project, f"schedule: '{schedule}'\n")
    assert config.validate_app("my-report") == []
    assert deploy_windows.task_xml(app)[0] == description


@pytest.mark.parametrize("config_text, problem", [
    ("schedule: '*/7 * * * *'\n", "schedule: cron minute interval '*/7' resets each hour"),
    ("schedule: '0,30 * * * *'\n", "schedule: Windows Task Scheduler requires one numeric "
                                   "minute; got '0,30'"),
    ("schedule: '0 */2 * * *'\n", "schedule: Windows Task Scheduler requires one numeric "
                                  "hour; got '*/2'"),
    ("schedule: '0 9 * * */2'\n", "schedule: Windows Task Scheduler cannot represent "
                                  "day-of-week field '*/2'"),
    ("schedule: '0 9 1 * 1'\n", "schedule: cron '0 9 1 * 1' cannot be represented"),
    ("schedule: daily\ntimezone: America/New_York\n",
     "timezone: the windows provider runs on this PC's local time"),
])
def test_validate_reports_what_the_windows_deploy_rejects(project, config_text, problem):
    app = windows_app(project, config_text)
    problems = config.validate_app("my-report")
    assert len(problems) == 1 and problems[0].startswith(f"my-report: {problem}")
    with pytest.raises(deploy_windows.WindowsDeployError) as rejected:
        deploy_windows.task_xml(app)
    assert problems[0] == f"my-report: {problem.split(': ', 1)[0]}: {rejected.value}"


@pytest.mark.parametrize("host", ["false", "5"])
def test_a_host_that_is_not_text_is_rejected_and_never_runs_locally(project, monkeypatch, capsys,
                                                                    host):
    windows_app(project)
    (project / "my-report" / "config.yml").write_text(
        (project / "my-report" / "config.yml").read_text() + f"platform:\n  host: {host}\n")
    assert any("platform.host" in problem for problem in config.validate_app("my-report"))
    monkeypatch.setattr(deploy_windows, "deploy", lambda app, yes: pytest.fail("deployed locally"))
    monkeypatch.setattr(deploy_windows.sys, "argv", ["deploy_windows.py", "deploy", "my-report"])

    assert deploy_windows.main() == 1
    assert "platform.host" in capsys.readouterr().out
