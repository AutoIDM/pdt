import os
import subprocess
import sys
from pathlib import Path

import pytest

from pdt import __version__, config, deploy, powershell, scaffold


def project_with_starter(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    (tmp_path / "pdt.yml").write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n")
    return tmp_path


def test_a_failed_deploy_keeps_its_record_until_a_destroy_succeeds(tmp_path, monkeypatch):
    project_with_starter(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: 1)
    assert deploy.deploy(scaffold.STARTER) == 1
    assert config.deployed_platform(scaffold.STARTER) == {"provider": "aws",
                                                          "region": "us-east-1"}
    assert deploy.destroy(scaffold.STARTER) == 1
    assert config.is_deployed(scaffold.STARTER)
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: 0)
    assert deploy.destroy(scaffold.STARTER) == 0
    assert not config.is_deployed(scaffold.STARTER)


def test_concurrent_records_keep_every_app(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    project_with_starter(tmp_path, monkeypatch)
    names = [f"app-{index}" for index in range(40)]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda name: config.mark_deployed(name, {"provider": "aws"}), names))
    assert sorted(config.deployed_places()) == sorted(names)


def test_destroy_ignores_a_record_that_is_not_a_platform(tmp_path, monkeypatch):
    project = project_with_starter(tmp_path, monkeypatch)
    write_raw_state(project, f'{{"deployed": ["{scaffold.STARTER}"], '
                             f'"platforms": {{"{scaffold.STARTER}": true}}}}')
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
    platform = config.merged_app(scaffold.STARTER)["platform"]
    config.mark_deployed(scaffold.STARTER, platform)
    state = json.loads((project / ".pdt" / "state").read_text())
    assert state == {"deployed": [scaffold.STARTER], "platforms": {scaffold.STARTER: platform}}


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
    assert sorted(p.name for p in (project / ".pdt").iterdir()) == [".gitignore", "state", "state.lock"]


def test_deploy_refuses_a_powershell_app_whose_scripts_certainly_fail(tmp_path, monkeypatch, capsys):
    project_with_starter(tmp_path, monkeypatch)
    (tmp_path / "report").mkdir()
    (tmp_path / "report" / "report.ps1").write_text("Read-Host 'name'\n")
    (tmp_path / "report" / "config.yml").write_text("schedule: daily\n")
    finding = powershell.Finding("interactive", "report.ps1", 1, "Read-Host waits for a person", True)
    monkeypatch.setattr(powershell, "scan", lambda app, provider: powershell.ScriptScan(
        ["report.ps1"], [], [], [finding]))
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    assert deploy.deploy("report") == 1
    assert "report.ps1:1: Read-Host waits for a person" in capsys.readouterr().out


def test_a_config_problem_names_the_app_once(tmp_path, monkeypatch, capsys):
    project_with_starter(tmp_path, monkeypatch)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: azure\n  region: eastu2\n")
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    assert deploy.deploy(scaffold.STARTER) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert f"{scaffold.STARTER}: platform.region in pdt.yml" in out
    assert f"{scaffold.STARTER}: {scaffold.STARTER}:" not in out


@pytest.mark.parametrize("yml,problem", [
    ("platform:\n  provider: aws\n  region: us-east-1\n  regoin: us-east-1\n",
     "pdt.yml: platform: unknown key 'regoin'"),
    ("docker-build: sometimes\nplatform:\n  provider: aws\n  region: us-east-1\n",
     "pdt.yml: docker-build"),
])
def test_a_shared_config_problem_names_the_app_once(tmp_path, monkeypatch, capsys, yml, problem):
    project_with_starter(tmp_path, monkeypatch)
    (tmp_path / "pdt.yml").write_text(yml)
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    assert deploy.deploy(scaffold.STARTER) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert f"{scaffold.STARTER}: {problem}" in out
    assert f"{scaffold.STARTER}: {scaffold.STARTER}" not in out


def test_a_config_load_problem_names_the_app(tmp_path, monkeypatch, capsys):
    project_with_starter(tmp_path, monkeypatch)
    (tmp_path / "pdt.yml").write_text("platform: azure\n")
    assert deploy.deploy(scaffold.STARTER) == 1
    assert f"{scaffold.STARTER}: pdt.yml" in " ".join(capsys.readouterr().out.split())


def test_an_apps_entry_problem_names_the_app_once(tmp_path, monkeypatch, capsys):
    project_with_starter(tmp_path, monkeypatch)
    (tmp_path / "pdt.yml").write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n"
        f"apps:\n  - name: {scaffold.STARTER}\n    docker-build: sometimes\n")
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    assert deploy.deploy(scaffold.STARTER) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert f"error: pdt.yml: apps entry '{scaffold.STARTER}'" in out
    assert f"{scaffold.STARTER}: pdt.yml: apps entry" not in out


@pytest.mark.parametrize("plan_only", [False, True])
def test_a_wrong_region_in_the_environment_stops_the_deploy(tmp_path, monkeypatch, capsys,
                                                            plan_only):
    project_with_starter(tmp_path, monkeypatch)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: azure\n")
    monkeypatch.setenv("PDT_AZURE_REGION", "eastu2")
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    assert deploy.deploy(scaffold.STARTER, assume_yes=True, plan_only=plan_only) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert f"{scaffold.STARTER}: the environment variable PDT_AZURE_REGION:" in out


def test_a_provider_script_pins_the_version_of_the_pdt_that_ran_it(tmp_path, monkeypatch):
    # From a clone, `uv run --script` imports pdt from src/ with no package
    # metadata beside it, so the version must arrive from the parent.
    project_with_starter(tmp_path, monkeypatch)
    src = Path(deploy.__file__).resolve().parent.parent
    code = f"import sys; sys.path.insert(0, {str(src)!r}); import pdt; print(pdt.__version__)"
    out = subprocess.run([sys.executable, "-S", "-c", code], env=deploy.provider_env(),
                         check=True, capture_output=True, text=True).stdout.strip()
    assert out == __version__ != "0.0.0.dev0"
