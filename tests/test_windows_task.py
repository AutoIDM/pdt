import re
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from conftest import add_app, plain
from pdt import config, deploy_windows

SID = "S-1-5-21-1-2-3-1001"
TASK_PATH = "\\pdt\\pdt-my-report"
LEGACY_PATH = "\\pdt-my-report"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def windows_app(project, schedule="hourly"):
    add_app(project, "my-report", f"schedule: {schedule}\ntimezone: local\n")
    return config.merged_app("my-report")


@pytest.mark.parametrize(("cron", "times", "every"), [
    ("*/15 * * * *", [(0, 0)], 15),
    ("*/7 * * * *", [(0, minute) for minute in range(0, 60, 7)], 60),
    ("30,45 * * * *", [(0, 30), (0, 45)], 60),
    ("0,30 9,17 * * 1-5", [(9, 0), (9, 30), (17, 0), (17, 30)], 0),
    ("0 9 * 1 *", [(9, 0)], 0),
])
def test_trigger_times_are_the_start_times_and_the_repeat_in_minutes(cron, times, every):
    assert config.windows_trigger_times(cron) == (times, every)
    assert config.windows_schedule_problem(cron) == ""


def test_a_minute_step_that_does_not_divide_60_gets_one_hourly_trigger_per_minute():
    times, every = config.windows_trigger_times("*/7 * * * *")
    assert len(times) == 9
    assert every == 60


def test_weekdays_are_weekly_and_a_month_is_monthly_on_every_day():
    assert config.windows_trigger_days("0,30 9,17 * * 1-5") == ("weekly", [1, 2, 3, 4, 5], [])
    assert config.windows_trigger_days("0 9 * 1 *") == ("monthly", list(range(1, 32)), [1])
    assert config.windows_trigger_days("0 9 * * *") == ("daily", [], [])


def test_a_cron_that_needs_more_than_48_triggers_is_rejected_naming_the_limit():
    problem = config.windows_schedule_problem("* 9-17 * * *")
    assert "540" in problem
    assert "48" in problem


def test_a_cron_with_a_day_of_month_and_a_day_of_week_is_rejected():
    assert "day-of-week" in config.windows_schedule_problem("0 9 1 * 1")


def test_validate_reports_a_windows_schedule_problem(project):
    add_app(project, "my-report", 'schedule: "* 9-17 * * *"\ntimezone: local\n')
    problems = config.validate_app("my-report")
    assert any("my-report: schedule:" in problem and "48" in problem for problem in problems)


def test_validate_reports_no_windows_problem_for_a_cron_another_provider_accepts(
        tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: aws\n")
    monkeypatch.chdir(tmp_path)
    add_app(tmp_path, "my-report", 'schedule: "0 9 1 * 1"\n')
    assert not any("Task Scheduler" in problem for problem in config.validate_app("my-report"))


def test_a_repeating_trigger_has_one_start_and_a_15_minute_interval():
    xml = deploy_windows.schedule_triggers("*/15 * * * *")
    assert xml.count("<CalendarTrigger>") == 1
    assert re.findall(r"<StartBoundary>(.*?)</StartBoundary>", xml) == ["2000-01-01T00:00:00"]
    assert "<Repetition><Interval>PT15M</Interval>" in xml
    assert "<ScheduleByDay>" in xml


def test_a_repeating_trigger_lasts_to_the_end_of_the_day_and_every_trigger_has_no_time_limit():
    hourly = deploy_windows.schedule_triggers("0 * * * *")
    assert "<Duration>PT1381M</Duration>" in hourly
    quarter = deploy_windows.schedule_triggers("*/15 * * * *")
    assert "<Duration>PT1426M</Duration>" in quarter
    for xml in (hourly, quarter, deploy_windows.schedule_triggers("0,30 9,17 * * 1-5"),
                deploy_windows.schedule_triggers("*/7 * * * *")):
        assert xml.count("</StartBoundary><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
                         "<Enabled>true</Enabled>") == xml.count("<CalendarTrigger>")


def test_a_minute_step_of_7_makes_9_hourly_triggers():
    xml = deploy_windows.schedule_triggers("*/7 * * * *")
    assert xml.count("<CalendarTrigger>") == 9
    assert xml.count("<Interval>PT1H</Interval>") == 9
    assert re.findall(r"T00:(\d\d):00", xml) == [f"{minute:02d}" for minute in range(0, 60, 7)]


def test_a_weekday_cron_makes_four_weekly_triggers_with_no_repetition():
    xml = deploy_windows.schedule_triggers("0,30 9,17 * * 1-5")
    assert xml.count("<CalendarTrigger>") == 4
    assert re.findall(r"<StartBoundary>(.*?)</StartBoundary>", xml) == [
        "2000-01-01T09:00:00", "2000-01-01T09:30:00",
        "2000-01-01T17:00:00", "2000-01-01T17:30:00"]
    assert xml.count("<DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/>"
                     "</DaysOfWeek>") == 4
    assert "<Repetition>" not in xml


def test_a_cron_windows_cannot_run_raises_the_shared_problem():
    with pytest.raises(deploy_windows.WindowsDeployError, match="48"):
        deploy_windows.schedule_triggers("* 9-17 * * *")


def test_the_task_description_comes_from_describe_schedule(project):
    description, _xml = deploy_windows.task_xml(windows_app(project, "'*/15 * * * *'"))
    assert description == config.describe_schedule("*/15 * * * *")


def test_the_deploying_user_and_sid_come_from_whoami(monkeypatch):
    monkeypatch.setattr(
        deploy_windows.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, f'"PC\\jon","{SID}"\n', ""))
    assert deploy_windows._deploying_user() == (r"PC\jon", SID)


def schtasks_row(result="0", comment="Managed by pdt; runs my-report from X"):
    row = [""] * 11
    row[deploy_windows.LAST_RESULT_COLUMN] = result
    row[deploy_windows.COMMENT_COLUMN] = comment
    return '"' + '","'.join(row) + '"\n'


def fake_query(monkeypatch, stdout, code=0):
    calls = []
    monkeypatch.setattr(deploy_windows, "_schtasks",
                        lambda *args: calls.append(args) or subprocess.CompletedProcess(
                            args, code, stdout, ""))
    return calls


def test_task_state_is_ready_for_a_managed_task_that_is_not_running(monkeypatch):
    calls = fake_query(monkeypatch, schtasks_row("0"))
    assert deploy_windows._task_state(TASK_PATH) == "ready"
    assert calls == [("/Query", "/TN", TASK_PATH, "/V", "/FO", "CSV", "/NH")]


def test_task_state_is_running_when_the_last_result_is_267009(monkeypatch):
    fake_query(monkeypatch, schtasks_row("267009"))
    assert deploy_windows._task_state(TASK_PATH) == "running"


def test_task_state_is_unmanaged_when_the_comment_does_not_start_with_the_marker(monkeypatch):
    fake_query(monkeypatch, schtasks_row("0", "Backup by someone else"))
    assert deploy_windows._task_state(TASK_PATH) == "unmanaged"


def test_task_state_is_absent_when_schtasks_returns_a_nonzero_code(monkeypatch):
    fake_query(monkeypatch, "", code=1)
    assert deploy_windows._task_state(TASK_PATH) == "absent"


def test_a_failed_pause_names_the_refused_change_and_the_fix(monkeypatch, capsys):
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_state", lambda path: "ready")
    monkeypatch.setattr(deploy_windows, "_schtasks",
                        lambda *args: subprocess.CompletedProcess(args, 5, "", "Access denied"))
    assert deploy_windows.pause({"name": "my-report"}, True) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "refused /Change /DISABLE for \\pdt\\pdt-my-report: Access denied" in out
    assert "deploy the app again from this Windows account" in out


def deploy_script(project, monkeypatch, states):
    app = windows_app(project)
    scripts = []
    shown = []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_state", lambda path: states.get(path, "absent"))
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: (r"PC\jon", SID))
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append(script) or True)
    monkeypatch.setattr(deploy_windows, "confirm",
                        lambda lines, assume_yes, cost=None: shown.extend(lines) or True)
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    (script,) = scripts
    return script, shown


def test_deploy_registers_in_the_pdt_folder_with_the_users_security_descriptor(
        project, monkeypatch):
    script, _shown = deploy_script(project, monkeypatch, {})
    assert "-TaskPath '\\pdt\\'" in script
    assert f"SetSecurityDescriptor('D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;FRFWFX;;;{SID})', 0)" in script
    assert script.index("Register-ScheduledTask") < script.index("SetSecurityDescriptor")


def test_deploy_refuses_to_replace_an_unmanaged_task_in_the_pdt_folder(project, monkeypatch):
    script, _shown = deploy_script(project, monkeypatch, {})
    assert "StartsWith('Managed by pdt;')" in script
    assert script.index("is not managed by PDT") < script.index("Register-ScheduledTask")


def test_deploy_unregisters_a_managed_task_in_the_root_folder(project, monkeypatch):
    script, _shown = deploy_script(project, monkeypatch, {})
    assert "-TaskPath '\\' -ErrorAction SilentlyContinue" in script
    assert "Unregister-ScheduledTask -InputObject $legacy" in script
    assert script.index("SetSecurityDescriptor") < script.index("Unregister-ScheduledTask")


def test_deploy_plans_the_root_task_deletion_only_when_that_task_exists(project, monkeypatch):
    _script, shown = deploy_script(project, monkeypatch, {LEGACY_PATH: "ready"})
    assert any("delete the task \\pdt-my-report" in line for line in plain(shown))


def test_deploy_without_a_root_task_plans_no_deletion(project, monkeypatch):
    _script, shown = deploy_script(project, monkeypatch, {})
    assert not any("delete the task" in line for line in plain(shown))


def test_deploy_refuses_an_unmanaged_task_in_the_pdt_folder_before_asking(
        project, monkeypatch, capsys):
    app = windows_app(project)
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_state",
                        lambda path: "unmanaged" if path == TASK_PATH else "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: (r"PC\jon", SID))
    assert deploy_windows.deploy(app, assume_yes=True) == 1
    assert "\\pdt\\pdt-my-report exists but is not managed by PDT" in capsys.readouterr().out


def test_plan_lists_the_user_who_may_start_pause_and_unpause(project):
    app = windows_app(project)
    lines = plain(deploy_windows.plan(
        app, "create", "hourly", r"PC\jon", {}, False, legacy=True))
    assert lines[2] == "runs only while this PC is on; a missed run starts when the PC is next on"
    assert lines[3] == r"let PC\jon start, pause, and unpause the task with no administrator prompt"
    assert lines[-1].startswith("delete the task \\pdt-my-report")


def destroy_script(project, monkeypatch, states, leftovers=False):
    app = windows_app(project)
    if leftovers:
        (project / "ProgramData" / "pdt" / "my-report" / "logs").mkdir(parents=True)
    scripts = []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_state", lambda path: states.get(path, "absent"))
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append(script) or True)
    assert deploy_windows.destroy(app, assume_yes=True) == 0
    (script,) = scripts
    return script


def test_destroy_unregisters_a_managed_task_in_both_folders_and_removes_the_empty_folder(
        project, monkeypatch):
    script = destroy_script(project, monkeypatch, {TASK_PATH: "ready", LEGACY_PATH: "ready"})
    assert "-TaskName 'pdt-my-report' -TaskPath '\\pdt\\'" in script
    assert "-TaskName 'pdt-my-report' -TaskPath '\\'" in script
    assert script.count("Unregister-ScheduledTask -InputObject $task -Confirm:$false") == 2
    assert script.count("StartsWith('Managed by pdt;')") == 2
    assert "schtasks.exe" not in script
    assert "DeleteFolder('pdt', 0)" in script
    assert script.index("-TaskPath '\\pdt\\'") < script.index("-TaskPath '\\'")
    assert script.index("-TaskPath '\\'") < script.index("DeleteFolder")


def test_destroy_still_unregisters_the_root_task_when_the_pdt_task_is_absent(
        project, monkeypatch):
    script = destroy_script(project, monkeypatch, {LEGACY_PATH: "ready"})
    assert "-TaskName 'pdt-my-report' -TaskPath '\\'" in script
    assert "Unregister-ScheduledTask -InputObject $task" in script
    assert "DeleteFolder('pdt', 0)" in script


def test_destroy_with_only_leftovers_still_ends_with_the_folder_cleanup(project, monkeypatch):
    script = destroy_script(project, monkeypatch, {}, leftovers=True)
    assert "try { $folder = $service.GetFolder('\\pdt') } catch { $folder = $null }" in script
    assert "DeleteFolder('pdt', 0)" in script
    assert script.index("DeleteFolder") < script.index("Remove-Item -Recurse -Force")


def test_deploy_exports_the_current_task_and_restores_it_when_the_change_fails(
        project, monkeypatch):
    script, _shown = deploy_script(project, monkeypatch, {TASK_PATH: "ready"})
    export = script.index("Export-ScheduledTask -TaskName $name -TaskPath '\\pdt\\'")
    register = script.index("Register-ScheduledTask -TaskName $name", export)
    attempt = script.index("try {", register)
    catch = script.index("} catch {", attempt)
    restore = script.index("Register-ScheduledTask -TaskName $name -TaskPath '\\pdt\\' "
                           "-Xml $previous -Force", catch)
    remove = script.index("else { Unregister-ScheduledTask -TaskName $name -TaskPath '\\pdt\\'",
                          restore)
    assert attempt < script.index("SetSecurityDescriptor") < catch
    assert attempt < script.index("Unregister-ScheduledTask -InputObject $legacy") < catch
    assert catch < restore < remove < script.index("throw }", remove)


def test_deploy_refuses_while_the_root_task_runs_and_changes_nothing(
        project, monkeypatch, capsys):
    app = windows_app(project)
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: "powershell.exe")
    monkeypatch.setattr(deploy_windows, "_task_state",
                        lambda path: "running" if path == LEGACY_PATH else "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: (r"PC\jon", SID))
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda *args, **kwargs: pytest.fail("deploy must change nothing"))
    monkeypatch.setattr(deploy_windows, "stage_release",
                        lambda *args: pytest.fail("deploy must change nothing"))
    assert deploy_windows.deploy(app, assume_yes=True) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "is running from Windows task \\pdt-my-report" in out


def test_deploy_does_not_plan_the_root_task_deletion_when_that_task_is_unmanaged(
        project, monkeypatch):
    _script, shown = deploy_script(project, monkeypatch, {LEGACY_PATH: "unmanaged"})
    assert not any("delete the task" in line for line in plain(shown))


NOW = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)


def task_info(**changes):
    info = {"enabled": True, "result": 0, "missed": 0, "last_run": NOW, "next_run": None}
    return info | changes


def test_a_disabled_task_of_an_app_that_is_not_paused_is_a_problem():
    problems = deploy_windows.task_problems({"name": "my-report"}, task_info(enabled=False), [NOW])
    assert len(problems) == 1
    assert "is disabled" in problems[0]
    assert "pdt unpause my-report" in problems[0]


def test_a_disabled_task_of_a_paused_app_is_no_problem():
    app = {"name": "my-report", "pause": True}
    assert deploy_windows.task_problems(app, task_info(enabled=False), [NOW]) == []


def test_missed_runs_are_a_problem_and_name_the_count():
    problems = deploy_windows.task_problems({"name": "my-report"}, task_info(missed=3), [NOW])
    assert len(problems) == 1
    assert "3 missed runs" in problems[0]


def test_a_failed_start_with_no_log_is_a_problem_and_names_the_result():
    problems = deploy_windows.task_problems(
        {"name": "my-report"}, task_info(result=0x80070002), [NOW - timedelta(hours=1)])
    assert len(problems) == 1
    assert "could not start my-report" in problems[0]
    assert "0x80070002" in problems[0]
    assert "pdt deploy my-report" in problems[0]


def test_a_log_from_an_earlier_day_later_than_the_window_does_not_hide_the_failed_start():
    late = NOW + deploy_windows.LOG_START_WINDOW + timedelta(seconds=1)
    problems = deploy_windows.task_problems(
        {"name": "my-report"}, task_info(result=0x80070002), [late])
    assert len(problems) == 1
    assert "could not start my-report" in problems[0]


def test_a_log_at_the_end_of_the_window_is_the_failed_starts_own_log():
    started = NOW + deploy_windows.LOG_START_WINDOW
    assert deploy_windows.task_problems(
        {"name": "my-report"}, task_info(result=1), [started]) == []


def test_a_failed_run_that_wrote_a_log_is_no_problem():
    started = NOW - timedelta(seconds=1)
    assert deploy_windows.task_problems(
        {"name": "my-report"}, task_info(result=1), [started]) == []


@pytest.mark.parametrize("result", [0, 0x41301, 0x41303])
def test_a_result_of_success_running_or_not_yet_run_is_no_start_problem(result):
    assert deploy_windows.task_problems(
        {"name": "my-report"}, task_info(result=result), []) == []
