import json
import threading

import pytest
from botocore.exceptions import ClientError

from pdt import config, deploy_aws
from pdt import deploy_aws_batch as batch_deploy
from pdt.deploy import DECLINED
from pdt.deploy_aws import PREFLIGHT_PASSES, PREFLIGHT_VALID_SECONDS, preflight
from pdt.deploy_aws_batch import DEPLOYER_ACTIONS
from test_aws_deployer_policy import ACCOUNT, BROAD_POLICY, CALLER, REGION, PolicyIam
from test_aws_security_group import FakeBatch, FakeEc2


class Sts:
    def __init__(self, account=ACCOUNT, arn=CALLER):
        self.identity = {"Account": account, "Arn": arn}

    def get_caller_identity(self):
        return self.identity


class CountingIam(PolicyIam):
    def __init__(self, granted=BROAD_POLICY):
        super().__init__(granted)
        self.calls = 0

    def simulate_principal_policy(self, **kwargs):
        self.calls += 1
        return super().simulate_principal_policy(**kwargs)


def check(iam, sts=None, account=ACCOUNT, region=REGION, actions=DEPLOYER_ACTIONS):
    _account, checked = preflight(sts or Sts(account), iam, account, region, actions)
    checked()


def test_a_passed_check_is_not_repeated(project):
    iam = CountingIam()
    check(iam)
    first = iam.calls
    check(iam)
    assert first > 0
    assert iam.calls == first


@pytest.mark.parametrize("changed", [
    {"sts": Sts(arn=f"arn:aws:iam::{ACCOUNT}:user/someone-else")},
    {"account": "210987654321"},
    {"region": "eu-west-1"},
    {"actions": [action for action in DEPLOYER_ACTIONS if not action.startswith("ecs:")]},
])
def test_a_different_principal_account_region_or_policy_is_checked_again(project, changed):
    iam = CountingIam()
    check(iam)
    first = iam.calls
    check(iam, **changed)
    assert iam.calls > first


def test_a_pass_older_than_a_day_is_checked_again(project, monkeypatch):
    iam = CountingIam()
    check(iam)
    first = iam.calls
    now = deploy_aws.time.time()
    monkeypatch.setattr(deploy_aws.time, "time", lambda: now + PREFLIGHT_VALID_SECONDS + 1)
    check(iam)
    assert iam.calls == 2 * first


def test_a_failed_check_is_not_cached(project):
    iam = CountingIam({"Version": "2012-10-17", "Statement": []})
    with pytest.raises(SystemExit):
        check(iam)
    assert PREFLIGHT_PASSES not in config.read_state()
    first = iam.calls
    with pytest.raises(SystemExit):
        check(iam)
    assert iam.calls == 2 * first


def test_access_denied_during_a_deploy_drops_the_cached_pass(project, monkeypatch, capsys):
    check(CountingIam())
    assert config.read_state()[PREFLIGHT_PASSES]

    def denied(app, assume_yes):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "CreateRole")

    class NoLogin:
        def __init__(self, **kwargs):
            pass

        def client(self, *args, **kwargs):
            raise RuntimeError("no login")

    monkeypatch.setattr(deploy_aws, "load_app", lambda name: {
        "name": name, "platform": {"account": ACCOUNT, "region": REGION}})
    monkeypatch.setattr(batch_deploy, "deploy", denied)
    monkeypatch.setattr(deploy_aws.boto3, "Session", NoLogin)
    monkeypatch.setattr(deploy_aws.sys, "argv", ["deploy_aws.py", "deploy", "my-app"])
    assert deploy_aws.main() == 1
    assert PREFLIGHT_PASSES not in config.read_state()
    assert "AccessDenied" in capsys.readouterr().out


def test_the_check_runs_on_a_thread_until_the_deploy_waits_for_it(project):
    release = threading.Event()

    class SlowIam(CountingIam):
        def simulate_principal_policy(self, **kwargs):
            release.wait(5)
            return super().simulate_principal_policy(**kwargs)

    iam = SlowIam()
    _account, checked = preflight(Sts(), iam, ACCOUNT, REGION, DEPLOYER_ACTIONS)
    assert iam.calls == 0
    release.set()
    checked()
    assert iam.calls > 0
    assert json.loads((project / ".pdt" / "state").read_text())[PREFLIGHT_PASSES]


@pytest.fixture
def deploy_events(monkeypatch):
    events = []

    def checked():
        events.append("permissions checked")

    for name, value in {
        "build_in_cloud": lambda app, builder, image_platform: False,
        "ensure_session": lambda app: None,
        "aws_settings": lambda app, session: (ACCOUNT, REGION),
        "preflight": lambda *args: (ACCOUNT, checked),
        "gather_secrets": lambda app: {},
        "resource_exists": lambda *args, **kwargs: events.append("state read") or False,
        "cost_estimate_for": lambda *args: events.append("prices fetched"),
        "confirm": lambda *args: events.append("asked to proceed") or False,
        "batch_clients": lambda session: {
            "ec2": FakeEc2(), "batch": FakeBatch(), "logs": None, "scheduler": None,
            "secretsmanager": None, "sts": None, "iam": None},
    }.items():
        monkeypatch.setattr(batch_deploy, name, value)
    monkeypatch.setattr(batch_deploy.console, "warn", lambda message: None)
    app = {"name": "my-app", "storage": False, "schedule": "0 6 * * *",
           "timezone": "UTC", "dir": ".", "pause": False}
    return events, lambda: batch_deploy.deploy(app, assume_yes=False)


def test_the_deploy_waits_for_the_check_after_the_reads_and_before_the_question(deploy_events):
    events, deploy = deploy_events
    assert deploy() == DECLINED
    assert events[-3:] == ["prices fetched", "permissions checked", "asked to proceed"]
    assert "state read" in events[:-3]


def test_a_failed_check_stops_the_deploy_before_the_question(deploy_events, monkeypatch):
    events, deploy = deploy_events

    def failed():
        raise SystemExit(1)

    monkeypatch.setattr(batch_deploy, "preflight", lambda *args: (ACCOUNT, failed))
    with pytest.raises(SystemExit):
        deploy()
    assert "asked to proceed" not in events
