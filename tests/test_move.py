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

    def __init__(self, monkeypatch, failing=(), before=lambda command, app_name: None):
        self.events = []
        self.failing = failing
        self.before = before
        self.write_text_atomically = move.config.write_text_atomically
        monkeypatch.setattr(move.deploy, "dispatch", self.dispatch)
        monkeypatch.setattr(move.config, "write_text_atomically", self.write)

    def dispatch(self, provider, command, app_name, assume_yes, extra=None, rebuild=False,
                 platform=None):
        if app_name in self.failing:
            return 1
        self.before(command, app_name)
        from pdt import config
        platform = platform or move.deploy.resolved(config.merged_app(app_name)["platform"])
        self.events.append((command, app_name, {**config.place(platform),
                                                **{key: platform[key] for key in ("profile",)
                                                   if platform.get(key)}}))
        return 0

    def write(self, path, text, newline=None):
        if path.name in (PROJECT_FILE, APP_FILE):
            from pdt import config
            self.events.append(("write", path.relative_to(config.find_project()).as_posix(), None))
        self.write_text_atomically(path, text, newline)

    @property
    def destroyed(self):
        return [(name, place) for command, name, place in self.events if command == "destroy"]

    @property
    def deployed(self):
        return [(name, place) for command, name, place in self.events if command == "deploy"]


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
AZURE_STAY = {"provider": "azure", "region": "eastus2", "subscription": "sub-1",
              "timezone": "Etc/UTC"}
AWS_PLACE = {"provider": "aws", "region": "us-east-2"}


@pytest.mark.parametrize("text", [
    "{platform: {provider: azure}, apps: []}\n",
    "platform: &defaults\n  provider: azure\napps:\n- name: report\n  platform: *defaults\n",
])
def test_replace_platform_refuses_a_form_it_cannot_change_safely(text):
    with pytest.raises(ConfigError, match="pdt.yml"):
        move.replace_platform(text, {"provider": "aws"})


@pytest.mark.parametrize("text", [
    "defaults: &defaults\n  provider: azure\nplatform: *defaults\napps: []\n",
    "zone: &zone UTC\nplatform:\n  provider: aws\n  timezone: *zone\napps: []\n",
    "platform:\n  provider: azure\napps: []\n...\n",
])
def test_replace_platform_refuses_an_alias_or_a_document_end(text):
    with pytest.raises(ConfigError, match="pdt.yml"):
        move.replace_platform(text, {"provider": "aws"})


def test_replace_platform_leaves_out_an_empty_setting():
    out = move.replace_platform("platform:\n  provider: azure\n  environment:\n",
                                    {"provider": "azure", "environment": None, "region": "eastus2"})
    assert yaml.safe_load(out) == {"platform": {"provider": "azure", "region": "eastus2"}}


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
    assert cloud.deployed == [("alpha", AWS_PLACE)]


def test_a_new_default_pins_each_app_that_stays(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS], [True, True])
    assert move.move_app("alpha") == 0
    out = capsys.readouterr().out
    assert "The default platform in pdt.yml is azure, region eastus2, subscription sub-1, resource group pdt." in out
    assert "update beta/config.yml: beta stays on azure, region eastus2, subscription sub-1, resource group pdt" in out
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"] == AWS
    assert platform_of(project, "alpha") is None
    assert platform_of(project, "beta") == AZURE_STAY
    assert platform_of(project, "gamma") == AZURE_STAY
    assert [event[:2] for event in cloud.events][:4] == [
        ("destroy", "alpha"), ("write", "beta/config.yml"), ("write", "gamma/config.yml"),
        ("write", "pdt.yml")]
    assert cloud.events[-1] == ("deploy", "alpha", AWS_PLACE)


def test_move_some_moves_the_chosen_apps(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["2", "1 3", *TO_AWS], [False, True])
    assert move.move_app("alpha") == 0
    table = "1  alpha  yes       pdt.yml\n2  beta   yes       pdt.yml\n3  gamma  yes       pdt.yml\n"
    out = capsys.readouterr().out
    assert out.index(table, out.index("4) Cancel")) < out.index("Where should your jobs run?")
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
    assert cloud.deployed == [("own", {**AWS_PLACE, "account": "123456789012"})]


def test_move_an_app_that_is_not_deployed_destroys_and_deploys_nothing(project, monkeypatch, capsys):
    for name in ("alpha", "beta"):
        add_app(project, name)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS], [False, True])
    assert move.move_app("alpha") == 0
    out = capsys.readouterr().out
    assert "alpha is not deployed. pdt.yml deploys it to azure, resource group pdt." in out
    assert "Plan:\n  update alpha/config.yml\n" in out
    assert platform_of(project, "alpha") == AWS
    assert cloud.destroyed == cloud.deployed == []


def test_move_an_app_with_no_provider_asks_the_init_questions(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text("apps: []\n")
    for name in ("alpha", "beta"):
        add_app(project, name)
    answer(monkeypatch, TO_AWS, [True, True])
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


def test_move_with_no_terminal_changes_nothing(project, monkeypatch, capsys):
    before = moving_project(project)
    monkeypatch.setattr(move.deploy, "can_prompt", lambda interactive: False)
    assert move.move_app("alpha") == 1
    assert "Run this in a terminal:\n  pdt move alpha\n" in capsys.readouterr().out
    assert (project / PROJECT_FILE).read_text() == before


def test_a_new_default_pins_no_profile_on_an_app_of_another_provider(project, monkeypatch):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n  profile: work\n  timezone: UTC\n")
    add_app(project, "alpha")
    add_app(project, "own", "platform:\n  provider: google-cloud\n  region: us-east4\n")
    answer(monkeypatch, ["3", "us-central1", "UTC"], [True, True])
    assert move.move_app("alpha") == 0
    assert platform_of(project, "own") == {"provider": "google-cloud", "region": "us-east4"}


def test_a_move_that_the_new_place_cannot_run_destroys_nothing(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text("platform:\n  provider: aws\n  region: us-east-2\n")
    deployed_app(project, "alpha", 'schedule: "0 0 1 * 1"\n')
    before = (project / "alpha" / APP_FILE).read_text()
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["4"], [False])
    assert move.move_app("alpha") == 1
    assert "alpha: schedule: " in capsys.readouterr().out
    assert cloud.events == []
    assert (project / "alpha" / APP_FILE).read_text() == before


def test_apps_with_other_aws_profiles_are_on_other_places(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text("platform:\n  provider: aws\n  region: us-east-2\n")
    add_app(project, "alpha", "platform:\n  profile: one\n")
    add_app(project, "beta", "platform:\n  profile: two\n")
    Cloud(monkeypatch)
    answer(monkeypatch, ["3", "us-central1", "UTC"], [False, True])
    assert move.move_app("alpha") == 0
    assert "Which apps should move" not in capsys.readouterr().out
    assert platform_of(project, "alpha")["provider"] == "google-cloud"
    assert platform_of(project, "beta") == {"profile": "two"}


def test_a_move_removes_each_platform_block_that_pdt_yml_now_sets(project, monkeypatch, capsys):
    moving_project(project, names=("alpha",))
    add_app(project, "beta", "platform:\n  provider: aws\n  region: us-east-2\n  timezone: UTC\n")
    Cloud(monkeypatch)
    answer(monkeypatch, TO_AWS, [True, True])
    assert move.move_app("alpha") == 0
    assert "  update beta/config.yml\n" in capsys.readouterr().out
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"] == AWS
    assert platform_of(project, "alpha") is None
    assert platform_of(project, "beta") is None


def test_a_move_reads_no_config_yml_that_it_does_not_change(project, monkeypatch):
    moving_project(project)
    (project / "beta" / APP_FILE).write_text("{}\n")
    Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS], [False, True])
    assert move.move_app("alpha") == 0
    assert platform_of(project, "alpha") == AWS
    assert (project / "beta" / APP_FILE).read_text() == "{}\n"


def test_an_app_that_stays_keeps_its_time_zone(project, monkeypatch):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n  timezone: America/Chicago\n")
    add_app(project, "alpha")
    add_app(project, "beta", "platform:\n  region: us-east-2\n")
    Cloud(monkeypatch)
    answer(monkeypatch, TO_AWS, [True, True])
    assert move.move_app("alpha") == 0
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"] == AWS
    assert platform_of(project, "beta") == {"timezone": "America/Chicago"}


def test_a_move_to_the_deployed_place_writes_the_config(project, monkeypatch):
    (project / PROJECT_FILE).write_text("platform:\n  provider: aws\n  region: us-east-2\n  timezone: UTC\n")
    deployed_app(project, "alpha")
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, TO_AWS, [False, True])
    assert move.move_app("alpha") == 0
    assert platform_of(project, "alpha") == AWS
    assert cloud.events == [("write", "alpha/config.yml", None)]


def test_no_answer_in_move_names_pdt_move(project, monkeypatch, capsys):
    moving_project(project)
    monkeypatch.setattr(move.deploy, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(move.console, "ask",
                        lambda question, default="": (_ for _ in ()).throw(EOFError))
    assert move.move_app("alpha") == 1
    assert "Run this in a terminal:\n  pdt move alpha\n" in capsys.readouterr().out


@pytest.mark.parametrize("assume_yes", [True, False])
def test_init_in_a_project_changes_nothing_and_names_pdt_move(project, monkeypatch, capsys, assume_yes):
    before = moving_project(project)
    cloud = Cloud(monkeypatch)
    assert scaffold.init(None, assume_yes=assume_yes) == 0
    assert "is already a pdt project.\n\nNext steps:\n  pdt move APP" in capsys.readouterr().out
    assert cloud.events == []
    assert (project / PROJECT_FILE).read_text() == before


def test_move_in_a_project_with_no_apps_moves_pdt_yml(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_bytes(
        "\ufeff# mine\r\nplatform:\r\n  provider: azure\r\n\r\napps: []\r\n".encode())
    answer(monkeypatch, TO_AWS, [True])
    assert move.move_app(None) == 0
    assert "Plan:\n  update pdt.yml\n" in capsys.readouterr().out
    assert (project / PROJECT_FILE).read_bytes().decode() == (
        "\ufeff# mine\r\nplatform:\r\n  provider: \"aws\"\r\n  region: \"us-east-2\"\r\n"
        "  timezone: \"UTC\"\r\n\r\napps: []\r\n")


@pytest.mark.parametrize("edited", [PROJECT_FILE, f"beta/{APP_FILE}"])
def test_an_edit_during_the_confirmation_stops_the_move_before_any_destroy(
        project, monkeypatch, capsys, edited):
    moving_project(project)
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS])

    def confirm(question, *args, **kwargs):
        if question == "Continue?":
            (project / edited).write_text("platform:\n  provider: google-cloud\n")
            return True
        return False

    monkeypatch.setattr(move.console, "confirm", confirm)
    assert move.move_app("alpha") == 1
    out = capsys.readouterr().out
    assert f"{edited} changed while pdt move was asking. Nothing changed." in out
    assert "Next steps:\n  pdt move alpha" in out
    assert cloud.events == []
    assert (project / edited).read_text() == "platform:\n  provider: google-cloud\n"


def test_a_failed_destroy_pins_each_app_still_deployed(project, monkeypatch, capsys):
    moving_project(project)
    cloud = Cloud(monkeypatch, failing=("beta",))
    answer(monkeypatch, ["3", *TO_AWS], [True, True])
    assert move.move_app("alpha") == 1
    out = capsys.readouterr().out
    assert "beta was not removed. It is still deployed to azure, region eastus2, subscription sub-1, resource group pdt." in out
    assert "alpha is deployed to aws, region us-east-2." in out
    assert ("Next steps:\n  pdt move beta   move it to aws, region us-east-2\n"
            "  pdt move gamma  move it to aws, region us-east-2\n") in out
    assert cloud.destroyed == [("alpha", AZURE_PIN)]
    assert cloud.deployed == [("alpha", AWS_PLACE)]
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"] == AWS
    assert platform_of(project, "beta") == AZURE_STAY
    assert platform_of(project, "gamma") == AZURE_STAY


def test_a_new_default_pins_an_app_that_is_not_deployed(project, monkeypatch):
    moving_project(project, names=("alpha", "beta"))
    add_app(project, "gamma")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS], [True, True])
    assert move.move_app("alpha") == 0
    assert cloud.destroyed == [("alpha", AZURE_PIN)]
    assert platform_of(project, "beta") == AZURE_STAY
    assert platform_of(project, "gamma") == AZURE_STAY


def test_a_config_yml_that_pdt_cannot_change_stops_the_move_before_any_destroy(project, monkeypatch):
    before = moving_project(project)
    (project / "beta" / APP_FILE).write_text("{}\n")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS], [True])
    with pytest.raises(ConfigError, match="beta/config.yml"):
        move.move_app("alpha")
    assert cloud.events == []
    assert (project / PROJECT_FILE).read_text() == before
    assert (project / "beta" / APP_FILE).read_text() == "{}\n"


def test_a_missing_env_var_stops_the_move_before_any_destroy(project, monkeypatch, capsys):
    monkeypatch.delenv("REPORT_TOKEN", raising=False)
    moving_project(project, names=())
    deployed_app(project, "alpha", "env:\n  required: [REPORT_TOKEN]\n")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, TO_AWS, [False])
    assert move.move_app("alpha") == 1
    assert "REPORT_TOKEN" in capsys.readouterr().out
    assert cloud.events == []


def test_ctrl_c_after_a_destroy_names_the_commands_left(project, monkeypatch, capsys):
    moving_project(project, names=("alpha",))

    def interrupt(command, app_name):
        if command == "deploy":
            raise KeyboardInterrupt

    Cloud(monkeypatch, before=interrupt)
    answer(monkeypatch, TO_AWS, [True, True])
    with pytest.raises(KeyboardInterrupt):
        move.move_app("alpha")
    out = capsys.readouterr().out
    assert "alpha was removed from azure, region eastus2, subscription sub-1, resource group pdt." in out
    assert "Next steps:\n  pdt deploy alpha  deploy it to aws, region us-east-2\n" in out


def test_an_edit_during_the_destroy_names_the_commands_left(project, monkeypatch, capsys):
    moving_project(project, names=("alpha",))

    def edit(command, app_name):
        (project / "alpha" / APP_FILE).write_text("schedule: daily\n")

    cloud = Cloud(monkeypatch, before=edit)
    answer(monkeypatch, TO_AWS, [False, True])
    assert move.move_app("alpha") == 1
    out = capsys.readouterr().out
    assert "alpha/config.yml changed while pdt move was running." in out
    assert "Next steps:\n  pdt move alpha  move it to aws, region us-east-2\n" in out
    assert "pdt deploy alpha" not in out
    assert [event[:2] for event in cloud.events] == [("destroy", "alpha")]


def test_the_plan_names_the_place_from_the_merged_platform(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text("platform:\n  provider: azure\n  region: eastus2\n"
                                        "  subscription: sub-1\n  resource_group: jobs\n")
    deployed_app(project, "own", "platform:\n  provider: google-cloud\n  region: us-central1\n")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", "eastus2"], [False, True])
    assert move.move_app("own") == 0
    assert ("deploy own to azure, region eastus2, subscription sub-1, resource group jobs"
            in capsys.readouterr().out)
    assert cloud.deployed == [("own", {"provider": "azure", "region": "eastus2",
                                       "subscription": "sub-1", "resource_group": "jobs"})]


def test_an_app_config_that_does_not_load_blocks_no_other_move(project, monkeypatch):
    moving_project(project, names=())
    add_app(project, "alpha", "env: []\n")
    add_app(project, "beta", "env: []\n")
    Cloud(monkeypatch)
    answer(monkeypatch, ["1", *TO_AWS], [False, True])
    assert move.move_app("alpha") == 0
    assert platform_of(project, "alpha") == AWS


def test_init_in_an_app_folder_finds_the_project(project, monkeypatch, capsys):
    add_app(project, "alpha")
    monkeypatch.chdir(project / "alpha")
    assert scaffold.init(None, assume_yes=True) == 0
    assert f"{project} is already a pdt project." in capsys.readouterr().out
    assert not (project / "alpha" / PROJECT_FILE).exists()


def test_a_stop_during_the_config_writes_names_the_command_for_each_app(project, monkeypatch, capsys):
    (project / PROJECT_FILE).write_text("platform:\n  provider: aws\n  region: us-east-2\n  timezone: UTC\n")
    azure = "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n"
    deployed_app(project, "alpha", azure)
    deployed_app(project, "beta", azure)
    Cloud(monkeypatch)
    write = move.config.write_text_atomically

    def interrupt(path, text, newline=None):
        if path == project / "beta" / APP_FILE:
            raise KeyboardInterrupt
        write(path, text, newline)

    monkeypatch.setattr(move.config, "write_text_atomically", interrupt)
    answer(monkeypatch, ["3", "2", "us-east-2", "UTC"], [True])
    with pytest.raises(KeyboardInterrupt):
        move.move_app("alpha")
    out = capsys.readouterr().out
    assert ("Next steps:\n  pdt deploy alpha  deploy it to aws, region us-east-2\n"
            "  pdt move beta     move it to aws, region us-east-2\n") in out
    assert "pdt deploy beta" not in out
    assert "pdt move alpha" not in out


def test_the_plan_resolves_the_place_with_the_app_env_file(project, monkeypatch, capsys):
    monkeypatch.setenv("PDT_AZURE_RESOURCE_GROUP", "unset")
    monkeypatch.delenv("PDT_AZURE_RESOURCE_GROUP")
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    deployed_app(project, "own", "platform:\n  provider: google-cloud\n  region: us-central1\n")
    (project / "own" / ".env").write_text("PDT_AZURE_RESOURCE_GROUP=jobs\n")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", "eastus2"], [True])
    assert move.move_app("own") == 0
    assert ("deploy own to azure, region eastus2, subscription sub-1, resource group jobs"
            in capsys.readouterr().out)
    assert cloud.deployed == [("own", {"provider": "azure", "region": "eastus2",
                                       "subscription": "sub-1", "resource_group": "jobs"})]


def test_a_region_change_keeps_the_resource_group_the_app_env_file_sets(project, monkeypatch, capsys):
    from pdt import config
    monkeypatch.setenv("PDT_AZURE_RESOURCE_GROUP", "unset")
    monkeypatch.delenv("PDT_AZURE_RESOURCE_GROUP")
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    add_app(project, "alpha")
    (project / "alpha" / ".env").write_text("PDT_AZURE_RESOURCE_GROUP=jobs\n")
    config.mark_deployed("alpha", {"provider": "azure", "region": "eastus2", "subscription": "sub-1",
                                   "resource_group": "jobs"})
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["1", "westus2"], [False, True])
    assert move.move_app("alpha") == 0
    assert ("deploy alpha to azure, region westus2, subscription sub-1, resource group jobs"
            in capsys.readouterr().out)
    assert cloud.deployed == [("alpha", {"provider": "azure", "region": "westus2",
                                         "subscription": "sub-1", "resource_group": "jobs"})]
    assert platform_of(project, "alpha") == {"provider": "azure", "region": "westus2",
                                             "subscription": "sub-1"}


def test_one_app_env_file_does_not_change_where_another_app_deploys(project, monkeypatch, capsys):
    monkeypatch.setenv("PDT_AZURE_RESOURCE_GROUP", "unset")
    monkeypatch.delenv("PDT_AZURE_RESOURCE_GROUP")
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    for name in ("alpha", "beta"):
        deployed_app(project, name, "platform:\n  provider: google-cloud\n  region: us-central1\n")
    (project / "beta" / ".env").write_text("PDT_AZURE_RESOURCE_GROUP=jobs\n")
    cloud = Cloud(monkeypatch)
    answer(monkeypatch, ["3", "1", "eastus2"], [True])
    assert move.move_app("alpha") == 0
    out = capsys.readouterr().out
    assert "deploy alpha to azure, region eastus2, subscription sub-1, resource group pdt" in out
    assert "deploy beta to azure, region eastus2, subscription sub-1, resource group jobs" in out
    assert cloud.deployed == [("alpha", AZURE_PIN), ("beta", {**AZURE_PIN, "resource_group": "jobs"})]
    assert "PDT_AZURE_RESOURCE_GROUP" not in os.environ


def test_a_move_saves_the_account_the_deploy_finds_in_the_record(project, monkeypatch):
    from pdt import config

    def find_account(command, app_name):
        if command == "deploy":
            config.save_platform_key(config.merged_app(app_name), "account", "123456789012")

    moving_project(project, names=("alpha",))
    Cloud(monkeypatch, before=find_account)
    answer(monkeypatch, TO_AWS, [False, True])
    assert move.move_app("alpha") == 0
    recorded = config.deployed_platform("alpha")
    assert recorded["account"] == "123456789012"
    assert not move.deploy.moved(recorded, config.merged_app("alpha")["platform"])


def test_an_app_that_stays_gets_no_pin_its_env_file_sets(project, monkeypatch):
    monkeypatch.setenv("PDT_AZURE_RESOURCE_GROUP", "unset")
    monkeypatch.delenv("PDT_AZURE_RESOURCE_GROUP")
    moving_project(project, names=("alpha",))
    add_app(project, "beta")
    (project / "beta" / ".env").write_text("PDT_AZURE_RESOURCE_GROUP=jobs\n")
    from pdt import config
    config.mark_deployed("beta", {**AZURE_PIN, "resource_group": "jobs"})
    Cloud(monkeypatch)
    answer(monkeypatch, TO_AWS, [True, True])
    assert move.move_app("alpha") == 0
    assert platform_of(project, "beta") == AZURE_STAY


def test_an_app_that_stays_pins_the_default_that_the_new_pdt_yml_changes(project, monkeypatch):
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    deployed_app(project, "alpha", "platform:\n  resource_group: jobs\n")
    deployed_app(project, "beta")
    Cloud(monkeypatch)
    answer(monkeypatch, ["1", "westus2"], [True, True])
    assert move.move_app("alpha") == 0
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"]["resource_group"] == "jobs"
    assert platform_of(project, "beta") == {"region": "eastus2", "resource_group": "pdt"}
    assert move.configured("beta")["resource_group"] == "pdt"


def test_an_app_that_stays_pins_the_aws_profile_its_environment_sets(project, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "dev")
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: aws\n  region: us-east-1\n  account: \"111111111111\"\n  timezone: UTC\n")
    deployed_app(project, "alpha", "platform:\n  account: \"222222222222\"\n  profile: prod\n")
    deployed_app(project, "beta")
    Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC"], [True, True])
    assert move.move_app("alpha") == 0
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"]["profile"] == "prod"
    assert platform_of(project, "beta")["profile"] == "dev"
    assert move.configured("beta")["profile"] == "dev"


def test_an_aws_app_that_stays_keeps_its_profile_when_pdt_yml_sets_another_provider(project, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "dev")
    (project / PROJECT_FILE).write_text(
        "platform:\n  provider: azure\n  region: eastus2\n  subscription: sub-1\n")
    aws = "platform:\n  provider: aws\n  region: us-east-1\n  account: \"111111111111\"\n"
    deployed_app(project, "alpha", aws + "  profile: prod\n")
    deployed_app(project, "beta", aws)
    Cloud(monkeypatch)
    answer(monkeypatch, ["2", "us-east-2", "UTC"], [True, True])
    assert move.move_app("alpha") == 0
    assert yaml.safe_load((project / PROJECT_FILE).read_text())["platform"]["profile"] == "prod"
    assert platform_of(project, "beta")["profile"] == "dev"
    assert move.configured("beta")["profile"] == "dev"

