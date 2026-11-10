"""A deploy of an app with no schedule asks for one and saves it in the app's pdt.yml."""

import pytest
import yaml

from conftest import add_app
from pdt import deploy
from pdt.config import merged_app


@pytest.fixture
def ps_app(project):
    # A PowerShell app is a folder of .ps1 files; it needs no pdt.yml.
    folder = project / "ps-report"
    folder.mkdir()
    (folder / "report.ps1").write_text("Write-Output 'hi'\n")
    return folder


def schedule_in(folder):
    path = folder / "pdt.yml"
    return yaml.safe_load(path.read_text()).get("schedule") if path.is_file() else None


def test_the_saved_schedule_is_the_merged_one(ps_app):
    app = merged_app("ps-report")
    deploy.choose_schedule(app, True)
    assert app["schedule"] == merged_app("ps-report")["schedule"] == "daily"


def test_the_user_can_pick_a_schedule_and_comments_survive(project, monkeypatch):
    folder = add_app(project, "my-report", "# my notes\nstorage: false\n")
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(deploy.console, "ask", lambda _question, _default: "weekly")
    assert deploy.choose_schedule(merged_app("my-report"), False) == ""
    assert (folder / "pdt.yml").read_text().startswith("# my notes\nstorage: false\n")
    assert schedule_in(folder) == "weekly"


def test_a_wrong_answer_is_asked_again(ps_app, monkeypatch):
    answers = iter(["every tuesday", "30 6 * * 1-5"])
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(deploy.console, "ask", lambda _question, _default: next(answers))
    assert deploy.choose_schedule(merged_app("ps-report"), False) == ""
    assert schedule_in(ps_app) == "30 6 * * 1-5"


def test_an_empty_answer_takes_daily(ps_app, monkeypatch):
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(deploy.console, "ask", lambda _question, _default: "")
    assert deploy.choose_schedule(merged_app("ps-report"), False) == ""
    assert schedule_in(ps_app) == "daily"


def test_no_one_to_ask_and_no_yes_names_the_fix(ps_app, monkeypatch):
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: False)
    problem = deploy.choose_schedule(merged_app("ps-report"), False)
    assert "schedule: daily" in problem and "ps-report/pdt.yml" in problem
    assert "--yes" in problem
    assert not (ps_app / "pdt.yml").exists()


def test_the_schedule_goes_into_an_existing_config_yml(ps_app):
    (ps_app / "config.yml").write_text("storage: false\n")
    problem = deploy.choose_schedule(merged_app("ps-report"), True)
    assert problem == ""
    assert (ps_app / "config.yml").read_text() == 'storage: false\nschedule: "daily"\n'
    assert not (ps_app / "pdt.yml").exists()


def test_a_set_schedule_is_never_asked_again(project, monkeypatch):
    folder = add_app(project, "my-report", "schedule: hourly\n")
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: pytest.fail("asked"))
    assert deploy.choose_schedule(merged_app("my-report"), False) == ""
    assert schedule_in(folder) == "hourly"
