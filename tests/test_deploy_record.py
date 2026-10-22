""".pdt/state records where each app is deployed, so no deploy or destroy leaves a copy behind."""

import json

import pytest

from pdt import alerts_cli, config, console, deploy, scaffold

APP = scaffold.STARTER
AWS = "platform:\n  provider: aws\n  region: us-east-1\n  account: \"123456789012\"\n"
AZURE = "platform:\n  provider: azure\n  region: eastus\n  subscription: sub-1\n"
GOOGLE = "platform:\n  provider: google-cloud\n  region: us-central1\n  project: my-project\n"
WINDOWS = "platform:\n  provider: windows\n"
MOVES = {
    "region": (AWS, AWS.replace("us-east-1", "eu-west-1")),
    "account": (AWS, AWS.replace("123456789012", "210987654321")),
    "project": (GOOGLE, GOOGLE.replace("my-project", "other-project")),
    "aws-to-azure": (AWS, AZURE),
    "azure-to-windows": (AZURE, WINDOWS),
    "windows-to-google-cloud": (WINDOWS, GOOGLE),
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.delenv(config.DEPLOYED_PLATFORM_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    monkeypatch.setattr(alerts_cli, "offer", lambda app_name, assume_yes: None)
    monkeypatch.setattr(deploy, "deployed_next_steps", lambda *args, **kwargs: None)
    return tmp_path


class Cloud:
    """A provider that records each command, and a person who gives `answers` in order."""

    def __init__(self, monkeypatch, answers=(), terminal=True):
        self.calls = []
        self.answers = list(answers)
        self.asked = []
        monkeypatch.setattr(deploy, "dispatch", self.dispatch)
        monkeypatch.setattr(deploy, "can_prompt", lambda interactive: terminal)
        monkeypatch.setattr(console, "ask", self.ask)

    def dispatch(self, provider, command, app_name, assume_yes, extra=None, rebuild=False,
                 platform=None):
        self.calls.append((provider, command, platform))
        return 0

    def ask(self, question, default=""):
        self.asked.append(question)
        return self.answers.pop(0)


def deployed_to(project, text, app=APP):
    """Write pdt.yml and record the app as deployed with it."""
    (project / "pdt.yml").write_text(text)
    platform = config.merged_app(app)["platform"]
    config.mark_deployed(app, platform)
    return platform


@pytest.mark.parametrize("text", [AWS, AZURE, GOOGLE, WINDOWS])
def test_a_deploy_records_where_the_app_runs_and_a_destroy_forgets_it(project, monkeypatch, text):
    (project / "pdt.yml").write_text(text)
    Cloud(monkeypatch)
    assert deploy.deploy(APP, run=False) == 0
    assert config.is_deployed(APP)
    assert config.deployed_places() == {APP: config.place(config.merged_app(APP)["platform"])}
    assert config.deployed_places()[APP]["provider"] == config.merged_app(APP)["platform"]["provider"]
    assert deploy.destroy(APP) == 0
    assert not config.is_deployed(APP)
    assert config.deployed_places() == {}


def test_the_record_keeps_the_keys_a_deploy_saved(project, monkeypatch):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  region: us-east-1\n")
    cloud = Cloud(monkeypatch)

    def dispatch(*args, **kwargs):
        config.save_platform_key(config.merged_app(APP), "account", "123456789012")
        return cloud.dispatch(*args, **kwargs)

    monkeypatch.setattr(deploy, "dispatch", dispatch)
    assert deploy.deploy(APP, run=False) == 0
    assert config.deployed_places()[APP] == {"provider": "aws", "region": "us-east-1",
                                             "account": "123456789012"}


@pytest.mark.parametrize("move", MOVES)
def test_cancel_is_the_default_and_changes_nothing(project, monkeypatch, capsys, move):
    old, new = MOVES[move]
    recorded = deployed_to(project, old)
    (project / "pdt.yml").write_text(new)
    cloud = Cloud(monkeypatch, answers=[""])
    assert deploy.deploy(APP, run=False) == deploy.DECLINED
    assert cloud.calls == []
    assert cloud.asked == ["Which one?"]
    assert config.deployed_platform(APP) == recorded
    out = capsys.readouterr().out
    assert "What do you want to do with the app currently deployed to" in out
    assert "1) Remove it from " in out and ", then deploy it to " in out
    assert "Cancel and change nothing" in out


@pytest.mark.parametrize("move", MOVES)
def test_remove_first_destroys_at_the_recorded_place_then_deploys(project, monkeypatch, move):
    old, new = MOVES[move]
    recorded = deployed_to(project, old)
    (project / "pdt.yml").write_text(new)
    cloud = Cloud(monkeypatch, answers=["3", "1"])
    assert deploy.deploy(APP, run=False) == 0
    current = config.merged_app(APP)["platform"]
    assert cloud.calls == [(recorded["provider"], "destroy", recorded),
                           (current["provider"], "deploy", None)]
    assert config.deployed_platform(APP) == current


@pytest.mark.parametrize("yes,terminal", [(True, True), (False, False)])
def test_with_yes_or_no_one_to_ask_it_stops_and_destroys_nothing(project, monkeypatch, capsys,
                                                                  yes, terminal):
    recorded = deployed_to(project, AWS)
    (project / "pdt.yml").write_text(AZURE)
    cloud = Cloud(monkeypatch, terminal=terminal)
    assert deploy.deploy(APP, assume_yes=yes, run=False) == 1
    assert cloud.calls == [] and cloud.asked == []
    assert config.deployed_platform(APP) == recorded
    out = capsys.readouterr().out
    assert "now deploys it to" in out and "was not deployed" in out
    assert f"\n  pdt deploy {APP}\n" in out


@pytest.mark.parametrize("new", [
    AWS.replace("  account: \"123456789012\"\n", ""),
    AWS + "  profile: other-profile\n  timezone: Europe/Paris\n",
])
def test_an_unset_account_or_a_change_that_is_not_a_place_asks_nothing(project, monkeypatch,
                                                                       new):
    deployed_to(project, AWS)
    (project / "pdt.yml").write_text(new)
    cloud = Cloud(monkeypatch)
    assert deploy.deploy(APP, run=False) == 0
    assert cloud.asked == []
    assert [command for _, command, _ in cloud.calls] == ["deploy"]


def test_an_app_deployed_by_an_older_pdt_gets_no_question_and_its_next_deploy_records_it(
        project, monkeypatch):
    (project / "pdt.yml").write_text(AZURE)
    config.write_state({"deployed": [APP]})
    cloud = Cloud(monkeypatch)
    assert deploy.deploy(APP, run=False) == 0
    assert cloud.asked == []
    assert config.deployed_places()[APP]["provider"] == "azure"


@pytest.mark.parametrize("move", MOVES)
def test_destroy_goes_where_the_record_says(project, monkeypatch, capsys, move):
    old, new = MOVES[move]
    recorded = deployed_to(project, old)
    (project / "pdt.yml").write_text(new)
    cloud = Cloud(monkeypatch)
    assert deploy.destroy(APP) == 0
    assert cloud.calls == [(recorded["provider"], "destroy", recorded)]
    assert "pdt destroys it where it is deployed" in capsys.readouterr().out
    assert not config.is_deployed(APP)


def test_destroy_at_the_same_place_says_nothing_extra(project, monkeypatch, capsys):
    recorded = deployed_to(project, AWS)
    cloud = Cloud(monkeypatch)
    assert deploy.destroy(APP) == 0
    assert cloud.calls == [("aws", "destroy", recorded)]
    assert "pdt destroys it where it is deployed" not in capsys.readouterr().out


def test_an_app_config_yml_that_names_another_provider_is_a_move(project, monkeypatch):
    recorded = deployed_to(project, AWS)
    (project / APP / config.APP_FILE).write_text(
        "schedule: daily\nplatform:\n  provider: google-cloud\n  region: europe-west1\n"
        "  project: my-project\n")
    cloud = Cloud(monkeypatch, answers=["1"])
    assert deploy.deploy(APP, run=False) == 0
    assert cloud.calls == [("aws", "destroy", recorded), ("google-cloud", "deploy", None)]
    assert config.deployed_places()[APP]["provider"] == "google-cloud"


def test_a_powershell_app_is_checked_and_destroyed_the_same_way(project, monkeypatch, capsys):
    (project / "report").mkdir()
    (project / "report" / "run.ps1").write_text("Write-Output 'hi'\n")
    recorded = deployed_to(project, WINDOWS, "report")
    (project / "pdt.yml").write_text(AWS)
    cloud = Cloud(monkeypatch, answers=[""])
    assert deploy.leave_old_place("report", False) == deploy.DECLINED
    assert deploy.destroy("report") == 0
    assert cloud.calls == [("windows", "destroy", recorded)]


def test_the_recorded_platform_replaces_the_config_only_for_the_app_it_names(project,
                                                                             monkeypatch):
    (project / "pdt.yml").write_text(AWS)
    monkeypatch.setenv(config.DEPLOYED_PLATFORM_ENV,
                       json.dumps({"app": APP, "platform": {"provider": "windows"}}))
    assert config.merged_app(APP)["platform"] == {"provider": "windows"}
    (project / "other").mkdir()
    (project / "other" / "run.py").write_text("")
    assert config.merged_app("other")["platform"]["provider"] == "aws"
