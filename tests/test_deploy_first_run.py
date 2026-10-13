"""After a deploy, pdt offers to start the first run of an app with no successful run yet."""

import json
from datetime import UTC, datetime

import pytest

from pdt import cli, config, console, deploy, runs_cli, scaffold

APP = scaffold.STARTER


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: aws\n  region: us-east-1\n")
    return tmp_path


def run(run_id, status):
    return runs_cli.Run(run_id, datetime(2026, 10, 9, tzinfo=UTC), None, status)


def runs_json(found):
    return json.dumps([runs_cli.run_json(item) for item in found])


class Cloud:
    """A provider that records each command and lists `history`."""

    def __init__(self, monkeypatch, history, terminal=True, answer="y",
                 deploy_code=0, start_code=0, runs_code=0):
        self.calls = []
        self.history = history
        self.asked = []
        self.codes = {"deploy": deploy_code, "start": start_code}
        self.runs_code = runs_code
        monkeypatch.setattr(deploy, "dispatch", self.dispatch)
        monkeypatch.setattr(deploy, "dispatch_output", self.dispatch_output)
        monkeypatch.setattr(deploy, "can_prompt", lambda interactive: terminal)
        monkeypatch.setattr(console, "confirm", self.confirm)
        self.answer = answer

    def dispatch(self, provider, command, app_name, assume_yes, extra=None):
        self.calls.append((command, *(extra or [])))
        return self.codes.get(command, 0)

    def dispatch_output(self, provider, command, app_name, extra):
        assert (command, extra) == ("runs", ["--", "--json"])
        self.calls.append(("runs",))
        return self.runs_code, runs_json(self.history)

    def confirm(self, question="Proceed?"):
        self.asked.append(question)
        if self.answer is None:
            raise EOFError
        return self.answer == "y"

    def commands(self):
        return [call[0] for call in self.calls]


FOLLOW = ("start", "--", "--follow")
QUESTION = "Start a run now and show its log?"


@pytest.mark.parametrize("history, said", [
    ([], "hello-world has not run yet."),
    ([run("old-2", "failed"), run("old-1", "failed")], "hello-world has no successful run yet."),
])
def test_a_yes_at_the_terminal_starts_the_run_and_follows_it(project, monkeypatch, capsys,
                                                              history, said):
    cloud = Cloud(monkeypatch, history)
    assert deploy.deploy(APP) == 0
    assert cloud.asked == [QUESTION]
    assert cloud.calls == [("deploy",), ("runs",), FOLLOW]
    out = capsys.readouterr().out
    assert said in out
    assert f"pdt run {APP} --deployed" not in out
    assert f"pdt logs {APP}" in out
    assert "see the run" not in out


def test_a_no_at_the_terminal_starts_nothing(project, monkeypatch, capsys):
    cloud = Cloud(monkeypatch, [], answer="n")
    assert deploy.deploy(APP) == 0
    assert cloud.asked == [QUESTION]
    assert cloud.commands() == ["deploy", "runs"]
    assert f"pdt run {APP} --deployed" in capsys.readouterr().out


def test_no_answer_at_the_terminal_starts_nothing(project, monkeypatch):
    cloud = Cloud(monkeypatch, [], answer=None)
    assert deploy.deploy(APP) == 0
    assert cloud.commands() == ["deploy", "runs"]


@pytest.mark.parametrize("assume_yes, terminal", [(True, True), (True, False), (False, False)])
def test_yes_or_no_terminal_starts_nothing_and_reads_no_runs(project, monkeypatch, capsys,
                                                             assume_yes, terminal):
    cloud = Cloud(monkeypatch, [], terminal=terminal)
    assert deploy.deploy(APP, assume_yes=assume_yes) == 0
    assert cloud.asked == []
    assert cloud.commands() == ["deploy"]
    assert f"pdt run {APP} --deployed" in capsys.readouterr().out


@pytest.mark.parametrize("assume_yes, terminal", [(False, True), (True, True), (True, False)])
def test_run_starts_the_run_with_no_question(project, monkeypatch, assume_yes, terminal):
    cloud = Cloud(monkeypatch, [run("old", "failed")], terminal=terminal)
    assert deploy.deploy(APP, assume_yes=assume_yes, run=True) == 0
    assert cloud.asked == []
    assert cloud.calls == [("deploy",), ("runs",), FOLLOW]


@pytest.mark.parametrize("run_option", [None, True])
def test_an_app_with_a_successful_run_gets_no_offer(project, monkeypatch, capsys, run_option):
    cloud = Cloud(monkeypatch, [run("old-2", "failed"), run("old-1", "succeeded")])
    assert deploy.deploy(APP, run=run_option) == 0
    assert cloud.asked == []
    assert cloud.commands() == ["deploy", "runs"]
    assert f"pdt run {APP} --deployed" in capsys.readouterr().out


def test_runs_that_cannot_be_read_get_no_offer(project, monkeypatch):
    cloud = Cloud(monkeypatch, [], runs_code=1)
    assert deploy.deploy(APP, run=True) == 0
    assert cloud.commands() == ["deploy", "runs"]


def test_deploy_all_never_offers(project, monkeypatch):
    cloud = Cloud(monkeypatch, [])
    assert deploy.deploy(APP, run=False) == 0
    assert cloud.commands() == ["deploy"]


def test_a_failed_deploy_offers_nothing(project, monkeypatch):
    cloud = Cloud(monkeypatch, [], deploy_code=1)
    assert deploy.deploy(APP, run=True) == 1
    assert cloud.commands() == ["deploy"]


def test_the_exit_code_is_the_runs(project, monkeypatch, capsys):
    cloud = Cloud(monkeypatch, [], start_code=1)
    assert deploy.deploy(APP) == 1
    assert cloud.calls[-1] == FOLLOW


@pytest.mark.parametrize("start_code, hint", [(0, True), (1, False)])
def test_pdt_run_deployed_ends_with_the_command_to_see_the_run(project, monkeypatch, capsys,
                                                               start_code, hint):
    cloud = Cloud(monkeypatch, [])
    cloud.codes["start"] = start_code
    config.mark_deployed(APP, True)
    assert deploy.start(APP) == start_code
    assert cloud.commands() == ["start"]
    assert (f"pdt runs {APP}" in capsys.readouterr().out) == hint


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


@pytest.mark.parametrize("argv, run_option", [((), None), (("--run",), True)])
def test_the_run_option_reaches_the_deploy(project, monkeypatch, argv, run_option):
    calls = []
    monkeypatch.setattr(deploy, "deploy",
                        lambda name, assume_yes=False, run=None: calls.append(run) or 0)
    assert run_cli(monkeypatch, "deploy", APP, *argv) == 0
    assert calls == [run_option]


def test_run_with_all_is_refused(project, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "deploy", lambda *a, **k: pytest.fail("deployed"))
    assert run_cli(monkeypatch, "deploy", "--all", "--run") == 1
    assert "--run starts the first run of one app; drop --all" in capsys.readouterr().out
