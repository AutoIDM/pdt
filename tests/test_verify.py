import xml.etree.ElementTree as ET

from inventory import Resource, classify
from verify import junit_tree, verify, wait_for, write_report

APPS = ["app-one", "app-two"]


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

    def run_pdt(self, verb, *args):
        self.calls.append((verb, *args[:1]))
        app = args[0] if args else None
        if verb == "deploy":
            if app in self.deploy_fails:
                return 1
            self.deploy(app)
        elif verb == "destroy":
            self.destroy(app)
        return 0

    def add(self, resource):
        self.resources[resource.id] = resource

    def deploy(self, app):
        self.add(Resource("job", f"pdt-{app}", tagged(), f"pdt-{app}"))
        self.add(Resource("registry", "pdt-registry", tagged(), "pdt-registry"))
        for resource in self.extra:
            self.add(resource)
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


def now(inventory, check):
    return check(inventory())


def run(cloud):
    return verify(cloud.apps, cloud.run_pdt, cloud.inventory,
                  report=lambda step: None, wait=now)


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
        "no resource of these apps exists before deploy",
        "deploy app-one",
        "deploy app-two",
        "health app-one",
        "runs app-one",
        "health app-two",
        "runs app-two",
        "every resource is tagged",
        "every resource has an owner",
        "destroy app-one",
        "app-one resources are gone",
        "other apps are untouched after destroy app-one",
        "destroy app-two",
        "app-two resources are gone",
        "other apps are untouched after destroy app-two",
        "account holds what it held before deploy",
    ]


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
    assert [step.name for step in failed(steps)] == ["account holds what it held before deploy"]
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

    steps = verify(cloud.apps, run_pdt, cloud.inventory, report=lambda step: None, wait=now)
    assert [step.name for step in failed(steps)] == ["health app-one"]
    assert failed(steps)[0].detail == "pdt health app-one exited 1"
    assert cloud.resources == {}


def test_an_exception_after_deploy_destroys_every_app():
    cloud = FakeCloud()

    def explode():
        if cloud.deployed:
            raise RuntimeError("the cloud said no")
        return []

    steps = verify(cloud.apps, cloud.run_pdt, explode,
                   report=lambda step: None, wait=now)
    assert [step.name for step in failed(steps)] == ["unexpected error"]
    assert "the cloud said no" in failed(steps)[0].detail
    assert cloud.calls == [
        ("deploy", "app-one"), ("deploy", "app-two"),
        ("health", "app-one"), ("runs", "app-one"),
        ("health", "app-two"), ("runs", "app-two"),
        ("destroy", "app-one"), ("destroy", "app-two"),
    ]
    assert cloud.resources == {}


def test_a_leftover_of_these_apps_is_left_untouched():
    cloud = FakeCloud()
    cloud.deploy("app-one")
    before = dict(cloud.resources)
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["no resource of these apps exists before deploy"]
    assert cloud.calls == []
    assert cloud.resources == before


def test_another_apps_resources_are_ignored_and_kept():
    cloud = FakeCloud()
    cloud.keep_shared = True
    other = [Resource("job", "pdt-payroll", tagged(), "pdt-payroll"),
             Resource("plan", "legacy-plan", {}, "legacy-plan"),
             Resource("registry", "pdt-registry", tagged(), "pdt-registry")]
    for resource in other:
        cloud.add(resource)
    steps = run(cloud)
    assert failed(steps) == []
    assert sorted(cloud.resources) == ["legacy-plan", "pdt-payroll", "pdt-registry"]


def test_removing_a_resource_that_existed_before_fails():
    cloud = FakeCloud()
    cloud.add(Resource("job", "pdt-payroll", tagged(), "pdt-payroll"))
    cloud.also_removes = {"app-one": ["pdt-payroll"]}
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["other apps are untouched after destroy app-one"]
    assert "pdt-payroll is gone" in failed(steps)[0].detail


def test_an_untagged_leftover_of_these_apps_counts():
    cloud = FakeCloud()
    cloud.add(Resource("job", "pdt-app-two", {}, "pdt-app-two"))
    steps = run(cloud)
    assert [step.name for step in failed(steps)] == ["no resource of these apps exists before deploy"]
    assert cloud.calls == []


def test_an_initial_inventory_exception_leaves_the_account_untouched():
    cloud = FakeCloud()
    cloud.deploy("app-one")
    before = dict(cloud.resources)

    def explode():
        raise RuntimeError("the cloud said no")

    steps = verify(cloud.apps, cloud.run_pdt, explode,
                   report=lambda step: None, wait=now)
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
    import inventory

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
