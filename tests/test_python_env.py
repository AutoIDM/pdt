"""Each env var a Python app's code reads is a required or optional env var, on every command path."""

import os

import pytest

from conftest import add_app
from pdt import cli, config, deploy, deploy_common, python_env

RUN_PY = '''import os

def main():
    tenant = os.environ["TENANT_ID"]
    note = os.getenv("NOTE")
    return 0
'''


@pytest.fixture(autouse=True)
def restore_environ():
    # python-dotenv writes straight into os.environ, past monkeypatch.
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def py_app(project, monkeypatch):
    folder = add_app(project, "py-report", "schedule: daily\n", RUN_PY)
    for name in ("TENANT_ID", "NOTE", "PDT_ENV_SECRET_RESOURCE"):
        monkeypatch.delenv(name, raising=False)
    return folder


def expected(project, app="py-report", read='run.py:4 reads os.environ["TENANT_ID"]'):
    return (
        f"missing required env var TENANT_ID ({read}). pdt looked in "
        f"the environment; there is no .env file in {project / app} or a folder above it. "
        f"To fix it, add each one as NAME=value to {project / '.env'}, then run the command again. "
        "If a script works without one that it reads, list that one under env: optional: in "
        f"{app}/config.yml instead.")


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


def unlisted(tmp_path, code, env=None):
    (tmp_path / "run.py").write_text(code)
    return python_env.unlisted_env_reads(tmp_path, env or {})


@pytest.mark.parametrize("code, found", [
    ('import os\nos.environ["A"]\n', ({"A": "run.py:2"}, [])),
    ('import os\nos.environ.get("A")\n', ({}, ["A"])),
    ('import os\nos.getenv("A")\n', ({}, ["A"])),
    ('import os\nos.environ.get("A", "x")\n', ({}, ["A"])),
    ('import os\nos.getenv("A", "x")\n', ({}, ["A"])),
    ('import os as o\no.environ["A"]\no.getenv("B")\n', ({"A": "run.py:2"}, ["B"])),
    ('from os import environ, getenv\nenviron["A"]\ngetenv("B")\nenviron.get("C")\n',
     ({"A": "run.py:2"}, ["B", "C"])),
    ('from os import environ as e\ne["A"]\n', ({"A": "run.py:2"}, [])),
])
def test_each_read_form(tmp_path, code, found):
    assert unlisted(tmp_path, code) == found


def test_a_required_read_wins_over_an_optional_one(tmp_path):
    assert unlisted(tmp_path, 'import os\nos.getenv("A")\nos.environ["A"]\n') == ({"A": "run.py:3"}, [])


@pytest.mark.parametrize("code", [
    'import os\nos.environ["A"] = "1"\nos.environ["A"]\n',
    'import os\nos.environ.setdefault("A", "1")\nos.environ["A"]\n',
    'import os\nos.putenv("A", "1")\nos.getenv("A")\n',
    'import os\ndel os.environ["A"]\nos.environ["A"]\n',
])
def test_a_name_the_code_sets_is_skipped(tmp_path, code):
    assert unlisted(tmp_path, code) == ({}, [])


def test_listed_system_and_pdt_names_are_skipped(tmp_path):
    code = ('import os\nos.environ["a"]\nos.getenv("B")\nos.environ["C"]\nos.environ["PATH"]\n'
            'os.environ["PDT_OUTPUT_DIR"]\n')
    assert unlisted(tmp_path, code, {"required": ["A"], "optional": ["b"], "one_of": [["C", "D"]]}) == ({}, [])


def test_a_name_built_at_run_time_is_not_found(tmp_path):
    assert unlisted(tmp_path, 'import os\nname = "A"\nos.environ[name]\nos.getenv(f"{name}_B")\n') == ({}, [])


def test_other_py_files_in_the_app_folder_count_after_run_py(tmp_path):
    (tmp_path / "helpers.py").write_text('import os\nos.environ["A"]\n')
    assert unlisted(tmp_path, 'import os\nos.environ["A"]\n') == ({"A": "run.py:2"}, [])
    assert unlisted(tmp_path, "") == ({"A": "helpers.py:2"}, [])


def test_a_listed_name_keeps_its_listing(py_app):
    (py_app / "config.yml").write_text("schedule: daily\nenv:\n  optional: [TENANT_ID]\n")
    assert config.env_spec(config.merged_app("py-report")) == {
        "optional": ["TENANT_ID", "NOTE"], "required": [], "read_by": {}}


def test_the_message_names_the_var_the_line_where_pdt_looked_and_the_fix(py_app, project):
    app = config.merged_app("py-report")
    assert config.missing_env(app) == expected(project)
    os.environ["TENANT_ID"] = "t"
    assert config.missing_env(app) == ""


def test_pdt_run_stops_before_the_app_starts(py_app, project, monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("the app started"))
    assert run_cli(monkeypatch, "run", "py-report") == 1
    assert expected(project) in " ".join(capsys.readouterr().out.split())


def test_pdt_validate_reports_the_read(py_app, project, monkeypatch, capsys):
    assert run_cli(monkeypatch, "validate") == 1
    assert f"py-report: {expected(project)}" in " ".join(capsys.readouterr().out.split())


def test_deploy_stops_before_dispatch(py_app, project, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
    assert deploy.deploy("py-report", assume_yes=True) == 1
    assert f"py-report: {expected(project)}" in " ".join(capsys.readouterr().out.split())


def test_deploy_sends_the_required_read_and_the_optional_read_when_set(py_app):
    app = config.merged_app("py-report")
    with pytest.raises(SystemExit):
        deploy_common.gather_secrets(app)
    os.environ["TENANT_ID"] = "t"
    assert deploy_common.gather_secrets(app) == {"TENANT_ID": "t"}
    os.environ["NOTE"] = "n"
    assert deploy_common.gather_secrets(app) == {"TENANT_ID": "t", "NOTE": "n"}


@pytest.mark.parametrize("action", ["diff", "save"])
def test_secrets_diff_and_save_check_the_read(py_app, project, monkeypatch, capsys, action):
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    assert deploy.secrets("py-report", action) == 1
    assert f"py-report: {expected(project)}" in " ".join(capsys.readouterr().out.split())


def test_a_powershell_app_and_a_python_app_give_the_same_message(py_app, project, monkeypatch):
    from test_powershell_env import FACTS
    from pdt import powershell
    folder = project / "ps-report"
    folder.mkdir()
    (folder / "report.ps1").write_text("")
    (folder / "config.yml").write_text("schedule: daily\nenv:\n  optional: [NOTE]\n")
    monkeypatch.setattr(powershell, "extract", lambda folder: FACTS)
    assert config.missing_env(config.merged_app("ps-report")) == expected(
        project, "ps-report", "report.ps1:4 reads $env:TENANT_ID")
    assert config.missing_env(config.merged_app("py-report")) == expected(project)
