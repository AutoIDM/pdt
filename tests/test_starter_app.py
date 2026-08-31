import shutil
from types import SimpleNamespace

import yaml

from pdt import __version__, cli, config, scaffold
from pdt.config import find_apps, validate_app


def init_into(folder, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(folder)
    assert scaffold.init(None, assume_yes=True) == 0
    return folder


def test_an_empty_folder_gets_a_working_starter_app(tmp_path, monkeypatch):
    root = init_into(tmp_path, monkeypatch)
    starter = root / scaffold.STARTER
    assert starter.is_dir()
    assert (starter / "run.py").is_file()
    assert find_apps() == [scaffold.STARTER]
    assert [p for p in validate_app(scaffold.STARTER)
            if "platform.provider" not in p] == []


def test_the_starter_pins_the_installed_version(tmp_path, monkeypatch):
    root = init_into(tmp_path, monkeypatch)
    header = (root / scaffold.STARTER / "run.py").read_text()
    assert f"pdt-cli=={__version__}" in header
    assert "PDT_VERSION" not in header


def test_a_folder_that_already_holds_files_gets_no_starter(tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("mine\n")
    root = init_into(tmp_path, monkeypatch)
    assert not (root / scaffold.STARTER).exists()
    assert find_apps() == []


def test_a_folder_holding_only_hidden_entries_still_counts_as_empty(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    root = init_into(tmp_path, monkeypatch)
    assert (root / scaffold.STARTER).is_dir()


def test_a_named_new_folder_gets_the_starter(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init("reports", assume_yes=True) == 0
    assert (tmp_path / "reports" / scaffold.STARTER / "run.py").is_file()


def test_the_starter_needs_no_env_vars(tmp_path, monkeypatch):
    root = init_into(tmp_path, monkeypatch)
    config = yaml.safe_load((root / scaffold.STARTER / "config.yml").read_text())
    assert config["env"]["required"] == []
    assert not (root / scaffold.STARTER / "env.template").exists()


def test_the_starter_is_also_offered_as_an_example():
    assert scaffold.STARTER in [example.name for example in scaffold.examples()]


def run_app(monkeypatch, name, returncode=0):
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=returncode))
    return cli.cmd_run(SimpleNamespace(app=name))


def test_running_the_starter_suggests_deploying_it(tmp_path, monkeypatch, capsys):
    init_into(tmp_path, monkeypatch)
    assert run_app(monkeypatch, scaffold.STARTER) == 0
    assert f"pdt deploy {scaffold.STARTER}" in capsys.readouterr().out


def test_other_apps_get_no_deploy_nudge(tmp_path, monkeypatch, capsys):
    root = init_into(tmp_path, monkeypatch)
    shutil.copytree(root / scaffold.STARTER, root / "myjob")
    assert run_app(monkeypatch, "myjob") == 0
    assert "pdt deploy" not in capsys.readouterr().out


def test_a_failed_starter_run_gets_no_deploy_nudge(tmp_path, monkeypatch, capsys):
    init_into(tmp_path, monkeypatch)
    assert run_app(monkeypatch, scaffold.STARTER, returncode=1) == 1
    assert "pdt deploy" not in capsys.readouterr().out


def test_an_already_deployed_starter_gets_no_nudge(tmp_path, monkeypatch, capsys):
    init_into(tmp_path, monkeypatch)
    config.mark_deployed(scaffold.STARTER, True)
    assert run_app(monkeypatch, scaffold.STARTER) == 0
    assert "pdt deploy" not in capsys.readouterr().out


def test_a_destroyed_starter_gets_the_nudge_again(tmp_path, monkeypatch, capsys):
    init_into(tmp_path, monkeypatch)
    config.mark_deployed(scaffold.STARTER, True)
    config.mark_deployed(scaffold.STARTER, False)
    assert run_app(monkeypatch, scaffold.STARTER) == 0
    assert f"pdt deploy {scaffold.STARTER}" in capsys.readouterr().out
