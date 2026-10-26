import json
import sys
from pathlib import Path

import pytest

from conftest import add_app, plain
from pdt import (
    cli, config, deploy, deploy_aws, deploy_azure, deploy_common, deploy_google_cloud, gcloud_sdk,
    pwsh, regions,
)
from pdt.utils import email_auth


@pytest.fixture
def plan_env(monkeypatch):
    monkeypatch.delenv(config.PLAN_ENV, raising=False)


@pytest.fixture
def aws_project(project, plan_env):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n")
    return project


def test_a_plan_keeps_a_platform_value_out_of_the_file_and_in_the_merged_app(aws_project):
    add_app(aws_project, "report", "schedule: daily\n")
    with config.plan_only():
        config.save_platform_key(config.merged_app("report"), "account", "000000000000")
        assert (aws_project / "pdt.yml").read_text() == "platform:\n  provider: aws\n"
        assert config.merged_app("report")["platform"]["account"] == "000000000000"


def test_a_plan_keeps_the_schedule_out_of_the_app_file(aws_project):
    folder = add_app(aws_project, "report")
    with config.plan_only():
        assert deploy.choose_schedule(config.merged_app("report"), True) == ""
        assert not (folder / "config.yml").exists()
        assert config.merged_app("report")["schedule"] == "daily"


def test_the_plan_asks_nothing_saves_nothing_and_starts_the_provider_with_plan(
        aws_project, monkeypatch, capsys):
    add_app(aws_project, "report")
    seen = []
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: False)
    monkeypatch.setattr(regions, "can_prompt", lambda _interactive: False, raising=False)
    monkeypatch.setattr(regions, "region_suggestion",
                        lambda provider, platform: ("us-east-1", "a test says so"))
    monkeypatch.setattr(deploy, "dispatch", lambda *args: seen.append(
        (args, config.unsaved())) or 0)
    monkeypatch.setattr(deploy.alerts_cli, "offer", lambda *args: pytest.fail("offered alerts"))
    monkeypatch.setattr(deploy, "offer_run", lambda *args: pytest.fail("offered a run"))
    before = sorted(str(path) for path in Path(aws_project).rglob("*"))

    assert deploy.deploy("report", plan_only=True) == 0

    assert seen == [(("aws", "deploy", "report", True, ["--plan"]),
                     {"schedule": "daily", "platform": {"region": "us-east-1"}})]
    assert sorted(str(path) for path in Path(aws_project).rglob("*")) == before
    assert (aws_project / "pdt.yml").read_text() == "platform:\n  provider: aws\n"
    assert not config.is_deployed("report")
    assert config.unsaved() is None
    out = capsys.readouterr().out
    assert "nothing was deployed" in out and "pdt deploy report --yes" in out
    assert "because" not in out and "has no schedule" not in out
    assert out.count("this plan changes no file") == 2


def test_the_plan_values_reach_the_provider_script(aws_project, monkeypatch):
    with config.plan_only():
        config.keep_unsaved("region", "us-east-1", platform=True)
        env = deploy.provider_env()
    assert json.loads(env[config.PLAN_ENV]) == {"platform": {"region": "us-east-1"}}


@pytest.mark.parametrize("flags", [["--all"], ["--yes"], ["--run"], ["--rebuild"]])
def test_plan_deploys_one_app_and_takes_no_deploy_flag(aws_project, monkeypatch, capsys, flags):
    add_app(aws_project, "report", "schedule: daily\n")
    monkeypatch.setattr(deploy, "deploy", lambda *args, **kwargs: pytest.fail("deployed"))
    app = [] if flags == ["--all"] else ["report"]
    monkeypatch.setattr("sys.argv", ["pdt", "deploy", *app, "--plan", *flags])
    assert cli.main() == 1
    assert "--plan shows one app" in capsys.readouterr().out


@pytest.mark.parametrize("script", sorted(set(deploy.PROVIDERS.values())))
def test_every_provider_script_takes_plan(script):
    source = (Path(deploy.__file__).with_name(script)).read_text()
    assert 'parser.add_argument("--plan", action="store_true")' in source
    assert "deploy(app, args.yes, args.plan)" in source
    assert "destroy(app, args.yes, args.plan)" in source


def test_a_google_cloud_plan_shows_the_plan_and_changes_nothing(project, monkeypatch, capsys):
    module = deploy_google_cloud
    enabled = []
    monkeypatch.setattr(module, "build_in_cloud", lambda app, builder, image_platform: False)
    monkeypatch.setattr(module, "preflight", lambda app, project, assume_yes: project)
    monkeypatch.setattr(module, "ensure_apis",
                        lambda project, assume_yes, apis: enabled.append(assume_yes) or False)
    monkeypatch.setattr(module, "gather_secrets", lambda app: {})
    monkeypatch.setattr(module, "read_json_or_none", lambda *args: None)
    monkeypatch.setattr(module, "service_account_or_none", lambda project, email: None)
    monkeypatch.setattr(module, "cost_estimate", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "run_quiet", lambda *args, **kwargs: pytest.fail("changed"))
    monkeypatch.setattr(module, "confirm", lambda *args: pytest.fail("asked to proceed"))
    app = {"name": "report", "dir": project, "schedule": "0 6 * * *", "timezone": "UTC",
           "storage": False, "pause": False,
           "platform": {"project": "demo-project", "region": "us-central1"}}

    assert module.deploy(app, True, plan_only=True) == 0

    assert enabled == [True]
    out = capsys.readouterr().out
    assert "Plan:" in out and "create Cloud Run job pdt-report" in out


def test_each_plan_starts_with_no_values_and_restores_the_previous_ones(plan_env, monkeypatch):
    monkeypatch.setenv(config.PLAN_ENV, '{"schedule": "hourly"}')
    with config.plan_only():
        assert config.unsaved() == {}
    assert config.unsaved() == {"schedule": "hourly"}


def test_a_plan_writes_no_state(project, plan_env):
    with config.plan_only():
        config.write_state({"preflight": 1})
    assert not (project / config.STATE_DIR).exists()


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def test_a_plan_asks_nothing(plan_env, terminal):
    assert email_auth.can_prompt(None)
    with config.plan_only():
        assert not email_auth.can_prompt(None)


class Profiles:
    available_profiles = ["one", "two"]


def test_an_aws_plan_picks_no_profile(aws_project, terminal, monkeypatch):
    monkeypatch.setattr(deploy_aws, "ask", lambda prompt: pytest.fail("asked"))
    with config.plan_only(), pytest.raises(SystemExit):
        deploy_aws.choose_profile({"name": "report"}, Profiles())


def test_a_google_cloud_plan_creates_no_project_and_runs_no_login(project, plan_env, terminal,
                                                                  monkeypatch):
    module = deploy_google_cloud
    monkeypatch.setattr(module, "have_credentials", lambda: False)
    monkeypatch.setattr(module, "login_with_key_file", lambda: pytest.fail("logged in"))
    monkeypatch.setattr(module, "run_quiet", lambda *args: pytest.fail("listed projects"))
    with config.plan_only():
        with pytest.raises(SystemExit):
            module.ensure_credentials()
        with pytest.raises(SystemExit):
            module.choose_project({"name": "report"}, "")


def test_a_plan_runs_no_container_to_choose_the_build(project, plan_env, monkeypatch):
    monkeypatch.setattr(deploy_common, "docker_problem", lambda: "")
    monkeypatch.setattr(deploy_common, "emulate", lambda image_platform: pytest.fail("ran"))
    app = {"name": "report", "dir": add_app(project, "report"), "docker_build": "auto",
           "docker_build_from": ""}
    with config.plan_only():
        assert deploy_common.build_in_cloud(app, "Cloud Build", "linux/amd64") is False


@pytest.mark.parametrize("setting, otherwise", [
    ("auto", "else in the cloud with CodeBuild."),
    ("local", "else stop. docker-build: local in pdt.yml allows only Docker."),
])
def test_a_plan_says_the_build_place_depends_on_what_docker_can_build(
        project, plan_env, monkeypatch, capsys, setting, otherwise):
    monkeypatch.setattr(deploy_common, "docker_problem", lambda: "")
    monkeypatch.setattr(deploy_common, "emulate", lambda image_platform: pytest.fail("ran"))
    app = {"name": "report", "dir": add_app(project, "report"), "docker_build": setting,
           "docker_build_from": "pdt.yml"}
    with config.plan_only():
        assert deploy_common.build_in_cloud(app, "CodeBuild", "linux/arm64") is False
    assert plain([capsys.readouterr().out.replace("\n", " ").strip()]) == [
        "Docker found. The deploy will build the image with Docker on this computer if it can "
        f"build linux/arm64 images, {otherwise}"]


def test_an_azure_plan_does_not_change_the_active_subscription(project, plan_env, monkeypatch):
    reads = {("account", "show"): {"id": "sub-a", "name": "a"},
             ("account", "list", "--all"): [{"id": "sub-b", "name": "b"}]}
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: reads[args])
    monkeypatch.setattr(deploy_azure, "run_quiet", lambda *args, **kwargs: pytest.fail("changed"))
    with config.plan_only(), pytest.raises(SystemExit):
        deploy_azure.preflight({"name": "report"}, {"subscription": "sub-b"})


def test_a_plan_installs_no_powershell_and_no_google_cloud_cli(tmp_path, plan_env, monkeypatch):
    monkeypatch.setattr(pwsh.shutil, "which", lambda name: None)
    monkeypatch.setattr(pwsh, "install_dir", lambda windows=False: tmp_path / "pwsh")
    monkeypatch.setattr(pwsh, "download_pwsh", lambda key: pytest.fail("installed"))
    monkeypatch.setattr(gcloud_sdk.shutil, "which", lambda name: None)
    monkeypatch.setattr(gcloud_sdk, "SDK_DIR", tmp_path / "gcloud")
    monkeypatch.setattr(gcloud_sdk, "LOCAL_GCLOUD", tmp_path / "gcloud" / "gcloud")
    monkeypatch.setattr(gcloud_sdk, "download_sdk", lambda key: pytest.fail("installed"))
    with config.plan_only():
        with pytest.raises(pwsh.PwshError, match="a plan installs nothing"):
            pwsh.ensure_pwsh()
        with pytest.raises(gcloud_sdk.GcloudError, match="a plan installs nothing"):
            gcloud_sdk.ensure_gcloud()


@pytest.mark.parametrize("suffix", ["", ".previous"])
def test_a_plan_uses_a_local_powershell_and_google_cloud_cli_of_another_version(tmp_path, plan_env,
                                                                                monkeypatch, suffix):
    monkeypatch.setattr(pwsh.shutil, "which", lambda name: None)
    monkeypatch.setattr(pwsh, "install_dir", lambda windows=False: tmp_path / "pwsh")
    monkeypatch.setattr(pwsh, "download_pwsh", lambda key: pytest.fail("installed"))
    monkeypatch.setattr(gcloud_sdk.shutil, "which", lambda name: None)
    monkeypatch.setattr(gcloud_sdk, "SDK_DIR", tmp_path / "gcloud")
    monkeypatch.setattr(gcloud_sdk, "LOCAL_GCLOUD", tmp_path / "gcloud" / "google-cloud-sdk" / "bin" / "gcloud")
    monkeypatch.setattr(gcloud_sdk, "GCLOUD_BIN", "gcloud")
    monkeypatch.setattr(gcloud_sdk, "download_sdk", lambda key: pytest.fail("installed"))
    local_pwsh = tmp_path / f"pwsh{suffix}" / pwsh.local_pwsh().name
    local_gcloud = tmp_path / f"gcloud{suffix}" / "google-cloud-sdk" / "bin" / "gcloud"
    for path in (local_pwsh, local_gcloud):
        path.parent.mkdir(parents=True)
        path.write_text("")
    before = sorted(tmp_path.rglob("*"))
    with config.plan_only():
        assert pwsh.ensure_pwsh() == str(local_pwsh)
        assert gcloud_sdk.ensure_gcloud() == str(local_gcloud)
    assert sorted(tmp_path.rglob("*")) == before


def test_a_destroy_plan_starts_the_provider_with_plan_where_the_app_is_recorded(
        aws_project, monkeypatch, capsys):
    add_app(aws_project, "report", "schedule: daily\n")
    recorded = {"provider": "aws", "region": "us-east-1"}
    config.mark_deployed("report", recorded)
    seen = []
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: seen.append(
        (args, kwargs, config.unsaved())) or 0)
    monkeypatch.setattr(deploy, "provider_step", lambda *args, **kwargs: pytest.fail("destroyed"))

    assert deploy.destroy("report", plan_only=True) == 0

    assert seen == [(("aws", "destroy", "report", True, ["--plan"]), {"platform": recorded}, {})]
    assert config.deployed_platform("report") == recorded
    assert config.unsaved() is None
    out = capsys.readouterr().out
    assert "nothing was deleted" in out and "pdt destroy report --yes" in out


@pytest.mark.parametrize("flags", [["--all"], ["--yes"]])
def test_a_destroy_plan_shows_one_app_and_takes_no_destroy_flag(aws_project, monkeypatch, capsys,
                                                                flags):
    add_app(aws_project, "report", "schedule: daily\n")
    monkeypatch.setattr(deploy, "destroy", lambda *args, **kwargs: pytest.fail("destroyed"))
    app = [] if flags == ["--all"] else ["report"]
    monkeypatch.setattr("sys.argv", ["pdt", "destroy", *app, "--plan", *flags])
    assert cli.main() == 1
    assert "--plan shows what one app's destroy deletes" in capsys.readouterr().out
