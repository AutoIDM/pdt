import argparse
from pathlib import Path

import pytest

from conftest import add_app
from pdt import cli, deploy


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


APP_COMMANDS = ["run", "deploy", "login", "destroy"]


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
    assert f"Which app do you want to {command}?" in out
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
    assert calls == [(("azure", "storage", "hello-world", False, None,
                        ["ls", "state/"]), {})]


def test_cloud_cli_passthroughs_are_registered():
    for name, script in cli.CLOUD_CLIS.items():
        path = Path(cli.__file__).with_name(script)
        assert path.exists(), f"pdt {name} points at a missing script"
        assert f'sys.argv[1] == "{name}"' in path.read_text(), (
            f"{script} has no `{name}` passthrough branch")
        assert name in cli.CLOUD_CLIS
