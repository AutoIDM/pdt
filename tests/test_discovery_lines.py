"""validate, run, and deploy say what pdt found on its own and where it came from."""

import os
from pathlib import Path

import io

import pytest
from rich.console import Console
from rich.text import Text

from conftest import add_app
from pdt import cli, config, console, deploy, deploy_common, powershell, regions
from pdt.deploy_azure import secret_actions
from test_powershell_env import FACTS

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
    os.environ["TENANT_ID"] = "t"
    return folder


@pytest.fixture
def ps_app(project, monkeypatch):
    folder = project / "ps-report"
    folder.mkdir()
    for name in ("cleanup.ps1", "report.ps1"):
        (folder / name).write_text("")
    (folder / "config.yml").write_text("schedule: daily\nenv:\n  optional: [NOTE]\n")
    facts = {**FACTS, "files": [{**FACTS["files"][0], "file": "cleanup.ps1", "envReads": []},
                                FACTS["files"][0]]}
    monkeypatch.setattr(powershell, "extract", lambda folder: facts)
    os.environ["TENANT_ID"] = "t"
    return folder


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


def text(capsys):
    return " ".join(capsys.readouterr().out.split())


def plain(lines):
    return [Text.from_markup(line).plain for line in lines]


def test_no_dot_env_file_says_only_the_terminal_counts(project):
    folder = add_app(project, "a")
    assert plain(config.env_file_lines(folder)) == [
        "no .env file in a or a folder above it, so pdt reads env vars only from this terminal"]


def test_the_dot_env_files_are_named_closest_first(project):
    folder = add_app(project, "a")
    (project / ".env").write_text("")
    assert plain(config.env_file_lines(project)) == [".env file read: .env"]
    (folder / ".env").write_text("")
    assert plain(config.env_file_lines(folder)) == [
        f".env files read, the first one wins: {Path('a', '.env')}, .env"]


def test_a_terminal_value_that_wins_over_dot_env_is_named_but_not_shown(project, monkeypatch):
    (project / ".env").write_text("SAME=1\nOTHER=from-file\nUNSET=x\n")
    monkeypatch.setenv("SAME", "1")
    monkeypatch.setenv("OTHER", "from-shell")
    monkeypatch.delenv("UNSET", raising=False)
    lines = plain(config.env_file_lines(project))
    assert lines[1] == ("this terminal already sets OTHER, so pdt uses that value and not "
                        "the one in .env")
    assert "from-shell" not in " ".join(lines)


def test_the_project_line_says_how_pdt_found_the_project(project, monkeypatch):
    assert plain([config.project_line()]) == [
        f"Project folder: {project}  (the first folder with pdt.yml, from here up)"]
    monkeypatch.setenv("PDT_PROJECT", str(project))
    assert plain([config.project_line()]) == [f"Project folder: {project}  (from PDT_PROJECT)"]


def test_the_env_vars_a_python_app_reads_are_named_with_their_line(py_app):
    assert plain(config.found_env_lines(config.merged_app("py-report"))) == [
        "env vars the code reads that config.yml does not list:",
        "  TENANT_ID  required, run.py line 4",
        "  NOTE       optional, run.py line 5"]


def test_the_env_vars_a_powershell_app_reads_are_named_with_their_line(ps_app):
    assert plain(config.found_env_lines(config.merged_app("ps-report"))) == [
        "env vars the code reads that config.yml does not list: "
        "TENANT_ID (required, report.ps1 line 4)"]


def test_a_listed_env_var_is_not_named(project, monkeypatch):
    add_app(project, "a", "env:\n  required: [TENANT_ID]\n  optional: [NOTE]\n", RUN_PY)
    assert config.found_env_lines(config.merged_app("a")) == []


def test_validate_says_what_it_found_for_each_app(py_app, project, monkeypatch, capsys):
    assert run_cli(monkeypatch, "validate") == 0
    out = text(capsys)
    assert f"Project folder: {project} (the first folder with pdt.yml, from here up)" in out
    assert ("py-report runs run.py env vars the code reads that config.yml does not list: "
            "TENANT_ID required, run.py line 4 NOTE optional, run.py line 5 "
            "no .env file in py-report or a folder above it") in out


def test_run_says_which_scripts_run_and_why(ps_app, monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kwargs: (
        cli.subprocess.CompletedProcess(command, 0)))
    assert run_cli(monkeypatch, "run", "ps-report") == 0
    assert ("ps-report runs, in order: cleanup.ps1, report.ps1 (each .ps1 that no other "
            "script loads, in name order) no .env file in ps-report") in text(capsys)


def test_run_says_it_runs_run_py(py_app, monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kwargs: (
        cli.subprocess.CompletedProcess(command, 0)))
    assert run_cli(monkeypatch, "run", "py-report") == 0
    assert "py-report runs run.py no .env file in py-report" in text(capsys)


@pytest.mark.parametrize("app", ["py-report", "ps-report"])
def test_deploy_says_what_it_found_before_the_provider_starts(py_app, ps_app, monkeypatch,
                                                              capsys, app):
    monkeypatch.setattr(powershell, "scan", lambda app, provider: powershell.ScriptScan(
        ["report.ps1"], [], [powershell.ModuleNeed("ImportExcel", None, "Import-Module in report.ps1")],
        [], entry_rule="run.ps1 is in the folder, so only it runs"))
    monkeypatch.setattr(regions, "choose_region", lambda *a: "")
    seen = []
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: seen.append(text(capsys)) or 1)
    deploy.deploy(app, assume_yes=True)
    found = {
        "py-report": "py-report runs run.py env vars the code reads that config.yml does not "
                     "list: TENANT_ID required, run.py line 4 NOTE optional, run.py line 5",
        "ps-report": "ps-report runs, in order: report.ps1 (run.ps1 is in the folder, so only it "
                     "runs) modules to install, found in the scripts: ImportExcel (latest, "
                     "Import-Module in report.ps1) env vars the code reads that config.yml does "
                     "not list: TENANT_ID (required, report.ps1 line 4)",
    }[app]
    assert found in seen[0]
    assert "no .env file in" in seen[0]


def test_a_plan_names_the_env_vars_in_a_secret_and_never_a_value():
    values = {"B_TOKEN": "secret-b", "A_KEY": "secret-a"}
    assert plain([deploy_common.secret_contents(values)]) == ["env vars A_KEY, B_TOKEN"]
    assert deploy_common.secret_contents({}) == "no env vars"
    assert plain(secret_actions("pdt-a", values, None, "{}")) == [
        "create Key Vault secret pdt-a (env vars A_KEY, B_TOKEN)"]


def test_the_cost_says_where_its_currency_comes_from(capsys):
    deploy_common.CostEstimate([("job", 1.0)], "list prices", currency="GBP").show()
    assert "pdt shows GBP, the currency of this computer's regional setting." in text(capsys)
    deploy_common.CostEstimate([("job", 1.0)], "list prices").show()
    assert "regional setting" not in text(capsys)


def test_a_value_prints_bold_on_a_terminal_and_plain_when_piped(monkeypatch):
    line = f"runs {console.value('run.py')}, then {console.value('[x]')}"
    for options, expected in (
            ({"force_terminal": True, "color_system": "standard"},
             "runs \x1b[1mrun.py\x1b[0m, then \x1b[1m[x]\x1b[0m"),
            ({"force_terminal": False}, "runs run.py, then [x]")):
        out = io.StringIO()
        monkeypatch.setattr(console, "_console", Console(file=out, soft_wrap=True, **options))
        console.detail(line)
        assert out.getvalue() == f"      {expected}\n"


def test_a_value_in_dim_text_prints_bold_and_not_dim(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, force_terminal=True,
                                                     color_system="standard", soft_wrap=True))
    console.styled(f"[dim]from {console.value('PDT_PROJECT')}[/]")
    assert out.getvalue() == "\x1b[2mfrom \x1b[0m\x1b[1mPDT_PROJECT\x1b[0m\n"


def test_detail_wraps_on_visible_text_and_keeps_a_hyphenated_name_whole(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(file=out, force_terminal=False,
                                                     soft_wrap=True, width=40))
    console.detail(f"  {console.value('ImportExcel')}  latest, {console.value('Import-Module')} "
                   f"in {console.value('report.ps1')}")
    assert out.getvalue() == ("        ImportExcel  latest,\n"
                              "        Import-Module in report.ps1\n")
