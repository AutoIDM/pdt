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
