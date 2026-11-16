"""A deploy of an app with no schedule asks for one and saves it under job: in the app's
pdt.yml, and a deploy of an app that sets a job: key in two places keeps one."""

import re
from pathlib import Path

import pytest
import yaml

from conftest import add_app, plain
from pdt import config, deploy
from pdt.config import ConfigError, merged_app

FILE = str(Path("my-report", "pdt.yml"))


@pytest.fixture
def ps_app(project):
    # A PowerShell app is a folder of .ps1 files; it needs no pdt.yml.
    folder = project / "ps-report"
    folder.mkdir()
    (folder / "report.ps1").write_text("Write-Output 'hi'\n")
    return folder


def schedule_in(folder):
    path = folder / "pdt.yml"
    return yaml.safe_load(path.read_text())["job"]["schedule"] if path.is_file() else None


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
    assert (ps_app / "config.yml").read_text() == 'storage: false\n\njob:\n  schedule: "daily"\n'
    assert not (ps_app / "pdt.yml").exists()


def test_a_set_schedule_is_never_asked_again(project, monkeypatch):
    folder = add_app(project, "my-report", "schedule: hourly\n")
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: pytest.fail("asked"))
    assert deploy.choose_schedule(merged_app("my-report"), False) == ""
    assert (folder / "pdt.yml").read_text() == "schedule: hourly\n"


def test_in_a_menu_the_marker_starts_on_daily(ps_app, monkeypatch, press):
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    press("down", "enter")
    assert deploy.choose_schedule(merged_app("ps-report"), False) == ""
    assert schedule_in(ps_app) == "weekly"


def test_the_last_line_of_the_menu_asks_for_a_cron_expression(ps_app, monkeypatch, press):
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(deploy.console, "ask", lambda _question, _default: "30 6 * * 1-5")
    press("down", "down", "down", "enter")
    assert deploy.choose_schedule(merged_app("ps-report"), False) == ""
    assert schedule_in(ps_app) == "30 6 * * 1-5"


def test_a_null_schedule_in_the_job_section_is_replaced_where_it_is(project):
    folder = add_app(project, "my-report", "job:\n  schedule: null  # pick later\n"
                                           "  timezone: America/Chicago\nstorage: false\n")
    assert deploy.choose_schedule(merged_app("my-report"), True) == ""
    assert (folder / "pdt.yml").read_text() == ('job:\n  schedule: "daily"\n'
                                                "  timezone: America/Chicago\nstorage: false\n")
    assert merged_app("my-report")["schedule"] == "daily"


def test_a_job_section_with_no_schedule_gets_it(project):
    folder = add_app(project, "my-report", "job:\n  timezone: America/Chicago\n")
    assert deploy.choose_schedule(merged_app("my-report"), True) == ""
    assert (folder / "pdt.yml").read_text() == ('job:\n  timezone: America/Chicago\n'
                                                '  schedule: "daily"\n')


@pytest.mark.parametrize("text, saved", [
    ("job:\n# choose later\n  schedule: null\n", 'job:\n# choose later\n  schedule: "daily"\n'),
    ('job:\n  "schedule": null\n  timezone: Etc/UTC\n',
     'job:\n  schedule: "daily"\n  timezone: Etc/UTC\n'),
    ("job:\n  timezone: Etc/UTC\n  'schedule': null\n",
     'job:\n  timezone: Etc/UTC\n  schedule: "daily"\n'),
    ("job:\n  schedule:\n    null\n  timezone: Etc/UTC\n",
     'job:\n  schedule: "daily"\n  timezone: Etc/UTC\n'),
])
def test_the_schedule_replaces_the_null_one_in_the_job_section(project, text, saved):
    folder = add_app(project, "my-report", text)
    assert deploy.choose_schedule(merged_app("my-report"), True) == ""
    assert (folder / "pdt.yml").read_text() == saved
    assert merged_app("my-report")["schedule"] == "daily"


def test_a_file_with_no_job_section_gets_one(project):
    folder = add_app(project, "my-report", "# my notes\nstorage: false\n")
    assert deploy.choose_schedule(merged_app("my-report"), True) == ""
    assert (folder / "pdt.yml").read_text() == ('# my notes\nstorage: false\n\n'
                                                'job:\n  schedule: "daily"\n')


@pytest.mark.parametrize("text, saved", [
    ("job: {schedule: null, timezone: Etc/UTC}  # one line\n",
     {"job": {"schedule": "daily", "timezone": "Etc/UTC"}}),
    ("job: {schedule: null}\nschedule:\n  null\n", {"job": {"schedule": "daily"}, "schedule": None}),
    ("job: null\n", {"job": {"schedule": "daily"}}),
])
def test_a_job_section_that_a_text_edit_would_break_is_written_again_as_a_block(
        project, text, saved):
    folder = add_app(project, "my-report", text)
    assert deploy.choose_schedule(merged_app("my-report"), True) == ""
    assert yaml.safe_load((folder / "pdt.yml").read_text()) == saved
    assert merged_app("my-report")["schedule"] == "daily"


def test_a_new_key_goes_after_the_whole_first_entry_and_comments_stay(project):
    folder = add_app(project, "my-report", "job:\n  timezone: >-  # folded\n    Etc/UTC\n"
                                           "  # later\n")
    assert deploy.choose_schedule(merged_app("my-report"), True) == ""
    assert (folder / "pdt.yml").read_text() == ('job:\n  timezone: >-  # folded\n    Etc/UTC\n'
                                                '  schedule: "daily"\n  # later\n')


def test_a_refused_write_fails_only_that_app(project, monkeypatch, capsys):
    add_app(project, "my-report", "storage: false\n")

    def refuse(app, key, value, drop_top_level=False):
        raise ConfigError("my-report/pdt.yml: pdt could not write schedule under job:.")

    monkeypatch.setattr(config, "save_job_key", refuse)
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: pytest.fail("dispatched"))
    assert deploy.deploy("my-report", assume_yes=True) == 1
    assert ("error: my-report: my-report/pdt.yml: pdt could not write schedule under job:."
            in plain(capsys.readouterr().out.splitlines()))


def test_a_job_section_pdt_cannot_write_names_the_file_and_the_key(project):
    folder = add_app(project, "my-report", "job: &shared {timezone: Etc/UTC}\nconfig: *shared\n")
    with pytest.raises(ConfigError, match=re.escape(FILE) + r": .*`schedule: \"daily\"` under `job:`"):
        config.save_job_key(merged_app("my-report"), "schedule", "daily")
    assert (folder / "pdt.yml").read_text() == "job: &shared {timezone: Etc/UTC}\nconfig: *shared\n"


BOTH = "job:\n  schedule: daily  # my choice\nschedule: weekly\nstorage: false\n"


def test_with_yes_both_schedules_stay_and_the_job_one_is_used(project, capsys):
    folder = add_app(project, "my-report", BOTH)
    deploy.keep_one_job_key(merged_app("my-report"), True)
    assert (folder / "pdt.yml").read_text() == BOTH
    assert merged_app("my-report")["schedule"] == "daily"
    assert (f"note: {FILE} sets both job.schedule daily and the top-level schedule "
            "weekly; this deploy uses job.schedule daily.") in " ".join(capsys.readouterr().out.split())


def test_a_plan_keeps_both_schedules(project, monkeypatch):
    folder = add_app(project, "my-report", BOTH)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: pytest.fail("asked"))
    with config.plan_only():
        deploy.keep_one_job_key(merged_app("my-report"), False)
    assert (folder / "pdt.yml").read_text() == BOTH


def test_no_one_to_ask_keeps_both_schedules(project, monkeypatch):
    folder = add_app(project, "my-report", BOTH)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: False)
    deploy.keep_one_job_key(merged_app("my-report"), False)
    assert (folder / "pdt.yml").read_text() == BOTH


def test_keeping_the_job_schedule_removes_the_top_level_one(project, monkeypatch, press, capsys):
    folder = add_app(project, "my-report", BOTH)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    press("enter")
    deploy.keep_one_job_key(merged_app("my-report"), False)
    assert (folder / "pdt.yml").read_text() == "job:\n  schedule: daily  # my choice\nstorage: false\n"
    out = plain(capsys.readouterr().out.splitlines())
    assert f"{FILE} sets schedule both under job: and at the top level." in out
    assert "Which schedule should my-report keep?" in out
    assert any(line.endswith("job.schedule (every day at 00:00)  daily") for line in out)
    assert any(line.endswith("top-level schedule (every Sunday at 00:00)  weekly") for line in out)
    assert ("Kept job.schedule daily (every day at 00:00) and removed the top-level schedule "
            f"weekly from {FILE}.") in out


def test_keeping_the_top_level_schedule_moves_it_under_job(project, monkeypatch, press, capsys):
    folder = add_app(project, "my-report", BOTH)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    press("down", "enter")
    deploy.keep_one_job_key(merged_app("my-report"), False)
    assert (folder / "pdt.yml").read_text() == 'job:\n  schedule: "weekly"\nstorage: false\n'
    assert merged_app("my-report")["schedule"] == "weekly"
    assert (f"Saved job.schedule weekly (every Sunday at 00:00) to {FILE} and removed "
            "the top-level schedule.") in plain(capsys.readouterr().out.splitlines())


def test_with_no_menu_a_number_picks_the_value_to_keep(project, monkeypatch, capsys):
    folder = add_app(project, "my-report", BOTH)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(deploy.console, "can_menu", lambda: False)
    answers = iter(["3", "2"])
    monkeypatch.setattr(deploy.console, "ask", lambda _question, _default: next(answers))
    deploy.keep_one_job_key(merged_app("my-report"), False)
    assert (folder / "pdt.yml").read_text() == 'job:\n  schedule: "weekly"\nstorage: false\n'
    out = plain(capsys.readouterr().out.splitlines())
    assert "  1) job.schedule (every day at 00:00)  daily" in out
    assert "  2) top-level schedule (every Sunday at 00:00)  weekly" in out


@pytest.mark.parametrize("provider, text, problem", [
    ("azure", "job:\n  schedule: daily\nschedule: not-a-schedule\n",
     "schedule 'not-a-schedule' is not a shorthand"),
    ("windows", "job:\n  schedule: daily\nschedule: \"*/7 * * * *\"\n",
     "schedule: cron minute interval '*/7' resets each hour"),
    ("aws", "job:\n  timezone: Etc/UTC\ntimezone: Mars/Olympus\n",
     "timezone 'Mars/Olympus' is not a time zone name"),
    ("azure", "job:\n  timezone: Etc/UTC\ntimezone: America/Chicago\n",
     "timezone 'America/Chicago': Azure evaluates cron schedules only in UTC"),
])
def test_a_top_level_value_that_is_not_valid_is_not_offered(project, monkeypatch, capsys,
                                                             provider, text, problem):
    (project / "pdt.yml").write_text(f"platform:\n  provider: {provider}\n")
    folder = add_app(project, "my-report", text)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(deploy.console, "can_menu", lambda: pytest.fail("asked"))
    deploy.keep_one_job_key(merged_app("my-report"), False)
    assert (folder / "pdt.yml").read_text() == text
    out = " ".join(capsys.readouterr().out.split())
    assert "the top-level value is not valid (" + problem in out
    assert out.endswith("this deploy uses job.schedule daily." if text.startswith("job:\n  schedule")
                        else "this deploy uses job.timezone Etc/UTC.")


SAME = "job:\n  schedule: daily\nschedule: daily\n"


def test_the_same_schedule_in_both_places_loses_the_top_level_one_with_no_question(
        project, monkeypatch, capsys):
    folder = add_app(project, "my-report", SAME)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(deploy.console, "can_menu", lambda: pytest.fail("asked"))
    deploy.keep_one_job_key(merged_app("my-report"), False)
    assert (folder / "pdt.yml").read_text() == "job:\n  schedule: daily\n"
    assert plain(capsys.readouterr().out.splitlines()) == [
        f"Removed the top-level schedule daily from {FILE}; job.schedule has the same "
        "value."]


@pytest.mark.parametrize("assume_yes, at_terminal", [(True, True), (False, False)])
def test_the_same_schedule_in_both_places_stays_without_a_terminal_or_with_yes(
        project, monkeypatch, capsys, assume_yes, at_terminal):
    folder = add_app(project, "my-report", SAME)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: at_terminal)
    deploy.keep_one_job_key(merged_app("my-report"), assume_yes)
    assert (folder / "pdt.yml").read_text() == SAME
    assert capsys.readouterr().out == ""


def test_a_timezone_in_both_places_is_asked_the_same_way(project, monkeypatch, press):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n")
    folder = add_app(project, "my-report", "job:\n  timezone: Etc/UTC\ntimezone: America/Chicago\n")
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    press("down", "enter")
    deploy.keep_one_job_key(merged_app("my-report"), False)
    assert (folder / "pdt.yml").read_text() == 'job:\n  timezone: "America/Chicago"\n'


@pytest.mark.parametrize("text", ["job:\n  schedule: daily\n", "schedule: weekly\n",
                                  "job:\n  schedule: null\nschedule: weekly\n"])
def test_one_set_schedule_is_not_asked_about(project, monkeypatch, text):
    folder = add_app(project, "my-report", text)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: pytest.fail("asked"))
    deploy.keep_one_job_key(merged_app("my-report"), False)
    assert (folder / "pdt.yml").read_text() == text
