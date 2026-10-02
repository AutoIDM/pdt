import argparse
from pathlib import Path

import pytest

from conftest import add_app
from pdt import cli, config, console, deploy


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


def test_health_of_one_app_relays_a_provider_failure(project, monkeypatch, capsys):
    add_app(project, "hello-world", "schedule: daily\n")
    monkeypatch.setattr(deploy, "dispatch_output",
                        lambda *a: (1, "error: not signed in to Azure\n"))
    assert run_cli(monkeypatch, "health", "hello-world") == 1
    out = capsys.readouterr().out
    assert "error: not signed in to Azure" in out
    assert "unknown" in out


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
