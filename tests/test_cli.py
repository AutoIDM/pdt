import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import add_app
from pdt import cli, config, console, deploy, powershell


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


APP_COMMANDS = ["run", "deploy", "login", "destroy", "secrets", "storage", "runs", "logs",
                "pause", "unpause"]


def test_no_command_prints_help(monkeypatch, capsys):
    assert run_cli(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "Usage: pdt" in out
    assert "deploy" in out


@pytest.mark.parametrize("command", APP_COMMANDS)
def test_command_without_app_lists_apps(project, monkeypatch, capsys, command):
    add_app(project, "hello-world")
    add_app(project, "daily-report")
    assert run_cli(monkeypatch, command) == 1
    out = capsys.readouterr().out
    assert cli.APP_QUESTIONS[command] in out
    assert "hello-world" in out
    assert "daily-report" in out
    assert "pdt list" in out
    assert f"pdt {command} <app>" in out


@pytest.mark.parametrize("command", APP_COMMANDS)
def test_command_with_unknown_app_lists_apps(project, monkeypatch, capsys, command):
    add_app(project, "hello-world")
    add_app(project, "daily-report")
    assert run_cli(monkeypatch, command, "hello-wrld") == 1
    out = capsys.readouterr().out
    assert "error: no app named 'hello-wrld'" in out
    assert "hello-world" in out
    assert "daily-report" in out
    assert f"pdt {command} <app>" in out


@pytest.mark.parametrize("command", APP_COMMANDS)
def test_command_without_app_caps_the_list_at_five(project, monkeypatch, capsys, command):
    for i in range(7):
        add_app(project, f"app-{i}")
    assert run_cli(monkeypatch, command) == 1
    out = capsys.readouterr().out
    assert "app-4" in out
    assert "app-5" not in out
    assert "... and 2 more" in out
    assert "pdt list" in out


@pytest.mark.parametrize("command", APP_COMMANDS)
def test_command_without_app_in_empty_project(project, monkeypatch, capsys, command):
    assert run_cli(monkeypatch, command) == 1
    out = capsys.readouterr().out
    assert "no apps yet" in out
    assert "pdt new" in out


def record_app(monkeypatch, command):
    picked = []
    if command == "run":
        monkeypatch.setattr(cli.subprocess, "run", lambda argv, cwd: picked.append(cwd.name)
                            or cli.subprocess.CompletedProcess(argv, 0))
    else:
        target = "pause" if command == "unpause" else command
        monkeypatch.setattr(deploy, target, lambda app, *a, **k: picked.append(app) or 0)
    return picked


@pytest.mark.parametrize("command", APP_COMMANDS)
def test_command_inside_an_app_folder_uses_that_app(project, monkeypatch, capsys, command):
    add_app(project, "hello-world")
    nested = add_app(project, "daily-report") / "data"
    nested.mkdir()
    monkeypatch.chdir(nested)
    picked = record_app(monkeypatch, command)
    assert run_cli(monkeypatch, command) == 0
    assert picked == ["daily-report"]
    assert "Using app daily-report (current folder)." in capsys.readouterr().out


@pytest.mark.parametrize("command", APP_COMMANDS)
def test_a_named_app_wins_over_the_app_folder(project, monkeypatch, capsys, command):
    add_app(project, "hello-world")
    monkeypatch.chdir(add_app(project, "daily-report"))
    picked = record_app(monkeypatch, command)
    assert run_cli(monkeypatch, command, "hello-world") == 0
    assert picked == ["hello-world"]
    assert "current folder" not in capsys.readouterr().out


@pytest.mark.parametrize("command", APP_COMMANDS)
def test_a_mistyped_app_inside_an_app_folder_is_an_error(project, monkeypatch, capsys, command):
    add_app(project, "hello-world")
    monkeypatch.chdir(add_app(project, "daily-report"))
    picked = record_app(monkeypatch, command)
    assert run_cli(monkeypatch, command, "hello-wrld") == 1
    assert picked == []
    out = capsys.readouterr().out
    assert "error: no app named 'hello-wrld'" in out
    assert "current folder" not in out


def test_a_disabled_app_folder_picks_no_app(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    monkeypatch.chdir(add_app(project, "not-ready", "enabled: false\n"))
    picked = record_app(monkeypatch, "deploy")
    assert run_cli(monkeypatch, "deploy") == 1
    assert picked == []
    assert cli.APP_QUESTIONS["deploy"] in capsys.readouterr().out


def test_pdt_project_with_the_working_folder_outside_it_lists_apps(project, tmp_path,
                                                                    monkeypatch, capsys):
    add_app(project, "hello-world")
    outside = tmp_path.parent / "outside-the-project"
    outside.mkdir()
    monkeypatch.setenv("PDT_PROJECT", str(project))
    monkeypatch.chdir(outside)
    assert run_cli(monkeypatch, "deploy") == 1
    assert cli.APP_QUESTIONS["deploy"] in capsys.readouterr().out


@pytest.mark.parametrize("command", ["runs", "logs"])
def test_json_output_inside_an_app_folder_has_no_extra_line(project, monkeypatch, capsys,
                                                            command):
    monkeypatch.chdir(add_app(project, "daily-report"))
    picked = record_app(monkeypatch, command)
    assert run_cli(monkeypatch, command, "--json") == 0
    assert picked == ["daily-report"]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("argv, sent", [
    (["secrets", "save"], ("secrets", "daily-report", False, ["save"])),
    (["secrets", "set", "API_KEY"], ("secrets", "daily-report", False, ["set", "API_KEY"])),
    (["logs", "3"], ("logs", "daily-report", False, ["--", "3"])),
    (["storage", "ls", "state/"], ("storage", "daily-report", False, ["--", "ls", "state/"])),
])
def test_the_next_argument_moves_past_the_app_inside_an_app_folder(project, monkeypatch,
                                                                    argv, sent):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.chdir(add_app(project, "daily-report", "schedule: daily\n"))
    monkeypatch.setattr(config, "check_env", lambda env: [])
    calls = []
    monkeypatch.setattr(deploy, "dispatch", lambda provider, *a: calls.append(a) or 0)
    assert run_cli(monkeypatch, *argv) == 0
    assert calls == [sent]


def test_a_run_number_at_the_project_root_is_not_an_app(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.setattr(deploy, "dispatch", lambda *a: pytest.fail("dispatched"))
    assert run_cli(monkeypatch, "logs", "3") == 1
    assert "no app named '3'" in capsys.readouterr().out


def test_an_unknown_secrets_action_is_an_error(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.setattr(deploy, "dispatch", lambda *a: pytest.fail("dispatched"))
    assert run_cli(monkeypatch, "secrets", "hello-world", "sav") == 1
    assert "no secrets action named 'sav'; choose diff, save, get, set" in (
        capsys.readouterr().out)


def test_every_app_command_goes_through_choose_app():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for command in APP_COMMANDS:
        app_arg = next(a for a in sub.choices[command]._actions if a.dest == "app")
        assert app_arg.nargs == "?", f"pdt {command} must accept a missing app name"


def test_storage_dispatches_with_the_extra_args(project, monkeypatch):
    add_app(project, "hello-world", "schedule: daily\n")
    calls = []
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: calls.append((a, k)) or 0)
    assert run_cli(monkeypatch, "storage", "hello-world", "ls", "state/") == 0
    assert calls == [(("azure", "storage", "hello-world", False, ["--", "ls", "state/"]), {})]


def test_run_starts_a_powershell_app_through_the_wrapper(project, monkeypatch):
    folder = project / "ps-report"
    folder.mkdir()
    monkeypatch.setattr(powershell, "extract", lambda folder: {"files": []})
    (folder / "report.ps1").write_text("")
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kwargs: calls.append(
        (command, kwargs)) or subprocess.CompletedProcess(command, 0))
    assert run_cli(monkeypatch, "run", "ps-report") == 0
    (command, kwargs), = calls
    assert command == ["uv", "run", "--project", str(project), "--script",
                       str(Path(cli.__file__).with_name("run_powershell.py")), str(folder)]
    assert kwargs["cwd"] == folder
    assert kwargs["env"]["PDT_PROJECT"] == str(project)


def test_run_starts_the_run_py_that_replaces_the_wrapper(project, monkeypatch):
    folder = project / "ps-report"
    folder.mkdir()
    monkeypatch.setattr(powershell, "extract", lambda folder: {"files": []})
    for name in ("report.ps1", "requirements.psd1", "run.py"):
        (folder / name).write_text("")
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kwargs: calls.append(
        (command, kwargs)) or subprocess.CompletedProcess(command, 0))
    assert run_cli(monkeypatch, "run", "ps-report") == 0
    assert calls == [(["uv", "run", "--script", "run.py"], {"cwd": folder})]


def test_new_takes_from_scripts_but_not_together_with_from(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["new", "ad-report", "--from-scripts"])
    assert args.from_scripts and args.source is None
    with pytest.raises(SystemExit):
        parser.parse_args(["new", "ad-report", "--from-scripts", "--from", "hello-world"])
    assert "not allowed with argument" in capsys.readouterr().err


def test_storage_with_an_unknown_app_lists_the_apps(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    assert run_cli(monkeypatch, "storage", "../hello-world", "ls") == 1
    out = capsys.readouterr().out
    assert "no app named '../hello-world'" in out
    assert "hello-world" in out
    assert "pdt storage <app>" in out


def test_runs_and_logs_forward_their_flags_after_a_separator(project, monkeypatch):
    add_app(project, "hello-world", "schedule: daily\n")
    calls = []
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: calls.append(a) or 0)
    assert run_cli(monkeypatch, "runs", "hello-world", "--json") == 0
    assert run_cli(monkeypatch, "logs", "hello-world") == 0
    assert run_cli(monkeypatch, "logs", "hello-world", "3", "--failed", "--errors") == 0
    assert run_cli(monkeypatch, "runs", "hello-world", "--since", "3d") == 0
    assert run_cli(monkeypatch, "logs", "hello-world", "2", "--since", "2026-09-20") == 0
    assert run_cli(monkeypatch, "runs", "hello-world", "--since", "3d", "--span", "1d",
                   "--count", "5") == 0
    assert run_cli(monkeypatch, "logs", "hello-world", "--count", "5", "--since", "3d",
                   "--span", "1d") == 0
    assert run_cli(monkeypatch, "logs", "hello-world", "--lines", "50", "--head") == 0
    assert run_cli(monkeypatch, "logs", "hello-world", "--follow") == 0
    assert calls == [
        ("azure", "runs", "hello-world", False, ["--", "--json"]),
        ("azure", "logs", "hello-world", False, ["--", "1"]),
        ("azure", "logs", "hello-world", False, ["--", "3", "--failed", "--errors"]),
        ("azure", "runs", "hello-world", False, ["--", "--since", "3d"]),
        ("azure", "logs", "hello-world", False, ["--", "2", "--since", "2026-09-20"]),
        ("azure", "runs", "hello-world", False,
         ["--", "--since", "3d", "--span", "1d", "--count", "5"]),
        ("azure", "logs", "hello-world", False,
         ["--", "1", "--since", "3d", "--span", "1d", "--count", "5"]),
        ("azure", "logs", "hello-world", False, ["--", "1", "--lines", "50", "--head"]),
        ("azure", "logs", "hello-world", False, ["--", "1", "--follow"]),
    ]


def test_health_checks_every_enabled_app(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\n")
    add_app(project, "not-ready", "enabled: false\n")
    add_app(project, "daily-report", "schedule: daily\n")
    calls = []

    def fake_output(provider, command, app_name, extra):
        calls.append((provider, command, app_name, extra))
        if app_name == "daily-report":
            return 0, 'status line\n[{"id": "e1", "started": "2026-09-23T10:00:00+00:00", ' \
                      '"ended": "2026-09-23T10:00:12+00:00", "status": "failed", "exit_code": 1, ' \
                      '"number": 1}]\n'
        return 0, "[]\n"

    monkeypatch.setattr(deploy, "dispatch_output", fake_output)
    assert run_cli(monkeypatch, "health") == 1
    assert calls == [("azure", "runs", name, ["--", "--json"])
                     for name in ("daily-report", "hello-world")]
    out = capsys.readouterr().out
    assert "not-ready" not in out
    assert "not yet run" in out
    assert "0 of 1 succeeded" in out


def fake_health(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy, "health", lambda names, as_json: calls.append(names) or 0)
    return calls


def test_health_inside_an_app_folder_checks_only_that_app(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    monkeypatch.chdir(add_app(project, "daily-report"))
    calls = fake_health(monkeypatch)
    assert run_cli(monkeypatch, "health") == 0
    assert run_cli(monkeypatch, "health", "--all") == 0
    assert calls == [["daily-report"], ["daily-report", "hello-world"]]
    out = capsys.readouterr().out
    assert "Using app daily-report (current folder)." in out
    assert "pdt health --all" in out


def test_health_at_the_project_root_checks_every_app(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    add_app(project, "daily-report")
    calls = fake_health(monkeypatch)
    assert run_cli(monkeypatch, "health") == 0
    assert calls == [["daily-report", "hello-world"]]
    assert "current folder" not in capsys.readouterr().out


def test_health_json_inside_an_app_folder_has_no_extra_line(project, monkeypatch, capsys):
    monkeypatch.chdir(add_app(project, "daily-report"))
    calls = fake_health(monkeypatch)
    assert run_cli(monkeypatch, "health", "--json") == 0
    assert calls == [["daily-report"]]
    assert capsys.readouterr().out == ""


def test_health_with_an_app_and_all_is_refused(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    calls = fake_health(monkeypatch)
    assert run_cli(monkeypatch, "health", "hello-world", "--all") == 1
    assert calls == []
    assert "pick an app or --all, not both" in capsys.readouterr().out


def fake_deploys(monkeypatch, codes):
    calls = []

    def fake_deploy(name, assume_yes=False):
        calls.append((name, assume_yes))
        return codes.get(name, 0)

    monkeypatch.setattr(deploy, "deploy", fake_deploy)
    return calls


def test_deploy_all_deploys_every_enabled_app_in_order(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    add_app(project, "not-ready", "enabled: false\n")
    add_app(project, "daily-report")
    calls = fake_deploys(monkeypatch, {})
    assert run_cli(monkeypatch, "deploy", "--all", "--yes") == 0
    assert calls == [("daily-report", True), ("hello-world", True)]
    out = capsys.readouterr().out
    assert "Deploying daily-report (1 of 2)" in out
    assert "Deploying hello-world (2 of 2)" in out
    assert "not-ready" not in out


def answer(monkeypatch, *answers):
    questions = []
    queue = list(answers)

    def fake_confirm(question="Proceed?"):
        questions.append(question)
        return queue.pop(0)

    monkeypatch.setattr(cli, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(console, "confirm", fake_confirm)
    return questions


def four_apps(project):
    for name in ("alpha", "bravo", "charlie", "delta"):
        add_app(project, name)


STOP_HINT = ("Fix the problem above and run pdt deploy --all again, "
             "or add --skip-failures to go on past it.")


def test_deploy_all_stops_when_the_person_will_not_skip(project, monkeypatch, capsys):
    four_apps(project)
    calls = fake_deploys(monkeypatch, {"bravo": 3})
    questions = answer(monkeypatch, False)
    assert run_cli(monkeypatch, "deploy", "--all") == 3
    assert calls == [("alpha", False), ("bravo", False)]
    assert questions == ["Skip the failing app bravo and deploy the rest?"]
    out = capsys.readouterr().out
    assert "bravo did not deploy." in out
    assert STOP_HINT in out


def test_deploy_all_with_yes_and_no_terminal_stops_at_the_failure(project, monkeypatch, capsys):
    four_apps(project)
    calls = fake_deploys(monkeypatch, {"bravo": 3})
    monkeypatch.setattr(cli, "can_prompt", lambda interactive: False)
    assert run_cli(monkeypatch, "deploy", "--all", "--yes") == 3
    assert calls == [("alpha", True), ("bravo", True)]
    assert config.is_enabled("bravo")
    assert STOP_HINT in capsys.readouterr().out


def test_deploy_all_with_skip_failures_runs_unattended(project, monkeypatch, capsys):
    four_apps(project)
    calls = fake_deploys(monkeypatch, {"bravo": 3})
    questions = answer(monkeypatch)
    assert run_cli(monkeypatch, "deploy", "--all", "--yes", "--skip-failures") == 1
    assert [name for name, _ in calls] == ["alpha", "bravo", "charlie", "delta"]
    assert questions == []
    assert config.is_enabled("bravo")
    out = capsys.readouterr().out
    assert "bravo did not deploy." in out
    assert "Not deployed: bravo" in out


def test_skip_failures_without_all_is_refused(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    calls = fake_deploys(monkeypatch, {})
    assert run_cli(monkeypatch, "deploy", "hello-world", "--skip-failures") == 1
    assert calls == []
    assert "--skip-failures only works with --all" in capsys.readouterr().out


def test_deploy_all_with_yes_never_disables_an_app(project, monkeypatch, capsys):
    four_apps(project)
    calls = fake_deploys(monkeypatch, {"bravo": 3})
    questions = answer(monkeypatch, True)
    assert run_cli(monkeypatch, "deploy", "--all", "--yes") == 1
    assert [name for name, _ in calls] == ["alpha", "bravo", "charlie", "delta"]
    assert questions == ["Skip the failing app bravo and deploy the rest?"]
    assert config.is_enabled("bravo")
    assert "Not deployed: bravo" in capsys.readouterr().out


def test_deploy_all_skips_without_disabling_the_app(project, monkeypatch, capsys):
    four_apps(project)
    calls = fake_deploys(monkeypatch, {"bravo": 3})
    questions = answer(monkeypatch, True, False)
    assert run_cli(monkeypatch, "deploy", "--all") == 1
    assert [name for name, _ in calls] == ["alpha", "bravo", "charlie", "delta"]
    assert questions == ["Skip the failing app bravo and deploy the rest?",
                         "Disable the failing app bravo?"]
    assert config.is_enabled("bravo")
    assert not (project / "bravo" / "config.yml").exists()
    assert "Not deployed: bravo" in capsys.readouterr().out


def test_deploy_all_disables_a_skipped_app(project, monkeypatch, capsys):
    four_apps(project)
    (project / "bravo" / "config.yml").write_text("# my notes\nschedule: daily\n")
    fake_deploys(monkeypatch, {"bravo": 3})
    questions = answer(monkeypatch, True, True)
    assert run_cli(monkeypatch, "deploy", "--all") == 1
    assert "Skip the failing app bravo" in questions[0]
    assert "Disable the failing app bravo" in questions[1]
    assert not config.is_enabled("bravo")
    text = (project / "bravo" / "config.yml").read_text()
    assert "# my notes" in text
    assert "schedule: daily" in text
    out = capsys.readouterr().out
    assert f"Disabled bravo in {Path('bravo', 'config.yml')}." in out
    assert "Not deployed: bravo" in out


def test_deploy_all_with_no_terminal_stops_at_the_failure(project, monkeypatch, capsys):
    four_apps(project)
    calls = fake_deploys(monkeypatch, {"bravo": 3})
    questions = answer(monkeypatch)
    monkeypatch.setattr(cli, "can_prompt", lambda interactive: False)
    assert run_cli(monkeypatch, "deploy", "--all") == 3
    assert calls == [("alpha", False), ("bravo", False)]
    assert questions == []
    assert STOP_HINT in capsys.readouterr().out


def test_deploy_all_with_an_app_name_is_refused(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    calls = fake_deploys(monkeypatch, {})
    assert run_cli(monkeypatch, "deploy", "hello-world", "--all") == 1
    assert calls == []
    assert "pick an app or --all, not both" in capsys.readouterr().out


def test_health_of_one_app_shows_a_provider_failure_as_unknown(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.setattr(deploy, "dispatch_output", lambda *a: (1, ""))
    assert run_cli(monkeypatch, "health", "hello-world") == 1
    assert "unknown" in capsys.readouterr().out


JSON_COMMANDS = [name for name, command in cli.build_parser().commands.items()
                 if "--json" in command._option_string_actions]


def no_apps(project, monkeypatch):
    pass


def no_project(project, monkeypatch):
    outside = project.parent / f"{project.name}-outside"
    outside.mkdir()
    monkeypatch.chdir(outside)


def provider_fails(project, monkeypatch):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: console.error("not signed in") or 1)
    monkeypatch.setattr(deploy, "dispatch_output", lambda *a: (1, ""))


def unknown_provider(project, monkeypatch):
    add_app(project, "hello-world", "platform:\n  provider: nowhere\n")


@pytest.mark.parametrize("case", [no_apps, no_project, provider_fails, unknown_provider])
@pytest.mark.parametrize("command", JSON_COMMANDS)
def test_json_stdout_holds_only_json(project, monkeypatch, capsys, command, case):
    case(project, monkeypatch)
    argv = [command, "--json"] if case is no_apps else [command, "hello-world", "--json"]
    code = run_cli(monkeypatch, *argv)
    out = capsys.readouterr().out
    if out == "":
        assert code != 0
    else:
        json.loads(out)


def test_json_commands_are_found():
    assert {"health", "runs", "logs"} <= set(JSON_COMMANDS)


def test_health_json_without_apps_prints_an_empty_list(project, monkeypatch, capsys):
    assert run_cli(monkeypatch, "health", "--json") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == []
    assert "This project has no apps yet." in captured.err


def test_a_provider_script_for_json_prints_only_the_data_on_stdout(project):
    src = Path(deploy.__file__).resolve().parent.parent
    code = (f"import subprocess, sys; sys.path.insert(0, {str(src)!r}); "
            "from pdt import console; console.say('Signing in...'); "
            "subprocess.run([sys.executable, '-c', 'print(1)']); console.data('[]')")
    proc = subprocess.run([sys.executable, "-c", code], env=deploy.provider_env(["--", "--json"]),
                          check=True, capture_output=True, text=True)
    assert proc.stdout == "[]\n"
    assert proc.stderr == "Signing in...\n1\n"


def test_cloud_cli_passthroughs_are_registered():
    for name, script in cli.CLOUD_CLIS.items():
        path = Path(cli.__file__).with_name(script)
        assert path.exists(), f"pdt {name} points at a missing script"
        assert f'sys.argv[1] == "{name}"' in path.read_text(), (
            f"{script} has no `{name}` passthrough branch")
        assert name in cli.CLOUD_CLIS


def test_pause_writes_the_key_and_tells_a_deployed_app(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\n")
    calls = []
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: calls.append(a) or 0)
    assert run_cli(monkeypatch, "pause", "hello-world") == 0
    assert (project / "hello-world" / "config.yml").read_text() == "schedule: daily\npause: true\n"
    assert calls == []
    assert "not deployed" in capsys.readouterr().out
    config.mark_deployed("hello-world", True)
    assert run_cli(monkeypatch, "unpause", "hello-world") == 0
    assert (project / "hello-world" / "config.yml").read_text() == "schedule: daily\npause: false\n"
    assert calls == [("azure", "unpause", "hello-world", False)]


def test_run_deployed_starts_the_deployed_job(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\n")
    calls = []
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: calls.append(a) or 0)
    assert run_cli(monkeypatch, "run", "hello-world", "--deployed") == 1
    assert "not deployed" in capsys.readouterr().out
    config.mark_deployed("hello-world", True)
    assert run_cli(monkeypatch, "run", "hello-world", "--deployed") == 0
    assert calls == [("azure", "start", "hello-world", False)]


def test_list_shows_whether_an_app_is_paused(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\npause: true\n")
    add_app(project, "daily-report", "schedule: daily\n")
    assert run_cli(monkeypatch, "list") == 0
    lines = capsys.readouterr().out.splitlines()
    assert "paused" in lines[0]
    assert next(line for line in lines if "hello-world" in line).split()[-1] == "true"
    assert next(line for line in lines if "daily-report" in line).split()[-1] == "false"


def test_list_shows_a_disabled_app_and_names_leaves_it_out(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    add_app(project, "not-ready")
    (project / "not-ready" / "config.yml").write_text("enabled: false\n")
    assert run_cli(monkeypatch, "list") == 0
    assert "not-ready" in capsys.readouterr().out
    assert run_cli(monkeypatch, "list", "--names") == 0
    assert capsys.readouterr().out.split() == ["hello-world"]
    assert run_cli(monkeypatch, "run", "not-ready") == 1
    assert "no app named 'not-ready'" in capsys.readouterr().out


def test_every_command_is_in_exactly_one_help_group():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    grouped = [name for names in cli.COMMAND_GROUPS.values() for name in names]
    assert sorted(grouped) == sorted(sub.choices)
    assert len(grouped) == len(set(grouped))


@pytest.mark.parametrize("argv, hint", [
    (["lgos"], "Did you mean `pdt logs`?"),
    (["deploy", "--yse"], "Did you mean `--yes`?"),
    (["logs", "--folow"], "Did you mean `--follow`?"),
    (["runs", "--sinse", "3d"], "Did you mean `--since`?"),
    (["completion", "zhs"], "Did you mean `pdt completion zsh`?"),
])
def test_a_mistyped_command_or_option_names_the_closest_one(monkeypatch, capsys, argv, hint):
    with pytest.raises(SystemExit) as stop:
        run_cli(monkeypatch, *argv)
    assert stop.value.code == 2
    assert capsys.readouterr().err.rstrip().endswith(hint)


def test_a_word_close_to_nothing_gets_no_hint(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "xyzzy")
    assert "Did you mean" not in capsys.readouterr().err


def test_a_subcommand_error_shows_a_usage_line_that_runs(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "logs", "--lines", "x")
    err = capsys.readouterr().err
    assert "pdt logs: error:" in err
    assert "<command> ..." not in err


def test_a_mistyped_app_names_the_closest_app(project, monkeypatch, capsys):
    add_app(project, "list-empty-security-groups", "schedule: daily\n")
    assert run_cli(monkeypatch, "logs", "list-empty-securty-groups") == 1
    assert ("no app named 'list-empty-securty-groups'. Did you mean "
            "`pdt logs list-empty-security-groups`?") in capsys.readouterr().out


def test_a_mistyped_secrets_action_names_the_closest_action(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\n")
    assert run_cli(monkeypatch, "secrets", "hello-world", "sav") == 1
    assert "Did you mean `pdt secrets hello-world save`?" in capsys.readouterr().out


def test_secrets_takes_the_action_before_the_app(project, monkeypatch):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.setattr(config, "check_env", lambda env: [])
    calls = []
    monkeypatch.setattr(deploy, "dispatch", lambda provider, *a: calls.append(a) or 0)
    assert run_cli(monkeypatch, "secrets", "save", "hello-world") == 0
    assert calls == [("secrets", "hello-world", False, ["save"])]


def test_validate_says_it_scans_a_powershell_app(monkeypatch, capsys):
    def scan(app, provider):
        raise powershell.PowerShellError("pwsh failed")

    monkeypatch.setattr(powershell, "scan", scan)
    assert cli.powershell_problems("report", {"platform": {}}) == ["report: pwsh failed"]
    assert "Scanning the PowerShell scripts in report..." in capsys.readouterr().out
