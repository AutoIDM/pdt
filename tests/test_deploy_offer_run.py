"""After an interactive deploy, pdt offers to start a run of the app and follow its log."""

import pytest

from pdt import cli, config, console, deploy, scaffold

APP = scaffold.STARTER
REGIONS = {"aws": "us-east-1", "azure": "eastus", "google-cloud": "us-central1", "windows": None}


@pytest.fixture(params=list(REGIONS))
def project(request, tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    region = REGIONS[request.param]
    (tmp_path / "pdt.yml").write_text(f"platform:\n  provider: {request.param}\n"
                                      + (f"  region: {region}\n" if region else ""))
    return tmp_path


class Cloud:
    """A provider that records each command and never lists runs."""

    def __init__(self, monkeypatch, terminal=True, answer="y", deploy_code=0, start_code=0):
        self.calls = []
        self.asked = []
        self.codes = {"deploy": deploy_code, "start": start_code}
        monkeypatch.setattr(deploy, "dispatch", self.dispatch)
        monkeypatch.setattr(deploy, "dispatch_output", self.dispatch_output)
        monkeypatch.setattr(deploy, "can_prompt", lambda interactive: terminal)
        monkeypatch.setattr(console, "confirm", self.confirm)
        self.answer = answer

    def dispatch(self, provider, command, app_name, assume_yes, extra=None, rebuild=False,
                 platform=None):
        self.calls.append((command, *(extra or [])))
        return self.codes.get(command, 0)

    def dispatch_output(self, provider, command, app_name, extra):
        pytest.fail(f"the deploy read {command}")

    def confirm(self, question="Proceed?"):
        self.asked.append(question)
        if self.answer is None:
            raise EOFError
        return self.answer == "y"

    def commands(self):
        return [call[0] for call in self.calls]


FOLLOW = ("start", "--", "--follow")
QUESTION = "Start a run now and show its log?"


def test_a_yes_at_the_terminal_starts_the_run_and_follows_it(project, monkeypatch, capsys):
    cloud = Cloud(monkeypatch)
    assert deploy.deploy(APP) == 0
    assert cloud.asked == [QUESTION]
    assert cloud.calls == [("deploy",), FOLLOW]
    out = capsys.readouterr().out
    assert "Reading the runs" not in out
    assert f"pdt run {APP} --deployed" not in out
    assert f"pdt logs {APP}" in out
    assert "see the run" not in out


def test_every_deploy_asks_again(project, monkeypatch):
    cloud = Cloud(monkeypatch)
    assert deploy.deploy(APP) == 0
    assert deploy.deploy(APP) == 0
    assert cloud.asked == [QUESTION, QUESTION]
    assert cloud.calls == [("deploy",), FOLLOW, ("deploy",), FOLLOW]


def test_a_no_at_the_terminal_starts_nothing(project, monkeypatch, capsys):
    cloud = Cloud(monkeypatch, answer="n")
    assert deploy.deploy(APP) == 0
    assert cloud.asked == [QUESTION]
    assert cloud.commands() == ["deploy"]
    assert f"pdt run {APP} --deployed --follow" in capsys.readouterr().out


def test_no_answer_at_the_terminal_starts_nothing(project, monkeypatch):
    cloud = Cloud(monkeypatch, answer=None)
    assert deploy.deploy(APP) == 0
    assert cloud.commands() == ["deploy"]


@pytest.mark.parametrize("assume_yes, terminal", [(True, True), (True, False), (False, False)])
def test_yes_or_no_terminal_starts_nothing(project, monkeypatch, capsys, assume_yes, terminal):
    cloud = Cloud(monkeypatch, terminal=terminal)
    assert deploy.deploy(APP, assume_yes=assume_yes) == 0
    assert cloud.asked == []
    assert cloud.commands() == ["deploy"]
    assert f"pdt run {APP} --deployed --follow" in capsys.readouterr().out


@pytest.mark.parametrize("assume_yes, terminal", [(False, True), (True, True), (True, False)])
def test_run_starts_the_run_with_no_question(project, monkeypatch, assume_yes, terminal):
    cloud = Cloud(monkeypatch, terminal=terminal)
    assert deploy.deploy(APP, assume_yes=assume_yes, run=True) == 0
    assert cloud.asked == []
    assert cloud.calls == [("deploy",), FOLLOW]


def test_deploy_all_never_offers(project, monkeypatch):
    cloud = Cloud(monkeypatch)
    assert deploy.deploy(APP, run=False) == 0
    assert cloud.commands() == ["deploy"]


def test_a_failed_deploy_offers_nothing(project, monkeypatch):
    cloud = Cloud(monkeypatch, deploy_code=1)
    assert deploy.deploy(APP, run=True) == 1
    assert cloud.commands() == ["deploy"]


def test_the_exit_code_is_the_runs(project, monkeypatch, capsys):
    cloud = Cloud(monkeypatch, start_code=1)
    assert deploy.deploy(APP) == 1
    assert cloud.calls[-1] == FOLLOW


@pytest.mark.parametrize("start_code, hint", [(0, True), (1, False)])
def test_pdt_run_deployed_ends_with_the_commands_to_follow_the_run(project, monkeypatch, capsys,
                                                                   start_code, hint):
    monkeypatch.setattr(console._console, "width", 80)
    cloud = Cloud(monkeypatch)
    cloud.codes["start"] = start_code
    config.mark_deployed(APP, True)
    assert deploy.start(APP) == start_code
    assert cloud.commands() == ["start"]
    out = capsys.readouterr().out.splitlines()
    steps = [
        "Next steps:",
        f"  pdt logs {APP} --follow  print the log lines as they arrive",
        f"  pdt runs {APP}           list the recent runs",
        f"  pdt health {APP}         show whether the last run succeeded",
    ]
    assert (out[-4:] == steps) == hint


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


@pytest.mark.parametrize("argv, run_option", [((), None), (("--run",), True)])
def test_the_run_option_reaches_the_deploy(project, monkeypatch, argv, run_option):
    calls = []
    monkeypatch.setattr(deploy, "deploy",
                        lambda name, assume_yes=False, run=None, rebuild=False, plan_only=False:
                        calls.append(run) or 0)
    assert run_cli(monkeypatch, "deploy", APP, *argv) == 0
    assert calls == [run_option]


def test_run_with_all_deploys_then_starts_a_run_with_no_question(project, monkeypatch):
    cloud = Cloud(monkeypatch)
    monkeypatch.setattr(cli, "can_prompt", lambda interactive: True)
    assert run_cli(monkeypatch, "deploy", "--all", "--run") == 0
    assert cloud.asked == []
    assert cloud.commands() == ["deploy", "start"]


def test_deploy_all_at_a_terminal_asks_nothing_and_starts_no_run(project, monkeypatch, capsys):
    cloud = Cloud(monkeypatch)
    monkeypatch.setattr(cli, "can_prompt", lambda interactive: True)
    assert run_cli(monkeypatch, "deploy", "--all") == 0
    assert cloud.asked == []
    assert cloud.commands() == ["deploy"]
    assert f"pdt run {APP} --deployed --follow" in capsys.readouterr().out
