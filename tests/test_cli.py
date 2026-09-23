import argparse
from pathlib import Path

import pytest

from conftest import add_app
from pdt import cli, deploy


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


APP_COMMANDS = ["run", "deploy", "login", "destroy", "secrets", "runs", "logs"]


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
    assert calls == [(("azure", "storage", "hello-world", False, ["ls", "state/"]), {})]


def test_runs_and_logs_forward_their_flags_after_a_separator(project, monkeypatch):
    add_app(project, "hello-world", "schedule: daily\n")
    calls = []
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: calls.append(a) or 0)
    assert run_cli(monkeypatch, "runs", "hello-world", "--json") == 0
    assert run_cli(monkeypatch, "logs", "hello-world") == 0
    assert run_cli(monkeypatch, "logs", "hello-world", "3", "--failed", "--errors") == 0
    assert calls == [
        ("azure", "runs", "hello-world", False, ["--", "--json"]),
        ("azure", "logs", "hello-world", False, ["--", "1"]),
        ("azure", "logs", "hello-world", False, ["--", "3", "--failed", "--errors"]),
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
                      '"ended": "2026-09-23T10:00:12+00:00", "status": "failed"}]\n'
        return 0, "[]\n"

    monkeypatch.setattr(deploy, "dispatch_output", fake_output)
    assert run_cli(monkeypatch, "health") == 1
    assert calls == [("azure", "runs", name, ["--", "--json"])
                     for name in ("daily-report", "hello-world")]
    out = capsys.readouterr().out
    assert "not-ready" not in out
    assert "not yet run" in out
    assert "0 of 1 succeeded" in out


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
