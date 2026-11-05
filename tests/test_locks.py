import subprocess
import sys

import pytest

from conftest import add_app
from pdt import cli, config, run_windows_task


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.delenv(config.LOCK_PROJECT_ENV, raising=False)
    monkeypatch.delenv(config.SERVICE_CHILD_ENV, raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def locks(project):
    return project / ".pdt" / "locks"


def test_a_run_holds_its_app_and_lets_another_app_run(project):
    with config.run_lock("first"):
        with config.run_lock("second"):
            pass
        with pytest.raises(config.LockHeld, match="app-first.lock"):
            with config.run_lock("first"):
                pass


def test_a_run_releases_its_lock(project):
    with config.run_lock("first"):
        pass
    with config.run_lock("first"):
        pass


def test_the_project_lock_stops_a_run(project):
    with config.project_lock():
        with pytest.raises(config.LockHeld, match="project.lock"):
            with config.run_lock("first"):
                pass


def test_a_run_lets_a_deploy_take_the_project_lock(project):
    with config.run_lock("first"):
        with config.project_lock():
            pass


def test_the_project_lock_with_apps_stops_when_a_run_holds_one(project):
    with config.run_lock("first"):
        with pytest.raises(config.LockHeld, match="app-first.lock"):
            with config.project_lock(["first"]):
                pass
    with config.project_lock(["first"]):
        pass


def test_a_failed_project_lock_releases_what_it_took(project):
    with config.run_lock("first"):
        with pytest.raises(config.LockHeld):
            with config.project_lock(["first"]):
                pass
        with config.run_lock("second"):
            pass


def test_the_message_names_the_lock_and_the_project(project):
    with config.project_lock():
        with pytest.raises(config.LockHeld) as held:
            with config.project_lock():
                pass
    assert str(locks(project) / "project.lock") in str(held.value)
    assert project.name in str(held.value)


def test_the_locks_of_another_project_folder_can_be_named(project, monkeypatch, tmp_path_factory):
    other = tmp_path_factory.mktemp("served")
    monkeypatch.setenv(config.LOCK_PROJECT_ENV, str(other))
    with config.run_lock("first"):
        assert (other / ".pdt" / "locks" / "app-first.lock").is_file()
    assert (other / ".pdt" / ".gitignore").read_text() == "*\n"


def holder(project, name):
    code = ("import fcntl, sys, time; "
            f"handle = open({str(locks(project) / name)!r}, 'w'); "
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)")
    locks(project).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    assert proc.stdout.readline().strip() == b"held"
    return proc


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.run()


@pytest.fixture
def held_app(project):
    add_app(project, "report", "schedule: daily\n")
    (project / "report" / "run.py").write_text("print('ran')\n")
    proc = holder(project, "app-report.lock")
    yield project
    proc.kill()
    proc.wait()


def test_a_terminal_run_stops_with_exit_1_and_the_fact(held_app, monkeypatch, capsys):
    assert run_cli(monkeypatch, "run", "report") == 1
    assert "app-report.lock" in " ".join(capsys.readouterr().out.split())


def test_a_child_of_the_service_stops_with_its_own_exit_code(held_app, monkeypatch):
    monkeypatch.setenv(config.SERVICE_CHILD_ENV, "1")
    assert run_cli(monkeypatch, "run", "report") == config.LOCKED_EXIT


def test_a_scheduled_run_writes_the_fact_to_its_log_and_exits_1(project, tmp_path):
    app = project / "report"
    app.mkdir()
    proc = holder(project, "app-report.lock")
    logs = tmp_path / "logs"
    try:
        assert run_windows_task.main([str(app), str(logs), "file:///x/"]) == 1
    finally:
        proc.kill()
        proc.wait()
    text = next(logs.glob("*.log")).read_text()
    assert "app-report.lock" in text and "pdt: exit 1" in text


def test_a_scheduled_run_takes_the_lock_of_the_project(project):
    app = project / "report"
    app.mkdir()
    held = run_windows_task.app_lock(app)
    assert (locks(project) / "app-report.lock").is_file()
    held.close()
