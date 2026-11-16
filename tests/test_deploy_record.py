import json
import os

import pytest
from rich.text import Text

from conftest import add_app

from pdt import alerts_cli, cli, config, console, deploy, deploy_aws, deploy_azure, scaffold

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
    assert config.deployed_places() == {APP: config.place(deploy.resolved(config.merged_app(APP)["platform"]))}
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
    assert "2) Cancel\n" in out and "Cancelled.\n" in out and "Nothing changed" not in out


@pytest.mark.parametrize("move", MOVES)
def test_remove_first_destroys_at_the_recorded_place_then_deploys(project, monkeypatch, move):
    old, new = MOVES[move]
    recorded = deployed_to(project, old)
    (project / "pdt.yml").write_text(new)
    cloud = Cloud(monkeypatch, answers=["3", "1"])
    assert deploy.deploy(APP, run=False) == 0
    current = deploy.resolved(config.merged_app(APP)["platform"])
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


def test_a_plan_shows_the_move_and_asks_destroys_and_records_nothing(project, monkeypatch, capsys):
    recorded = deployed_to(project, AWS)
    (project / "pdt.yml").write_text(AZURE)
    cloud = Cloud(monkeypatch)
    assert deploy.deploy(APP, plan_only=True) == 0
    assert cloud.calls == [("azure", "deploy", None)] and cloud.asked == []
    assert config.deployed_platform(APP) == recorded
    out = capsys.readouterr().out
    assert "now deploys it to" in out and "A deploy asks what to do" in out
    assert "Plan only; nothing was deployed" in out


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


def test_the_record_has_the_place_the_environment_selects(project, monkeypatch):
    (project / "pdt.yml").write_text(GOOGLE.replace("  project: my-project\n", ""))
    monkeypatch.setenv("PDT_GOOGLE_CLOUD_PROJECT", "env-project")
    cloud = Cloud(monkeypatch)
    assert deploy.deploy(APP, run=False) == 0
    assert config.deployed_places()[APP]["project"] == "env-project"
    assert deploy.deploy(APP, run=False) == 0
    assert cloud.asked == []
    monkeypatch.setenv("PDT_GOOGLE_CLOUD_PROJECT", "other-project")
    Cloud(monkeypatch, answers=[""])
    assert deploy.deploy(APP, run=False) == deploy.DECLINED


def test_a_removed_resource_group_is_a_move(project, monkeypatch, capsys):
    monkeypatch.delenv("PDT_AZURE_RESOURCE_GROUP", raising=False)
    deployed_to(project, AZURE + "  resource_group: custom-group\n")
    (project / "pdt.yml").write_text(AZURE)
    cloud = Cloud(monkeypatch, answers=[""])
    assert deploy.deploy(APP, run=False) == deploy.DECLINED
    assert cloud.asked == ["Which one?"]
    assert "resource group custom-group" in capsys.readouterr().out


def test_a_first_deploy_records_the_place_before_the_provider_runs(project, monkeypatch):
    (project / "pdt.yml").write_text(AWS)
    recorded = []

    def dispatch(*args, **kwargs):
        recorded.append(config.deployed_platform(APP))
        return 1

    Cloud(monkeypatch)
    monkeypatch.setattr(deploy, "dispatch", dispatch)
    assert deploy.deploy(APP, run=False) == 1
    assert recorded == [config.merged_app(APP)["platform"]]
    assert config.deployed_platform(APP) == recorded[0]


def test_destroy_offers_a_disabled_app_that_is_deployed(project, monkeypatch):
    deployed_to(project, AWS)
    config.set_app_enabled(APP, False)
    assert APP not in config.find_apps()
    cloud = Cloud(monkeypatch)
    assert cli.choose_app(APP, "destroy") == APP
    assert cli.destroy_all(assume_yes=True, skip_failures=False) == 0
    assert [command for _, command, _ in cloud.calls] == ["destroy"]
    assert not config.is_deployed(APP)


def test_a_destroy_with_a_record_needs_the_recorded_azure_subscription(project, monkeypatch,
                                                                       capsys):
    monkeypatch.setenv(config.DEPLOYED_PLATFORM_ENV, json.dumps(
        {"app": APP, "platform": {"provider": "azure", "subscription": "sub-1"}}))
    monkeypatch.setattr(deploy_azure, "az_json",
                        lambda *args: [{"id": "sub-2", "name": "Other"}])
    monkeypatch.setattr("builtins.input", lambda prompt="": "1")
    with pytest.raises(SystemExit) as stopped:
        deploy_azure.choose_subscription(config.merged_app(APP), "sub-1", True)
    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "is deployed to Azure subscription sub-1" in out
    assert "\n  pdt az login" in out and f"\n  pdt destroy {APP}" in out


def test_a_declined_deploy_keeps_a_found_key_and_restores_the_record(project, monkeypatch):
    (project / "pdt.yml").write_text(GOOGLE.replace("  project: my-project\n", ""))
    monkeypatch.setenv("PDT_GOOGLE_CLOUD_PROJECT", "project-a")
    Cloud(monkeypatch)
    assert deploy.deploy(APP, run=False) == 0
    recorded = config.deployed_platform(APP)
    monkeypatch.delenv("PDT_GOOGLE_CLOUD_PROJECT")
    seen = []

    def dispatch(*args, **kwargs):
        seen.append(config.deployed_platform(APP))
        config.save_platform_key(config.merged_app(APP), "project", "project-a")
        return deploy.DECLINED

    monkeypatch.setattr(deploy, "dispatch", dispatch)
    assert deploy.deploy(APP, run=False) == deploy.DECLINED
    assert seen[0]["project"] == "project-a"
    assert config.deployed_platform(APP) == recorded


def test_a_declined_first_deploy_is_not_deployed(project, monkeypatch):
    (project / "pdt.yml").write_text(AWS)
    Cloud(monkeypatch)
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: deploy.DECLINED)
    assert deploy.deploy(APP, run=False) == deploy.DECLINED
    assert not config.is_deployed(APP)
    assert config.deployed_platform(APP) is None


def test_a_failed_deploy_records_the_keys_it_saved(project, monkeypatch):
    (project / "pdt.yml").write_text(AWS.replace("  account: \"123456789012\"\n", ""))

    def dispatch(*args, **kwargs):
        config.save_platform_key(config.merged_app(APP), "account", "123456789012")
        return 1

    Cloud(monkeypatch)
    monkeypatch.setattr(deploy, "dispatch", dispatch)
    assert deploy.deploy(APP, run=False) == 1
    assert config.deployed_places()[APP]["account"] == "123456789012"


def test_destroy_goes_to_the_default_resource_group_the_app_was_deployed_to(project,
                                                                             monkeypatch):
    monkeypatch.delenv("PDT_AZURE_RESOURCE_GROUP", raising=False)
    (project / "pdt.yml").write_text(AZURE)
    cloud = Cloud(monkeypatch)
    assert deploy.deploy(APP, run=False) == 0
    monkeypatch.setenv("PDT_AZURE_RESOURCE_GROUP", "other-group")
    assert deploy.destroy(APP) == 0
    assert cloud.calls[-1][2]["resource_group"] == "pdt"


def test_the_aws_session_uses_the_region_the_record_has(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

    class Session:
        def __init__(self, region_name=None, profile_name=None):
            self.region_name = region_name or os.environ["AWS_DEFAULT_REGION"]
            self.profile_name = profile_name

        def get_credentials(self):
            return object()

        def client(self, *args, **kwargs):
            return self

        def get_caller_identity(self):
            return {}

    monkeypatch.setattr(deploy_aws.boto3, "Session", Session)
    platform = {"provider": "aws"}
    session = deploy_aws.ensure_session({"name": APP, "platform": platform})
    assert session.region_name == deploy.resolved(platform)["region"] == "us-east-1"


def test_an_interrupted_deploy_records_the_keys_it_saved(project, monkeypatch):
    (project / "pdt.yml").write_text(AWS.replace("  account: \"123456789012\"\n", ""))

    def dispatch(*args, **kwargs):
        config.save_platform_key(config.merged_app(APP), "account", "123456789012")
        raise KeyboardInterrupt

    Cloud(monkeypatch)
    monkeypatch.setattr(deploy, "dispatch", dispatch)
    with pytest.raises(KeyboardInterrupt):
        deploy.deploy(APP, run=False)
    assert config.deployed_places()[APP]["account"] == "123456789012"


def test_a_deploy_goes_to_the_found_key_the_record_keeps(project, monkeypatch):
    (project / "pdt.yml").write_text(GOOGLE.replace("  project: my-project\n", ""))
    monkeypatch.setenv("PDT_GOOGLE_CLOUD_PROJECT", "project-a")
    cloud = Cloud(monkeypatch)
    assert deploy.deploy(APP, run=False) == 0
    monkeypatch.delenv("PDT_GOOGLE_CLOUD_PROJECT")
    assert deploy.deploy(APP, run=False) == 0
    assert cloud.calls[0][2] is None
    assert cloud.calls[1][2]["project"] == "project-a"
    assert config.deployed_places()[APP]["project"] == "project-a"


@pytest.mark.parametrize("pressed,commands", [(["enter"], []), (["up", "enter"], ["destroy", "deploy"])])
def test_in_a_menu_the_marker_starts_on_cancel(project, monkeypatch, press, pressed, commands):
    old, new = MOVES["region"]
    deployed_to(project, old)
    (project / "pdt.yml").write_text(new)
    cloud = Cloud(monkeypatch)
    press(*pressed)
    deploy.deploy(APP, run=False)
    assert [command for _provider, command, _platform in cloud.calls] == commands
    assert cloud.asked == []


SUB_1 = "11111111-1111-1111-1111-111111111111"
SUB_2 = "22222222-2222-2222-2222-222222222222"
AZURE_ID = AZURE.replace("sub-1", SUB_1)


def reader(project, text, read_from=APP):
    add_app(project, "reader", f"storage:\n  read: [{read_from}]\n")
    (project / "pdt.yml").write_text(text)
    return config.merged_app("reader")


@pytest.mark.parametrize("text,elsewhere,store", [
    (AWS, {"provider": "aws", "region": "us-east-1", "account": "210987654321"},
     "AWS account 123456789012 and reads only from the storage of that account"),
    (AZURE_ID, {"provider": "azure", "region": "eastus", "subscription": SUB_2},
     f"Azure subscription {SUB_1} and reads only from the storage of that subscription"),
    (GOOGLE, {"provider": "google-cloud", "region": "us-central1", "project": "other-project"},
     "Google Cloud project my-project and reads only from the storage of that project"),
    (WINDOWS, {"provider": "azure", "region": "eastus", "subscription": SUB_1},
     "Windows and reads only from the storage of that computer"),
])
def test_a_read_app_deployed_to_another_store_is_refused(project, text, elsewhere, store):
    app = reader(project, text)
    config.mark_deployed(APP, elsewhere)
    message = place_problem(app)
    assert message.startswith(f"storage.read in reader/pdt.yml names {APP}, which is deployed to "
                              f"{deploy.describe_place(config.place(elsewhere))}. ")
    assert f"reader deploys to {store}." in message
    assert message.endswith(", or remove it from storage.read.")


def place_problem(app, base=None):
    base = os.environ if base is None else base
    planned = {app["name"]: deploy.destination(app["name"], base)}
    return Text.from_markup(deploy.read_place_problem(app, base, planned)).plain


@pytest.mark.parametrize("text,region", [(AWS, "eu-west-1"), (AZURE_ID, "westeurope"),
                                         (GOOGLE, "europe-west1")])
def test_a_read_app_in_another_region_of_the_same_store_is_read(project, text, region):
    app = reader(project, text)
    platform = config.merged_app(APP)["platform"]
    config.mark_deployed(APP, {**platform, "region": region})
    assert place_problem(app) == ""


def test_a_read_app_whose_config_names_another_project_is_refused(project):
    app = reader(project, GOOGLE)
    (project / APP / "pdt.yml").write_text("platform:\n  project: other-project\n")
    message = place_problem(app)
    assert (f"names {APP}, which deploys to google-cloud, region us-central1, "
            "project other-project.") in message
    assert message.endswith(f"Deploy {APP} to Google Cloud project my-project, "
                            "or remove it from storage.read.")


def test_a_read_app_that_is_not_deployed_takes_the_place_its_own_env_gives(project):
    app = reader(project, AZURE_ID.replace(f"  subscription: {SUB_1}\n", ""))
    (project / "reader" / ".env").write_text(f"PDT_AZURE_SUBSCRIPTION={SUB_1}\n")
    (project / APP / ".env").write_text(f"PDT_AZURE_SUBSCRIPTION={SUB_2}\n")
    message = place_problem(app, base={})
    assert f"names {APP}, which deploys to azure, region eastus, subscription {SUB_2}, " in message
    assert f"reader deploys to Azure subscription {SUB_1}" in message
    (project / APP / ".env").unlink()
    assert place_problem(app, base={"PDT_AZURE_SUBSCRIPTION": SUB_1}) == ""


def test_a_read_list_in_the_project_file_is_named_there(project):
    add_app(project, "reader")
    (project / "pdt.yml").write_text(
        f"{AZURE_ID}apps:\n  - name: reader\n    storage:\n      read: [{APP}]\n")
    config.mark_deployed(APP, {"provider": "azure", "region": "eastus", "subscription": SUB_2})
    message = place_problem(config.merged_app("reader"))
    assert message.startswith(f"storage.read in pdt.yml: apps entry 'reader' names {APP}, ")


def test_validation_takes_the_provider_where_the_read_app_is_deployed(project):
    reader(project, AZURE_ID)
    (project / APP / "pdt.yml").write_text("platform:\n  provider: google-cloud\n")
    config.mark_deployed(APP, {"provider": "azure", "region": "eastus", "subscription": SUB_1})
    assert not any("provider" in problem for problem in config.validate_app("reader"))
    assert place_problem(config.merged_app("reader")) == ""
    config.mark_deployed(APP, None)
    assert any(f"names '{APP}', which uses the google-cloud provider" in problem
               for problem in config.validate_app("reader"))


def test_deploy_all_checks_every_app_where_this_run_deploys_it(project, monkeypatch, capsys):
    reader(project, AZURE_ID)
    config.mark_deployed(APP, {"provider": "azure", "region": "eastus", "subscription": SUB_1})
    (project / APP / "pdt.yml").write_text(f"platform:\n  subscription: {SUB_2}\n")
    cloud = Cloud(monkeypatch)
    assert deploy.deploy("reader", assume_yes=True, run=False) == 0
    cloud.calls.clear()
    assert cli.deploy_all(assume_yes=True, skip_failures=False) == 1
    assert cloud.calls == []
    out = " ".join(capsys.readouterr().out.split())
    assert f"names {APP}, which deploys to azure, region eastus, subscription {SUB_2}, " in out


@pytest.mark.parametrize("plan_only", [False, True])
def test_the_refusal_stops_the_deploy_before_the_provider_runs(project, monkeypatch, capsys,
                                                                plan_only):
    reader(project, AZURE_ID)
    config.mark_deployed(APP, {"provider": "azure", "region": "eastus", "subscription": SUB_2})
    cloud = Cloud(monkeypatch)
    assert deploy.deploy("reader", run=False, plan_only=plan_only) == 1
    assert cloud.calls == []
    assert not config.is_deployed("reader")
    out = " ".join(capsys.readouterr().out.split())
    assert f"Deploy {APP} to Azure subscription {SUB_1}, or remove it from storage.read." in out


def test_the_refusal_comes_before_the_env_check_and_the_powershell_scan(project, monkeypatch):
    reader(project, AZURE_ID)
    config.mark_deployed(APP, {"provider": "azure", "region": "eastus", "subscription": SUB_2})
    monkeypatch.setattr(config, "missing_env", lambda app: pytest.fail("env checked"))
    monkeypatch.setattr(config, "powershell_scripts", lambda folder: pytest.fail("scanned"))
    Cloud(monkeypatch)
    assert deploy.deploy("reader", run=False) == 1


def test_a_shared_env_file_expands_each_app_s_own_values(project):
    app = reader(project, GOOGLE.replace("  project: my-project\n", ""))
    (project / ".env").write_text("PDT_GOOGLE_CLOUD_PROJECT=${PROJECT_ID}\n")
    (project / "reader" / ".env").write_text("PROJECT_ID=project-a\n")
    (project / APP / ".env").write_text("PROJECT_ID=project-b\n")
    assert config.app_environment(project / APP, {})["PDT_GOOGLE_CLOUD_PROJECT"] == "project-b"
    assert config.app_environment(project / APP, {"PROJECT_ID": "c"})["PDT_GOOGLE_CLOUD_PROJECT"] == "c"
    message = place_problem(app, base={})
    assert f"names {APP}, which deploys to google-cloud, region us-central1, project project-b." in message
    assert "reader deploys to Google Cloud project project-a" in message


def test_each_deploy_of_a_run_checks_where_the_run_deploys_the_read_app(project, monkeypatch):
    reader(project, AZURE_ID.replace(SUB_1, SUB_2), read_from="warehouse")
    add_app(project, "warehouse", "schedule: daily\n")
    config.mark_deployed("warehouse", {"provider": "azure", "region": "eastus", "subscription": SUB_1})
    Cloud(monkeypatch)
    # --yes does not move warehouse from its recorded subscription, so only its deploy stops.
    cli.deploy_all(assume_yes=True, skip_failures=True)
    assert config.deployed_places()["reader"]["subscription"] == SUB_2


@pytest.mark.parametrize("everything", [False, True])
def test_the_place_message_comes_before_the_provider_problem(project, monkeypatch, capsys, everything):
    reader(project, AZURE_ID)
    (project / APP / "pdt.yml").write_text("platform:\n  provider: google-cloud\n  project: p\n")
    config.mark_deployed(APP, {"provider": "google-cloud", "region": "us-central1", "project": "p"})
    monkeypatch.setattr(config, "missing_env", lambda app: pytest.fail("env checked"))
    cloud = Cloud(monkeypatch)
    if everything:
        assert cli.deploy_all(assume_yes=True, skip_failures=False) == 1
    else:
        assert deploy.deploy("reader", run=False) == 1
    assert cloud.calls == []
    out = " ".join(capsys.readouterr().out.split())
    where = "deploys to google-cloud, region eastus" if everything else (
        "is deployed to google-cloud, region us-central1")
    assert f"names {APP}, which {where}, project p. reader deploys to Azure subscription" in out
    assert "uses the google-cloud provider" not in out


def test_a_subscription_compares_only_by_its_id(project):
    app = reader(project, AZURE.replace("sub-1", "Pay-As-You-Go"))
    config.mark_deployed(APP, {"provider": "azure", "region": "eastus", "subscription": SUB_2})
    assert place_problem(app) == ""
    config.mark_deployed("reader", {"provider": "azure", "region": "eastus", "subscription": SUB_1})
    assert f"reader deploys to Azure subscription {SUB_1} and" in place_problem(app)
    config.mark_deployed(APP, {"provider": "azure", "region": "eastus", "subscription": "Production"})
    assert place_problem(app) == ""


def test_a_subscription_id_compares_in_any_case(project):
    app = reader(project, AZURE_ID.replace(SUB_1, SUB_1.upper().replace("1", "A")))
    config.mark_deployed(APP, {"provider": "azure", "region": "eastus",
                               "subscription": SUB_1.replace("1", "a")})
    assert place_problem(app) == ""


def test_the_id_found_for_a_display_name_stays_in_the_record_while_the_name_does(project):
    app = reader(project, AZURE.replace("sub-1", "Prod"))
    config.mark_deployed("reader", {"provider": "azure", "region": "eastus", "subscription": "Prod",
                                    config.STORE_ID_KEY: SUB_1})
    config.mark_deployed(APP, {"provider": "azure", "region": "eastus", "subscription": SUB_2})
    assert f"reader deploys to Azure subscription {SUB_1} and" in place_problem(app)
    deploy.record_place("reader", config.deployed_platform("reader"))
    assert config.deployed_platform("reader")[config.STORE_ID_KEY] == SUB_1
    (project / "pdt.yml").write_text(AZURE.replace("sub-1", "Dev"))
    assert place_problem(config.merged_app("reader")) == ""
    deploy.record_place("reader", config.deployed_platform("reader"))
    assert config.STORE_ID_KEY not in config.deployed_platform("reader")


def test_env_values_follow_load_dotenv_for_a_name_set_twice(project):
    (project / APP / ".env").write_text("PROJECT_ID=old\nX=${PROJECT_ID}\nPROJECT_ID=new\n")
    env = config.app_environment(project / APP, {})
    assert (env["PROJECT_ID"], env["X"]) == ("new", "old")
    from dotenv import dotenv_values
    assert dotenv_values(project / APP / ".env") == {"PROJECT_ID": "new", "X": "old"}


def test_validation_in_a_run_takes_the_place_the_run_deploys_the_read_app(project, monkeypatch):
    reader(project, AZURE_ID, read_from="warehouse")
    add_app(project, "warehouse", "schedule: daily\n")
    config.mark_deployed("warehouse", {"provider": "windows"})
    assert any("uses the windows provider" in problem for problem in config.validate_app("reader"))
    Cloud(monkeypatch)
    cli.deploy_all(assume_yes=True, skip_failures=True)
    assert config.deployed_places()["reader"]["provider"] == "azure"


def test_an_unknown_provider_is_left_to_validation(project):
    app = reader(project, AZURE_ID)
    assert deploy.read_place_problem(app, {}, {"reader": {"provider": "nowhere"}}) == ""
