import datetime
import xml.etree.ElementTree as ET

import inventory
from inventory import Resource, classify
from verify import (fire_schedule, junit_tree, scheduled, verify, verify_local,
                    wait_for, write_report)

APPS = ["app-one", "app-two"]
FIRE = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)


def tagged(**extra):
    return {"managed-by": "pdt", **extra}


class FakeCloud:
    """A cloud account that gains resources on deploy and loses them on destroy."""

    def __init__(self, apps=APPS):
        self.apps = list(apps)
        self.deployed = []
        self.resources = {}
        self.calls = []
        self.also_removes = {}
        self.extra = []
        self.keep_shared = False
        self.deploy_fails = ()
        self.no_storage = {"app-two"}
        self.fired = {"app-two": ["run-1"]}
        self.redeploy_adds = None

    def run_pdt(self, verb, *args):
        self.calls.append((verb, *args[:1]))
        app = args[0] if args else None
        if verb == "storage":
            return 1 if app in self.no_storage else 0
        if verb == "deploy":
            if app in self.deploy_fails:
                return 1
            if app in self.deployed and self.redeploy_adds:
                self.add(self.redeploy_adds)
            self.deploy(app)
        elif verb == "destroy":
            self.destroy(app)
        return 0

    def runs(self, app, _since):
        return self.fired.get(app, [])

    def add(self, resource):
        self.resources[resource.id] = resource

    def deploy(self, app):
        self.add(Resource("job", f"pdt-{app}", tagged(), f"pdt-{app}"))
        self.add(Resource("registry", "pdt-registry", tagged(), "pdt-registry"))
        for resource in self.extra:
            self.add(resource)
        if app not in self.deployed:
            self.deployed.append(app)

    def destroy(self, app):
        if app not in self.deployed:
            return
        self.deployed.remove(app)
        self.resources.pop(f"pdt-{app}", None)
        for identifier in self.also_removes.get(app, ()):
            self.resources.pop(identifier, None)
        if not self.deployed and not self.keep_shared:
            self.resources.pop("pdt-registry", None)
            for resource in self.extra:
                self.resources.pop(resource.id, None)

    def inventory(self):
        return list(self.resources.values())


def now(inventory, check, _deadline=None, _poll=None):
    return check(inventory())


def run(cloud, inventory=None):
    return verify(cloud.apps, cloud.run_pdt, inventory or cloud.inventory,
                  cloud.runs, FIRE, report=lambda step: None, wait=now)


def failed(steps):
    return [step for step in steps if not step.ok]


def test_classify_needs_the_managed_by_tag():
    assert classify(Resource("job", "pdt-app-one"), APPS) == "untagged"


def test_classify_prefers_the_pdt_app_tag():
    resource = Resource("job", "abc123", tagged(**{"pdt-app": "app-two"}))
    assert classify(resource, APPS) == "app-two"


def test_classify_falls_back_to_the_id():
    assert classify(Resource("job", "arn:pdt-app-one:1", tagged()), APPS) == "app-one"


def test_classify_falls_back_to_the_name():
    resource = Resource("job", "abc123", tagged(), "pdt-app-two-env")
    assert classify(resource, APPS) == "app-two"


def test_classify_calls_an_ambiguous_name_shared():
    resource = Resource("group", "pdt-app-one-pdt-app-two", tagged())
    assert classify(resource, APPS) == "shared"


def test_classify_calls_an_unmatched_name_shared():
    assert classify(Resource("registry", "pdt-registry", tagged()), APPS) == "shared"


def test_happy_path_leaves_the_account_empty():
    cloud = FakeCloud()
    steps = run(cloud)
    assert failed(steps) == []
    assert cloud.resources == {}
    assert [step.name for step in steps] == [
        "account is empty before deploy",
        "deploy app-one",
        "deploy app-two",
        "health",
        "runs app-one",
        "runs app-two",
        "every resource is tagged",
        "every resource has an owner",
        "app-two ran on schedule",
        "storage ls app-one",
        "storage ls app-two is refused",
        "deploy app-one again",
        "nothing changed after redeploy app-one",
        "destroy app-one",
        "app-one resources are gone",
        "other apps are untouched after destroy app-one",
        "destroy app-two",
        "app-two resources are gone",
        "other apps are untouched after destroy app-two",
        "account is empty after destroy",
        "destroy app-one again",
        "account is still empty",
    ]


def test_a_schedule_that_never_fires_fails_and_cleans_up():
    cloud = FakeCloud()
    cloud.fired = {}
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["app-two ran on schedule"]
    assert "no successful run of app-two" in failed(steps)[0].detail
    assert cloud.resources == {}


def test_a_redeploy_that_changes_the_account_fails():
    cloud = FakeCloud()
    cloud.redeploy_adds = Resource("role", "pdt-app-one-extra", tagged())
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["nothing changed after redeploy app-one"]
    assert "pdt-app-one-extra appeared" in failed(steps)[0].detail


def test_storage_must_be_refused_when_it_is_off():
    cloud = FakeCloud()
    cloud.no_storage = set()
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["storage ls app-two is refused"]


def test_fire_schedule_rounds_up_to_the_next_whole_minute():
    now = datetime.datetime(2026, 1, 1, 23, 52, 30, tzinfo=datetime.timezone.utc)
    cron, fire = fire_schedule(now, lead_minutes=10)
    assert cron == "3 0 * * *"
    assert fire == datetime.datetime(2026, 1, 2, 0, 3, tzinfo=datetime.timezone.utc)
    assert fire_schedule(now.replace(second=0), lead_minutes=10)[0] == "2 0 * * *"


def test_scheduled_writes_the_cron_and_restores_the_file(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("storage: false\n")
    with scheduled(tmp_path, "3 0 * * *"):
        assert path.read_text() == 'storage: false\nschedule: "3 0 * * *"\n'
    assert path.read_text() == "storage: false\n"


def test_the_local_scenario_runs_the_commands_that_need_no_account():
    calls = []

    def run_pdt(*args, cwd=None):
        calls.append((args, cwd is not None))
        return 1 if args == ("validate",) and cwd is not None else 0

    steps = verify_local(run_pdt, report=lambda step: None)
    assert failed(steps) == []
    assert [step.name for step in steps] == [
        "validate", "list", "examples", "completion --script bash", "run windows-a",
        "init --yes in a new folder", "new report --from hello-world in a new folder",
        "list in a new folder", "validate in a new folder reports the missing provider",
    ]
    assert [folder for _args, folder in calls] == [False] * 5 + [True] * 4


def test_aws_runs_count_streams_that_logged_the_greeting(monkeypatch):
    events = {"events": [
        {"logStreamName": "ecs/one", "message": '{"msg": "Hello from pdt."}'},
        {"logStreamName": "ecs/two", "message": "Traceback"},
    ]}
    monkeypatch.setattr(inventory, "aws", lambda *args: events)
    assert inventory.aws_runs({"region": "us-east-1"}, "app-two", FIRE) == ["ecs/one"]

    def missing(*args):
        raise inventory.InventoryError("(ResourceNotFoundException) no group")

    monkeypatch.setattr(inventory, "aws", missing)
    assert inventory.aws_runs({"region": "us-east-1"}, "app-two", FIRE) == []


def test_azure_runs_count_succeeded_executions_since(monkeypatch):
    listed = [
        {"name": "ok", "properties": {"status": "Succeeded", "startTime": "2026-01-01T12:05:00Z"}},
        {"name": "bad", "properties": {"status": "Failed", "startTime": "2026-01-01T12:06:00Z"}},
        {"name": "old", "properties": {"status": "Succeeded", "startTime": "2026-01-01T11:00:00Z"}},
    ]
    monkeypatch.setattr(inventory, "az", lambda *args: listed)
    assert inventory.azure_runs({"resource_group": "pdt-verify"}, "app-two", FIRE) == ["ok"]


def test_google_cloud_runs_count_succeeded_executions_since(monkeypatch):
    listed = [
        {"metadata": {"name": "ok", "creationTimestamp": "2026-01-01T12:05:00Z"},
         "status": {"succeededCount": 1}},
        {"metadata": {"name": "bad", "creationTimestamp": "2026-01-01T12:06:00Z"},
         "status": {"failedCount": 1}},
        {"metadata": {"name": "old", "creationTimestamp": "2026-01-01T11:00:00Z"},
         "status": {"succeededCount": 1}},
    ]
    monkeypatch.setattr(inventory, "gcloud", lambda *args: listed)
    assert inventory.google_cloud_runs(
        {"region": "us-central1", "project": "p"}, "app-two", FIRE) == ["ok"]


def test_windows_runs_need_a_zero_result_since(monkeypatch):
    replies = iter([
        {"LastRunTime": "2026-01-01T12:05:00.0000000Z", "LastTaskResult": 0},
        {"LastRunTime": "2026-01-01T12:05:00.0000000Z", "LastTaskResult": 1},
        {"LastRunTime": "2026-01-01T11:00:00.0000000Z", "LastTaskResult": 0},
    ])
    monkeypatch.setattr(inventory, "run_json", lambda command: next(replies))
    assert inventory.windows_runs({}, "app-two", FIRE) == ["2026-01-01T12:05:00.0000000Z"]
    assert inventory.windows_runs({}, "app-two", FIRE) == []
    assert inventory.windows_runs({}, "app-two", FIRE) == []


def test_an_untagged_resource_fails():
    cloud = FakeCloud()
    cloud.extra = [Resource("plan", "pdt-plan", {}, "pdt-plan")]
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["every resource is tagged"]
    assert "pdt-plan" in failed(steps)[0].detail


def test_destroying_one_app_must_not_remove_another():
    cloud = FakeCloud()
    cloud.also_removes = {"app-one": ["pdt-app-two"]}
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["other apps are untouched after destroy app-one"]
    assert "pdt-app-two" in failed(steps)[0].detail


def test_a_shared_resource_left_behind_fails():
    cloud = FakeCloud()
    cloud.keep_shared = True
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["account is empty after destroy"]
    assert "pdt-registry" in failed(steps)[0].detail


def test_a_failure_destroys_every_app():
    cloud = FakeCloud()
    cloud.deploy_fails = ("app-two",)
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["deploy app-two"]
    assert cloud.calls[-2:] == [("destroy", "app-one"), ("destroy", "app-two")]
    assert cloud.resources == {}


def test_a_failing_health_check_fails_and_destroys_every_app():
    cloud = FakeCloud()

    def run_pdt(verb, *args):
        return 1 if verb == "health" else cloud.run_pdt(verb, *args)

    steps = verify(cloud.apps, run_pdt, cloud.inventory, cloud.runs, FIRE,
                   report=lambda step: None, wait=now)
    assert [step.name for step in failed(steps)] == ["health"]
    assert failed(steps)[0].detail == "pdt health exited 1"
    assert cloud.resources == {}


def test_an_exception_after_deploy_destroys_every_app():
    cloud = FakeCloud()

    def explode():
        if cloud.deployed:
            raise RuntimeError("the cloud said no")
        return []

    steps = run(cloud, explode)
    assert [step.name for step in failed(steps)] == ["unexpected error"]
    assert "the cloud said no" in failed(steps)[0].detail
    assert cloud.calls == [
        ("deploy", "app-one"), ("deploy", "app-two"),
        ("health",), ("runs", "app-one"), ("runs", "app-two"),
        ("destroy", "app-one"), ("destroy", "app-two"),
    ]
    assert cloud.resources == {}


def test_a_nonempty_account_is_left_untouched():
    cloud = FakeCloud()
    cloud.deploy("app-one")
    before = dict(cloud.resources)
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["account is empty before deploy"]
    assert cloud.calls == []
    assert cloud.resources == before


def test_an_initial_inventory_exception_leaves_the_account_untouched():
    cloud = FakeCloud()
    cloud.deploy("app-one")
    before = dict(cloud.resources)

    def explode():
        raise RuntimeError("the cloud said no")

    steps = run(cloud, explode)
    assert [step.name for step in failed(steps)] == ["unexpected error"]
    assert cloud.calls == []
    assert cloud.resources == before


def test_wait_for_retries_until_the_listing_catches_up():
    readings = [[Resource("job", "pdt-app-one", tagged())], []]
    slept = []
    clock = iter([0.0, 0.0, 10.0, 10.0])

    problems = wait_for(lambda: readings.pop(0),
                        lambda resources: [item.id for item in resources],
                        sleep=slept.append, clock=lambda: next(clock))
    assert problems == []
    assert slept == [10]


def test_wait_for_gives_up_at_the_deadline():
    slept = []
    clock = iter([0.0, 1000.0])
    problems = wait_for(lambda: [Resource("job", "pdt-app-one", tagged())],
                        lambda resources: [item.id for item in resources],
                        sleep=slept.append, clock=lambda: next(clock))
    assert problems == ["pdt-app-one"]
    assert slept == []


def test_the_junit_report_holds_one_testcase_per_step(tmp_path):
    cloud = FakeCloud()
    cloud.keep_shared = True
    steps = run(cloud)
    path = tmp_path / "verify-aws.xml"
    write_report(path, "aws", steps)

    suite = ET.parse(path).getroot()
    assert suite.tag == "testsuite"
    assert suite.get("name") == "verify:aws"
    assert suite.get("tests") == str(len(steps))
    assert suite.get("failures") == "1"
    cases = suite.findall("testcase")
    assert [case.get("name") for case in cases] == [step.name for step in steps]
    assert cases[-1].find("failure") is not None
    assert "pdt-registry" in cases[-1].find("failure").text


def test_the_junit_report_marks_a_clean_run(tmp_path):
    steps = run(FakeCloud())
    suite = junit_tree("azure", steps).getroot()
    assert suite.get("failures") == "0"
    assert suite.findall("testcase/failure") == []


def test_the_azure_inventory_covers_the_shared_environment_group(monkeypatch):
    groups = {"pdt-verify": [{"type": "Microsoft.App/jobs", "id": "/job", "name": "pdt-app-one"}],
              "pdt-shared": [{"type": "Microsoft.App/managedEnvironments", "id": "/env",
                              "name": "pdt-eastus2", "tags": tagged()}]}

    def az(*args):
        if args[0] == "group" and args[1] == "exists":
            return args[3] in groups
        if args[0] == "group":
            return {"id": f"/{args[3]}", "name": args[3], "tags": tagged()}
        return groups[args[3]]

    monkeypatch.setattr(inventory, "az", az)
    monkeypatch.setattr(inventory, "azure_deleted_vaults", list)
    found = inventory.azure_inventory({"resource_group": "pdt-verify", "environment": ""})
    assert [resource.name for resource in found] == [
        "pdt-verify", "pdt-app-one", "pdt-shared", "pdt-eastus2"]
    assert classify(found[3], APPS) == "shared"
    found = inventory.azure_inventory(
        {"resource_group": "pdt-verify", "environment": "pdt-shared/pdt-eastus2"})
    assert [resource.name for resource in found] == ["pdt-verify", "pdt-app-one"]


def test_a_retained_data_store_is_left_out_of_every_listing():
    from verify import listing

    kept = Resource("s3", "arn:aws:s3:::pdt-data-1", tagged() | {"pdt-lifecycle": "retain"})
    job = Resource("job", "pdt-app-one", tagged())
    assert listing(lambda: [kept, job]) == [job]
