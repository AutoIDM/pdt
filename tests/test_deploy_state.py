import os

import pytest

from pdt import config, deploy, scaffold


def project_with_starter(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    (tmp_path / "pdt.yml").write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n")
    return tmp_path


def test_a_successful_deploy_records_the_app(tmp_path, monkeypatch):
    project_with_starter(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: 0)
    assert deploy.deploy(scaffold.STARTER) == 0
    assert config.is_deployed(scaffold.STARTER)


def test_a_failed_or_declined_deploy_records_nothing(tmp_path, monkeypatch):
    project_with_starter(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: 1)
    assert deploy.deploy(scaffold.STARTER) == 1
    assert not config.is_deployed(scaffold.STARTER)


def test_destroy_clears_the_record(tmp_path, monkeypatch):
    project_with_starter(tmp_path, monkeypatch)
    config.mark_deployed(scaffold.STARTER, True)
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: 0)
    assert deploy.destroy(scaffold.STARTER) == 0
    assert not config.is_deployed(scaffold.STARTER)


def test_the_state_dir_ignores_itself(tmp_path, monkeypatch):
    project_with_starter(tmp_path, monkeypatch)
    config.mark_deployed(scaffold.STARTER, True)
    assert (tmp_path / ".pdt" / ".gitignore").read_text() == "*\n"


def test_init_gitignores_the_state_dir(tmp_path, monkeypatch):
    project_with_starter(tmp_path, monkeypatch)
    assert ".pdt/" in (tmp_path / ".gitignore").read_text().splitlines()


def test_the_state_file_is_json(tmp_path, monkeypatch):
    import json

    project_with_starter(tmp_path, monkeypatch)
    config.mark_deployed(scaffold.STARTER, True)
    state = json.loads((tmp_path / ".pdt" / "state").read_text())
    assert state["deployed"] == [scaffold.STARTER]


def write_raw_state(project, text):
    (project / ".pdt").mkdir(exist_ok=True)
    (project / ".pdt" / "state").write_text(text)


def test_a_corrupt_state_file_means_not_deployed(tmp_path, monkeypatch, capsys):
    project = project_with_starter(tmp_path, monkeypatch)
    capsys.readouterr()
    write_raw_state(project, '{"hello":')
    assert not config.is_deployed("hello")
    out = capsys.readouterr().out
    assert out.count(".pdt/state is not valid; treating every app as not deployed") == 1


def test_a_state_file_holding_a_list_is_treated_as_corrupt(tmp_path, monkeypatch, capsys):
    project = project_with_starter(tmp_path, monkeypatch)
    capsys.readouterr()
    write_raw_state(project, '["hello"]\n')
    assert not config.is_deployed("hello")
    assert ".pdt/state is not valid" in capsys.readouterr().out


def test_mark_deployed_rewrites_a_corrupt_state_file(tmp_path, monkeypatch):
    import json

    project = project_with_starter(tmp_path, monkeypatch)
    write_raw_state(project, '{"hello":')
    config.mark_deployed(scaffold.STARTER, True)
    state = json.loads((project / ".pdt" / "state").read_text())
    assert state == {"deployed": [scaffold.STARTER]}


def test_a_failed_state_write_keeps_the_old_file(tmp_path, monkeypatch):
    project = project_with_starter(tmp_path, monkeypatch)
    config.mark_deployed(scaffold.STARTER, True)
    before = (project / ".pdt" / "state").read_text()

    def fail_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        config.mark_deployed("another-app", True)
    assert (project / ".pdt" / "state").read_text() == before
    assert sorted(p.name for p in (project / ".pdt").iterdir()) == [".gitignore", "state"]
