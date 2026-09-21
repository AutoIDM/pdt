from conftest import add_app
from pdt import cli, deploy
from pdt.deploy_common import LogPage, NotDeployed, RemoteJob, Run, RunState, run_once


def remote_job(states, pages):
    def start():
        return Run("r1", RunState.STARTING, "https://logs.example/r1")

    def poll(run):
        return Run(run.id, states.pop(0), run.logs_url, 7)

    def read_logs(run, cursor):
        return pages.pop(0)

    return RemoteJob(start, poll, read_logs)


def test_run_once_streams_each_page_and_drains_after_completion(capsys):
    job = remote_job(
        [RunState.RUNNING, RunState.SUCCEEDED],
        [LogPage(["starting"], "one"), LogPage(["working"], "two"), LogPage(["done"], "three")],
    )
    assert run_once(job, True, "hello-world", "azure", sleep=lambda _: None) == 0
    out = capsys.readouterr().out
    assert out.count("starting") == 1
    assert out.count("working") == 1
    assert out.count("done") == 1
    assert "Succeeded in" in out


def test_run_once_returns_the_failed_exit_code(capsys):
    job = remote_job([RunState.FAILED], [LogPage([], "one"), LogPage([], "two")])
    assert run_once(job, True, "hello-world", "azure", sleep=lambda _: None) == 7
    assert "Failed after" in capsys.readouterr().out


def test_run_once_returns_one_when_the_failed_exit_code_is_unknown():
    job = RemoteJob(
        lambda: Run("r1", RunState.FAILED, "url"),
        lambda run: run,
        lambda run, cursor: LogPage([], cursor),
    )
    assert run_once(job, True, "hello-world", "azure", sleep=lambda _: None) == 1


def test_run_once_does_not_poll_without_wait():
    calls = []
    job = RemoteJob(
        lambda: Run("r1", RunState.STARTING, "url"),
        lambda run: calls.append("poll") or run,
        lambda run, cursor: calls.append("logs") or LogPage([], cursor),
    )
    assert run_once(job, False, "hello-world", "azure") == 0
    assert calls == []


def test_run_once_detaches_on_keyboard_interrupt(capsys):
    job = RemoteJob(
        lambda: Run("r1", RunState.STARTING, "url"),
        lambda run: run,
        lambda run, cursor: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    assert run_once(job, True, "hello-world", "azure", sleep=lambda _: None) == 130
    assert "r1" in capsys.readouterr().out


def test_run_once_explains_when_the_job_is_not_deployed(capsys):
    job = RemoteJob(
        lambda: (_ for _ in ()).throw(NotDeployed),
        lambda run: run,
        lambda run, cursor: LogPage([], cursor),
    )
    assert run_once(job, True, "hello-world", "azure") == 1
    out = capsys.readouterr().out
    assert "not deployed to azure" in out
    assert "pdt deploy hello-world" in out


def test_remote_run_dispatches(project, monkeypatch):
    add_app(project, "hello-world")
    calls = []
    monkeypatch.setattr(deploy, "dispatch", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr("sys.argv", ["pdt", "run", "hello-world", "--remote"])
    assert cli.main() == 0
    assert calls == [("azure", "run", "hello-world", False, [])]


def test_remote_run_without_wait_dispatches_the_flag(project, monkeypatch):
    add_app(project, "hello-world")
    calls = []
    monkeypatch.setattr(deploy, "dispatch", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr("sys.argv", ["pdt", "run", "hello-world", "--remote", "--no-wait"])
    assert cli.main() == 0
    assert calls == [("azure", "run", "hello-world", False, ["--no-wait"])]


def test_no_wait_requires_remote(project, monkeypatch, capsys):
    add_app(project, "hello-world")
    monkeypatch.setattr("sys.argv", ["pdt", "run", "hello-world", "--no-wait"])
    assert cli.main() == 1
    assert "--no-wait only applies with --remote" in capsys.readouterr().out


def test_every_provider_script_accepts_run():
    providers = ["deploy_aws.py", "deploy_azure.py", "deploy_google_cloud.py", "deploy_windows.py"]
    root = __import__("pathlib").Path(cli.__file__).parent
    for provider in providers:
        assert '"run"' in (root / provider).read_text()
