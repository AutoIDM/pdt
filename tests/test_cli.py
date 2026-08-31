from pathlib import Path

import pytest

from conftest import add_app
from pdt import cli


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


def test_deploy_without_app_lists_apps(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    add_app(project, "daily-report")
    assert run_cli(monkeypatch, "deploy") == 1
    out = capsys.readouterr().out
    assert "Which app do you want to deploy?" in out
    assert "hello-world" in out
    assert "daily-report" in out
    assert "pdt list" in out
    assert "pdt deploy <app>" in out


def test_deploy_without_app_caps_the_list_at_five(project, monkeypatch, capsys):
    for i in range(7):
        add_app(project, f"app-{i}")
    assert run_cli(monkeypatch, "deploy") == 1
    out = capsys.readouterr().out
    assert "app-4" in out
    assert "app-5" not in out
    assert "... and 2 more" in out
    assert "pdt list" in out


def test_deploy_without_app_in_empty_project(project, monkeypatch, capsys):
    assert run_cli(monkeypatch, "deploy") == 1
    out = capsys.readouterr().out
    assert "no apps yet" in out
    assert "pdt new" in out


def test_cloud_cli_passthroughs_are_registered():
    for name, script in cli.CLOUD_CLIS.items():
        path = Path(cli.__file__).with_name(script)
        assert path.exists(), f"pdt {name} points at a missing script"
        assert f'sys.argv[1] == "{name}"' in path.read_text(), (
            f"{script} has no `{name}` passthrough branch")
        assert f"\n  {name} <args...>" in cli.__doc__, (
            f"pdt {name} is missing from the CLI help")
