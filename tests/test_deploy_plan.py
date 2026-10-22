"""`pdt deploy APP --plan` prints the plan and the cost estimate, then changes nothing."""

import json
from pathlib import Path

import pytest

from conftest import add_app
from pdt import cli, config, deploy, deploy_google_cloud, regions


@pytest.fixture
def plan_env(monkeypatch):
    monkeypatch.delenv(config.PLAN_ENV, raising=False)


@pytest.fixture
def aws_project(project, plan_env):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n")
    return project


def test_a_plan_keeps_a_platform_value_out_of_the_file_and_in_the_merged_app(aws_project):
    add_app(aws_project, "report", "schedule: daily\n")
    config.plan_only()
    config.save_platform_key(config.merged_app("report"), "account", "000000000000")
    assert (aws_project / "pdt.yml").read_text() == "platform:\n  provider: aws\n"
    assert config.merged_app("report")["platform"]["account"] == "000000000000"


def test_a_plan_keeps_the_schedule_out_of_the_app_file(aws_project):
    folder = add_app(aws_project, "report")
    config.plan_only()
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

    assert seen == [(("aws", "deploy", "report", False, ["--plan"]),
                     {"schedule": "daily", "platform": {"region": "us-east-1"}})]
    assert sorted(str(path) for path in Path(aws_project).rglob("*")) == before
    assert (aws_project / "pdt.yml").read_text() == "platform:\n  provider: aws\n"
    assert not config.is_deployed("report")
    out = capsys.readouterr().out
    assert "nothing was deployed" in out and "pdt deploy report --yes" in out


def test_the_plan_values_reach_the_provider_script(aws_project, monkeypatch):
    config.plan_only()
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
    assert "args.yes, args.plan)" in source


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

    assert module.deploy(app, False, plan_only=True) == 0

    assert enabled == [True]
    out = capsys.readouterr().out
    assert "Plan:" in out and "create Cloud Run job pdt-report" in out
