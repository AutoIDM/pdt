import os

import pytest
import yaml

from conftest import add_app
from pdt import move, scaffold
from pdt.config import APP_FILE, PROJECT_FILE, ConfigError


def test_replace_platform_keeps_every_other_line():
    text = "# mine\nplatform:\n  # which cloud\n  provider: azure\n  region: eastus2\n\napps: []\n"
    out = move.replace_platform(text, {"provider": "aws", "region": "us-east-1"})
    assert out.startswith("# mine\n")
    assert yaml.safe_load(out) == {"platform": {"provider": "aws", "region": "us-east-1"},
                                   "apps": []}


@pytest.mark.parametrize("text", [
    "platform:\n  provider: azure\n# a comment at column zero\n  subscription: s\n\n# apps\napps: []\n",
    "#platform:\n#  provider: azure\nplatform:\n  provider: azure\n  subscription: s\napps: []\n",
    '"platform":\n  provider: azure\n  subscription: s\napps: []\n',
    "platform: {provider: azure, subscription: s}\napps: []\n",
])
def test_replace_platform_replaces_the_whole_active_block(text):
    out = move.replace_platform(text, {"provider": "aws"})
    assert yaml.safe_load(out) == {"platform": {"provider": "aws"}, "apps": []}
    assert "subscription" not in out


def test_replace_platform_keeps_the_comment_above_the_next_key():
    out = move.replace_platform("platform:\n  provider: azure\n\n# apps\napps: []\n",
                                    {"provider": "aws"})
    assert "# apps\napps: []" in out


def test_replace_platform_fills_in_the_commented_block():
    out = move.replace_platform(scaffold.project_yaml({}), {"provider": "aws"})
    assert yaml.safe_load(out)["platform"] == {"provider": "aws"}
    assert "#  region" not in out


def test_replace_platform_adds_a_block_when_there_is_none():
    out = move.replace_platform("apps: []\n", {"provider": "windows"})
    assert yaml.safe_load(out) == {"apps": [], "platform": {"provider": "windows"}}


@pytest.mark.parametrize("current,provider,kept", [
    ({"provider": "aws", "region": "us-east-1", "timezone": "America/Chicago"}, "aws",
     {"provider": "aws", "region": "us-east-1", "timezone": "America/Chicago"}),
    ({"provider": "aws", "account": "1", "timezone": "America/Chicago"}, "google-cloud",
     {"timezone": "America/Chicago"}),
    ({"provider": "aws", "timezone": "America/Chicago"}, "azure", {}),
    ({"provider": "aws", "timezone": "America/Chicago"}, "windows", {}),
    ({"provider": "windows", "timezone": "local"}, "aws", {}),
])
def test_a_move_keeps_only_the_settings_the_new_provider_reads(current, provider, kept):
    assert move.kept_settings(current, provider) == kept


def answer(monkeypatch, replies, confirms=()):
    replies, confirms = iter(replies), iter(confirms)
    monkeypatch.setattr(move.console, "ask", lambda question, default="": next(replies))
    monkeypatch.setattr(move.console, "confirm", lambda *a, **k: next(confirms))
    monkeypatch.setattr(move.deploy, "can_prompt", lambda interactive: True)


def deployed_app(project, name, config_text=""):
    add_app(project, name, config_text)
    from pdt import config
    config.mark_deployed(name, move.deploy.resolved(config.merged_app(name)["platform"]))


class Cloud:
    def __init__(self, monkeypatch, failing=()):
        self.destroyed, self.deployed = [], []
        self.failing = failing
        monkeypatch.setattr(move.deploy, "dispatch", self.dispatch)

    def dispatch(self, provider, command, app_name, assume_yes, extra=None, rebuild=False,
                 platform=None):
        if app_name in self.failing:
            return 1
        calls = self.deployed if command == "deploy" else self.destroyed
        calls.append((app_name, command, provider, assume_yes))
        return 0


def moving_project(project, names=("alpha", "beta", "gamma")):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    for name in names:
        deployed_app(project, name)
    return (project / PROJECT_FILE).read_text()


def platform_of(project, name):
    from pdt import config
    return config.load_yaml(project / name / APP_FILE).get("platform")


AZURE_PIN = {"provider": "azure", "region": "eastus2", "subscription": "sub-1",
             "resource_group": "pdt"}


def test_move_all_destroys_each_app_then_saves_pdt_yml(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "1"], [True])
    assert scaffold.init(None, assume_yes=False) == 0
    out = capsys.readouterr().out
    assert cloud.destroyed == [(name, "destroy", "azure", True) for name in ("alpha", "beta", "gamma")]
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"]["provider"] == "aws"
    assert platform_of(project, "alpha") is None
    assert "1  alpha  azure, region eastus2, subscription sub-1, resource group pdt" in out
    assert "What should happen to the apps currently deployed to azure, region eastus2, subscription sub-1, resource group pdt?" in out
    assert "3) Keep them on azure, region eastus2, subscription sub-1, resource group pdt (only new apps go to aws, region us-east-2)" in out
    assert "destroy alpha on azure, region eastus2, subscription sub-1, resource group pdt" in out
    assert "update pdt.yml" in out
    assert "deploy alpha to aws, region us-east-2" in out
    assert cloud.deployed == [(name, "deploy", "aws", True) for name in ("alpha", "beta", "gamma")]


def test_move_some_destroys_the_chosen_apps_and_pins_the_rest(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "2", "2"], [True])
    assert scaffold.init(None, assume_yes=False) == 0
    out = capsys.readouterr().out
    assert [name for name, *_ in cloud.destroyed] == ["beta"]
    assert platform_of(project, "alpha") == AZURE_PIN
    assert platform_of(project, "gamma") == AZURE_PIN
    assert platform_of(project, "beta") is None
    assert "update alpha/config.yml: alpha stays on azure, region eastus2, subscription sub-1, resource group pdt" in out
    assert [name for name, *_ in cloud.deployed] == ["beta"]


def test_keep_pins_every_deployed_app_and_destroys_nothing(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "3"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert cloud.destroyed == []
    from pdt import config
    for name in ("alpha", "beta", "gamma"):
        assert platform_of(project, name) == AZURE_PIN
        assert config.merged_app(name)["platform"]["provider"] == "azure"
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"]["provider"] == "aws"
    assert "alpha stays on azure, region eastus2, subscription sub-1, resource group pdt." in capsys.readouterr().out


def test_move_all_destroys_a_disabled_app_that_is_deployed(project, monkeypatch):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    deployed_app(project, "alpha", "enabled: false\n")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "1"], [True])
    assert scaffold.init(None, assume_yes=False) == 0
    assert [name for name, *_ in cloud.destroyed] == ["alpha"]


def test_keep_pins_the_region_an_environment_variable_selects(project, monkeypatch, capsys):
    monkeypatch.setenv("PDT_AZURE_REGION", "eastus2")
    (project / PROJECT_FILE).write_text("platform:\n  provider: azure\n  subscription: sub-1\n")
    deployed_app(project, "alpha")
    answer(monkeypatch, ["2", "us-east-2", "UTC", "3"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert platform_of(project, "alpha") == AZURE_PIN
    assert "deployed to azure, region eastus2, subscription sub-1, resource group pdt?" in capsys.readouterr().out


@pytest.mark.parametrize("replies,confirms", [
    (["4"], []),
    (["2", ""], []),
    (["1"], [False]),
])
def test_every_cancel_changes_nothing(project, monkeypatch, replies, confirms):
    before = moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", *replies], confirms)
    assert scaffold.init(None, assume_yes=False) == 0
    assert cloud.destroyed == []
    assert (project / PROJECT_FILE).read_text() == before
    assert not (project / "alpha" / APP_FILE).exists()


def test_a_failed_destroy_pins_each_app_still_deployed(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch, failing=("beta",))
    answer(monkeypatch, ["2", "us-east-2", "UTC", "1"], [True])
    assert scaffold.init(None, assume_yes=False) == 1
    out = capsys.readouterr().out
    assert [name for name, *_ in cloud.destroyed] == ["alpha"]
    assert platform_of(project, "beta") == AZURE_PIN
    assert platform_of(project, "gamma") == AZURE_PIN
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"]["provider"] == "aws"
    assert "beta was not removed. It is still deployed to azure, region eastus2, subscription sub-1, resource group pdt." in out
    assert [name for name, *_ in cloud.deployed] == ["alpha"]


def test_init_yes_in_a_project_moves_nothing(project, monkeypatch):
    before = moving_project(project)
    cloud = Cloud(monkeypatch)
    assert scaffold.init(None, assume_yes=True) == 0
    assert cloud.destroyed == []
    assert (project / PROJECT_FILE).read_text() == before


def test_init_again_moves_the_project_to_another_provider(project, monkeypatch):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n  account: \"1\"\n"
        "  timezone: America/Chicago\n\nalerts: []\n")
    answer(monkeypatch, ["3", "us-east4", ""])
    assert scaffold.init(None, assume_yes=False) == 0
    assert yaml.safe_load((project / PROJECT_FILE).read_text()) == {
        "platform": {"provider": "google-cloud", "timezone": "America/Chicago",
                     "region": "us-east4"},
        "alerts": []}


def test_a_region_change_asks_too(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text("platform:\n  provider: azure\n  region: eastus2\n")
    deployed_app(project, "report")
    answer(monkeypatch, ["1", "westeurope", "4"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert "apps currently deployed to azure, region eastus2, resource group pdt?" in capsys.readouterr().out


def test_an_app_with_its_own_provider_is_listed_without_a_number(project, monkeypatch, capsys):
    deployed_app(project, "own", "platform:\n  provider: google-cloud\n  region: us-east4\n")
    answer(monkeypatch, ["2", "us-east-2", ""])
    assert scaffold.init(None, assume_yes=False) == 0
    out = capsys.readouterr().out
    assert "\n  own  google-cloud, region us-east4; set in own/config.yml\n" in out
    assert "To move an app that sets its own place, run:\n  pdt move own\n" in out
    assert "What should happen" not in out
    assert platform_of(project, "own") == {"provider": "google-cloud", "region": "us-east4"}


def test_no_answer_at_the_move_question_changes_nothing(project, monkeypatch):
    deployed_app(project, "report")
    before = (project / PROJECT_FILE).read_text()
    replies = iter(["2", "us-east-2", ""])

    def ask(question, default=""):
        reply = next(replies, None)
        if reply is None:
            raise EOFError
        return reply

    monkeypatch.setattr(move.console, "ask", ask)
    with pytest.raises(ConfigError, match="no one to answer"):
        scaffold.init(None, assume_yes=False)
    assert (project / PROJECT_FILE).read_text() == before


def test_init_again_reads_this_folder_when_pdt_project_names_another(project, monkeypatch,
                                                                       capsys, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    (other / PROJECT_FILE).write_text("platform:\n  provider: azure\n")
    deployed_app(project, "report")
    monkeypatch.setenv("PDT_PROJECT", str(other))
    answer(monkeypatch, ["2", "us-east-2", "", "4"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert "1  report  azure" in capsys.readouterr().out
    assert yaml.safe_load((other / PROJECT_FILE).read_text())["platform"]["provider"] == "azure"


@pytest.mark.parametrize("text", [
    "{platform: {provider: azure}, apps: []}\n",
    "platform: &defaults\n  provider: azure\napps:\n- name: report\n  platform: *defaults\n",
])
def test_replace_platform_refuses_a_form_it_cannot_change_safely(text):
    with pytest.raises(ConfigError, match="by hand"):
        move.replace_platform(text, {"provider": "aws"})


def test_a_move_keeps_the_byte_order_mark_and_the_line_endings(project, monkeypatch):
    (project / PROJECT_FILE).write_bytes(
        "﻿# mine\r\nplatform:\r\n  provider: azure\r\n\r\napps: []\r\n".encode())
    answer(monkeypatch, ["2", "us-east-2", "UTC"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert (project / PROJECT_FILE).read_bytes().decode() == (
        "﻿# mine\r\nplatform:\r\n  provider: \"aws\"\r\n  region: \"us-east-2\"\r\n"
        "  timezone: \"UTC\"\r\n\r\napps: []\r\n")


def test_ctrl_c_at_the_remove_question_changes_nothing(project, monkeypatch):
    deployed_app(project, "report")
    before = (project / PROJECT_FILE).read_text()
    answer(monkeypatch, ["2", "us-east-2", "", "1"])
    monkeypatch.setattr(move.console, "confirm",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        scaffold.init(None, assume_yes=False)
    assert (project / PROJECT_FILE).read_text() == before


def test_init_again_puts_pdt_project_back(project, monkeypatch):
    answer(monkeypatch, ["5"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert "PDT_PROJECT" not in os.environ


@pytest.mark.parametrize("text", [
    "defaults: &defaults\n  provider: azure\nplatform: *defaults\napps: []\n",
    "zone: &zone UTC\nplatform:\n  provider: aws\n  timezone: *zone\napps: []\n",
    "platform:\n  provider: azure\napps: []\n...\n",
])
def test_replace_platform_refuses_an_alias_or_a_document_end(text):
    with pytest.raises(ConfigError, match="by hand"):
        move.replace_platform(text, {"provider": "aws"})


def test_replace_platform_leaves_out_an_empty_setting():
    out = move.replace_platform("platform:\n  provider: azure\n  environment:\n",
                                    {"provider": "azure", "environment": None, "region": "eastus2"})
    assert yaml.safe_load(out) == {"platform": {"provider": "azure", "region": "eastus2"}}


def test_a_change_to_pdt_yml_during_the_questions_stops_the_move(project, monkeypatch):
    replies = iter(["2", "us-east-2"])

    def ask(question, default=""):
        reply = next(replies, None)
        if reply is None:
            (project / PROJECT_FILE).write_text("platform:\n  provider: google-cloud\n")
            return ""
        return reply

    monkeypatch.setattr(move.console, "ask", ask)
    with pytest.raises(ConfigError, match="changed while pdt init was asking"):
        scaffold.init(None, assume_yes=False)
    assert (project / PROJECT_FILE).read_text() == "platform:\n  provider: google-cloud\n"


def test_keep_pins_every_setting_an_app_with_its_own_provider_inherits(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n  account: \"1\"\n  profile: work\n"
        "  timezone: America/Chicago\n")
    deployed_app(project, "own", "platform:\n  provider: aws\n  region: us-west-2\n")
    answer(monkeypatch, ["1", "eastus2"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert "What should happen" not in capsys.readouterr().out
    assert platform_of(project, "own") == {"provider": "aws", "region": "us-west-2", "account": "1",
                                           "profile": "work", "timezone": "America/Chicago"}


@pytest.mark.parametrize("text", ["{}\n", "platform: {region: westus2}\n"])
def test_a_config_yml_that_pdt_cannot_pin_stops_init_before_any_change(project, monkeypatch, text):
    before = moving_project(project, names=("alpha",))
    (project / "alpha" / APP_FILE).write_text(text)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "3"])
    with pytest.raises(ConfigError, match="alpha/config.yml"):
        scaffold.init(None, assume_yes=False)
    assert cloud.destroyed == []
    assert (project / PROJECT_FILE).read_text() == before
    assert (project / "alpha" / APP_FILE).read_text() == text


def test_an_app_that_sets_only_its_region_gets_a_number(project, monkeypatch, capsys):
    deployed_app(project, "report", "platform:\n  region: westus2\n")
    answer(monkeypatch, ["2", "us-east-2", "UTC", "4"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert "1  report  azure, region westus2, resource group pdt\n" in capsys.readouterr().out


def test_a_subscription_change_with_the_same_region_asks_too(project, monkeypatch, capsys):
    moving_project(project, names=("alpha",))
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-2\n")
    answer(monkeypatch, ["1", "eastus2", "4"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert "What should happen to the apps currently deployed to" in capsys.readouterr().out


def test_keep_pins_the_recorded_place_after_an_edit_to_pdt_yml(project, monkeypatch, capsys):
    moving_project(project, names=("alpha",))
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-2\n")
    answer(monkeypatch, ["2", "us-east-2", "UTC", "3"])
    assert scaffold.init(None, assume_yes=False) == 0
    assert platform_of(project, "alpha") == AZURE_PIN
    assert "alpha stays on azure, region eastus2, subscription sub-1, resource group pdt." in capsys.readouterr().out


@pytest.mark.parametrize("own,entry,platform", [
    ("platform:\n  region: westus2\n", "", None),
    ("", "apps:\n  - name: report\n    platform:\n      region: westus2\n",
     {"provider": "aws", "region": "us-east-2", "timezone": "UTC"}),
])
def test_a_moved_app_that_sets_its_own_region_deploys_to_the_new_region(project, monkeypatch,
                                                                        own, entry, platform):
    moving_project(project, names=())
    with (project / PROJECT_FILE).open("a") as file:
        file.write(entry)
    deployed_app(project, "report", own)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC", "1"], [True])
    assert scaffold.init(None, assume_yes=False) == 0
    from pdt import config
    assert platform_of(project, "report") == platform
    assert config.merged_app("report")["platform"]["region"] == "us-east-2"
    assert [name for name, *_ in cloud.deployed] == ["report"]


@pytest.mark.parametrize("replies", [["2", "cancel"], ["2", "us-east-2", "Cancel"]])
def test_cancel_at_the_region_or_time_zone_question_changes_nothing(project, monkeypatch, capsys,
                                                                     replies):
    before = moving_project(project)
    cloud = Cloud(monkeypatch)
    questions, replies = [], iter(replies)
    monkeypatch.setattr(move.console, "ask",
                        lambda question, default="": questions.append(question) or next(replies))
    assert scaffold.init(None, assume_yes=False) == 0
    assert cloud.destroyed == []
    assert (project / PROJECT_FILE).read_text() == before
    assert "Cancelled. Nothing changed." in capsys.readouterr().out
    assert questions[-1].endswith("(type cancel to change nothing)")


def test_cancel_at_the_region_question_of_a_new_project_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    answer(monkeypatch, ["1", "cancel"])
    with pytest.raises(ConfigError, match="cancelled"):
        scaffold.init(str(tmp_path / "jobs"), assume_yes=False)
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("platform,place", [
    ({"provider": "aws", "region": "us-east-1", "subscription": "sub-1"},
     {"provider": "aws", "region": "us-east-1"}),
    ({"provider": "windows", "region": "eastus2"}, {"provider": "windows"}),
])
def test_a_place_holds_only_the_keys_of_its_provider(platform, place):
    from pdt import config
    assert config.place(platform) == place


def test_an_app_with_its_own_provider_shows_only_its_own_place(project, monkeypatch, capsys):
    moving_project(project, names=())
    deployed_app(project, "own", "platform:\n  provider: aws\n  region: us-east-1\n")
    answer(monkeypatch, ["3", "us-east4", ""])
    assert scaffold.init(None, assume_yes=False) == 0
    out = capsys.readouterr().out
    assert "  own  aws, region us-east-1; set in own/config.yml\n" in out
    assert platform_of(project, "own") == {"provider": "aws", "region": "us-east-1"}


AWS = {"provider": "aws", "region": "us-east-2", "timezone": "UTC"}
TO_AWS = ["2", "us-east-2", "UTC"]


def test_move_only_an_app_on_pdt_yml_writes_only_its_config_yml(project, monkeypatch, capsys):
    before = moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS], [False, True])
    assert move.move_app("alpha") == 0
    out = capsys.readouterr().out
    assert "alpha is deployed to azure, region eastus2, subscription sub-1, resource group pdt." in out
    assert "Which apps should move from azure, region eastus2, subscription sub-1, resource group pdt?" in out
    assert "3) All 3 apps on azure, region eastus2, subscription sub-1, resource group pdt" in out
    assert "destroy alpha on azure, region eastus2, subscription sub-1, resource group pdt" in out
    assert "update alpha/config.yml" in out
    assert "deploy alpha to aws, region us-east-2" in out
    assert (project / PROJECT_FILE).read_text() == before
    assert platform_of(project, "alpha") == AWS
    assert platform_of(project, "beta") is None
    assert [name for name, *_ in cloud.destroyed] == ["alpha"]
    assert cloud.deployed == [("alpha", "deploy", "aws", True)]


def test_a_new_default_pins_each_app_that_stays(project, monkeypatch, capsys):
    moving_project(project)
    Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS], [True, True])
    assert move.move_app("alpha") == 0
    out = capsys.readouterr().out
    assert "The default platform in pdt.yml is azure, region eastus2, subscription sub-1, resource group pdt." in out
    assert "update beta/config.yml: beta stays on azure, region eastus2, subscription sub-1, resource group pdt" in out
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"] == AWS
    assert platform_of(project, "alpha") is None
    assert platform_of(project, "beta") == AZURE_PIN
    assert platform_of(project, "gamma") == AZURE_PIN


def test_move_some_moves_the_chosen_apps(project, monkeypatch):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "1 3", *TO_AWS], [False, True])
    assert move.move_app("alpha") == 0
    assert [name for name, *_ in cloud.destroyed] == ["alpha", "gamma"]
    assert [name for name, *_ in cloud.deployed] == ["alpha", "gamma"]
    assert platform_of(project, "alpha") == AWS
    assert platform_of(project, "beta") is None
    assert platform_of(project, "gamma") == AWS


def test_move_all_offers_the_new_default_and_writes_only_pdt_yml(project, monkeypatch):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["3", *TO_AWS], [])
    questions = []

    def confirm(question="Proceed?", default=False, choices=""):
        questions.append((question, default))
        return True

    monkeypatch.setattr(move.console, "confirm", confirm)
    assert move.move_app("beta") == 0
    assert questions == [("Change the default platform to aws, region us-east-2?", True),
                         ("Continue?", False)]
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"] == AWS
    assert [platform_of(project, name) for name in ("alpha", "beta", "gamma")] == [None, None, None]
    assert [name for name, *_ in cloud.deployed] == ["alpha", "beta", "gamma"]


def test_an_app_moved_to_the_default_place_uses_pdt_yml(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: aws\n  region: us-east-2\n  account: \"123456789012\"\n"
        "  timezone: UTC\n")
    for name in ("alpha", "beta", "gamma"):
        deployed_app(project, name)
    deployed_app(project, "own", "schedule: daily\nplatform:\n  provider: google-cloud\n"
                                 "  region: us-central1\n  project: demo\n  timezone: UTC\n")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, TO_AWS, [True])
    assert move.move_app("own") == 0
    out = capsys.readouterr().out
    assert "Which apps should move" not in out
    assert "Change the default platform" not in out
    assert (project / "own" / APP_FILE).read_text() == "schedule: daily\n"
    assert cloud.deployed == [("own", "deploy", "aws", True)]


def test_move_an_app_that_is_not_deployed_destroys_and_deploys_nothing(project, monkeypatch, capsys):
    for name in ("alpha", "beta"):
        add_app(project, name)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS], [False])
    assert move.move_app("alpha") == 0
    assert "alpha is not deployed. pdt.yml deploys it to azure, resource group pdt." in capsys.readouterr().out
    assert platform_of(project, "alpha") == AWS
    assert cloud.destroyed == cloud.deployed == []


def test_move_an_app_with_no_provider_asks_the_init_questions(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text("apps: []\n")
    for name in ("alpha", "beta"):
        add_app(project, name)
    answer(monkeypatch, TO_AWS, [True])
    assert move.move_app("alpha") == 0
    out = capsys.readouterr().out
    assert "alpha has no provider." in out
    assert "pdt.yml sets no default platform." in out
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"] == AWS
    assert platform_of(project, "alpha") is None


@pytest.mark.parametrize("replies,confirms", [
    (["4"], []),
    (["1", "5"], []),
    (["1", "2", "cancel"], []),
    (["1", *TO_AWS], [False, False]),
])
def test_every_cancel_in_move_changes_nothing(project, monkeypatch, capsys, replies, confirms):
    before = moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, replies, confirms)
    assert move.move_app("alpha") == 0
    assert "Cancelled. Nothing changed." in capsys.readouterr().out
    assert cloud.destroyed == []
    assert (project / PROJECT_FILE).read_text() == before
    assert platform_of(project, "alpha") is None


def test_a_failed_destroy_in_move_writes_no_config_yml_for_that_app(project, monkeypatch):
    before = moving_project(project)
    cloud = Cloud(monkeypatch, failing=("alpha",))
    answer(monkeypatch, ["1", *TO_AWS], [False, True])
    assert move.move_app("alpha") == 1
    assert cloud.deployed == []
    assert (project / PROJECT_FILE).read_text() == before
    assert platform_of(project, "alpha") is None


def test_move_to_the_current_place_changes_nothing(project, monkeypatch, capsys):
    before = moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", "1", "eastus2"])
    assert move.move_app("alpha") == 0
    assert ("azure, region eastus2, subscription sub-1, resource group pdt is the current place. "
            "Nothing changed.") in capsys.readouterr().out
    assert cloud.destroyed == []
    assert (project / PROJECT_FILE).read_text() == before


def test_move_with_no_terminal_changes_nothing(project, monkeypatch):
    before = moving_project(project)
    monkeypatch.setattr(move.deploy, "can_prompt", lambda interactive: False)
    with pytest.raises(ConfigError, match="Run pdt move in a terminal"):
        move.move_app("alpha")
    assert (project / PROJECT_FILE).read_text() == before


def test_a_new_default_pins_no_profile_on_an_app_of_another_provider(project, monkeypatch):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n  profile: work\n  timezone: UTC\n")
    add_app(project, "alpha")
    add_app(project, "own", "platform:\n  provider: google-cloud\n  region: us-central1\n")
    answer(monkeypatch, ["3", "us-central1", "UTC"], [True])
    assert move.move_app("alpha") == 0
    assert platform_of(project, "own") == {"provider": "google-cloud", "region": "us-central1"}
