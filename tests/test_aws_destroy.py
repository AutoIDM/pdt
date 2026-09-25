import pytest

from pdt import deploy_aws_batch
from pdt.deploy_aws_batch import (
    COMPUTE_ENVIRONMENT, JOB_QUEUE, ensure_shared, legacy_fargate_cleanup, remove,
    resource_names, shared_unused_after,
)

NAMES = resource_names("my-app")


def definition(name):
    return {"jobDefinitionName": name, "revision": 1, "status": "ACTIVE",
            "jobDefinitionArn": f"arn:job-definition/{name}:1", "tags": {"managed-by": "pdt"}}


MANAGED = {"managed-by": "pdt"}


def shared_item(resource, state="ENABLED", status="VALID", tags=MANAGED):
    name_key = resource.param + "Name"
    return {name_key: "pdt", resource.arn: f"arn:{resource.param}/pdt", "status": status,
            "state": state, "tags": dict(tags)}


class FakeBatch:
    """Batch as it behaves: an update flips the state, a delete leaves the item listed as DELETED."""

    def __init__(self, definitions=(), queue=True, environment=True, state="ENABLED",
                 status="VALID", tags=MANAGED):
        self.definitions = list(definitions)
        self.items = {
            JOB_QUEUE.key: [shared_item(JOB_QUEUE, state, status, tags)] if queue else [],
            COMPUTE_ENVIRONMENT.key: [shared_item(COMPUTE_ENVIRONMENT, state, status, tags)]
            if environment else [],
        }
        self.calls = []

    def describe_job_definitions(self, **kwargs):
        return {"jobDefinitions": self.definitions}

    def describe_job_queues(self, jobQueues):
        return {JOB_QUEUE.key: self.items[JOB_QUEUE.key]}

    def describe_compute_environments(self, computeEnvironments):
        return {COMPUTE_ENVIRONMENT.key: self.items[COMPUTE_ENVIRONMENT.key]}

    def update_job_queue(self, jobQueue, state):
        self.calls.append(("update_job_queue", state))
        self.items[JOB_QUEUE.key][0]["state"] = state

    def delete_job_queue(self, jobQueue):
        self.calls.append(("delete_job_queue",))
        self.items[JOB_QUEUE.key][0]["status"] = "DELETED"

    def update_compute_environment(self, computeEnvironment, state):
        self.calls.append(("update_compute_environment", state))
        self.items[COMPUTE_ENVIRONMENT.key][0]["state"] = state

    def delete_compute_environment(self, computeEnvironment):
        self.calls.append(("delete_compute_environment",))
        self.items[COMPUTE_ENVIRONMENT.key][0]["status"] = "DELETED"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(deploy_aws_batch, "BATCH_WAIT_DELAYS", (0, 0))


@pytest.mark.parametrize("others, queue, environment, expected", [
    ([], True, True, [JOB_QUEUE, COMPUTE_ENVIRONMENT]),
    (["pdt-other"], True, True, []),
    ([], False, True, [COMPUTE_ENVIRONMENT]),
    ([], False, False, []),
    (["not-pdt"], True, True, [JOB_QUEUE, COMPUTE_ENVIRONMENT]),
])
def test_the_shared_queue_and_environment_go_only_when_no_other_pdt_app_remains(
        others, queue, environment, expected):
    batch = FakeBatch([definition("pdt-my-app"), *map(definition, others)], queue, environment)
    assert shared_unused_after(batch, "pdt-my-app") == expected


@pytest.mark.parametrize("resource", [JOB_QUEUE, COMPUTE_ENVIRONMENT])
def test_remove_disables_deletes_and_treats_the_deleted_listing_as_gone(resource):
    batch = FakeBatch()
    remove(batch, resource)
    assert batch.calls == [(resource.update, "DISABLED"), (resource.delete,)]


def test_remove_still_deletes_an_invalid_queue():
    batch = FakeBatch(status="INVALID")
    remove(batch, JOB_QUEUE)
    assert batch.calls == [("update_job_queue", "DISABLED"), ("delete_job_queue",)]


def test_remove_of_an_absent_queue_does_nothing():
    batch = FakeBatch(queue=False)
    remove(batch, JOB_QUEUE)
    assert batch.calls == []


def test_a_queue_still_listed_as_deleted_is_not_a_shared_resource_to_remove():
    batch = FakeBatch([definition("pdt-my-app")], environment=False, status="DELETED")
    assert shared_unused_after(batch, "pdt-my-app") == []


def test_an_untagged_queue_is_left_alone_by_destroy():
    batch = FakeBatch([definition("pdt-my-app")], environment=False, tags={})
    assert shared_unused_after(batch, "pdt-my-app") == []


def test_ensure_shared_reads_an_enabled_queue_without_touching_it():
    batch = FakeBatch()
    assert ensure_shared(batch, JOB_QUEUE, lambda: batch.calls.append("create")) == "arn:jobQueue/pdt"
    assert batch.calls == []


def test_ensure_shared_re_enables_a_disabled_queue():
    batch = FakeBatch(state="DISABLED")
    assert ensure_shared(batch, JOB_QUEUE, lambda: batch.calls.append("create")) == "arn:jobQueue/pdt"
    assert batch.calls == [("update_job_queue", "ENABLED")]


def test_ensure_shared_creates_a_queue_that_is_absent_or_still_listed_as_deleted():
    for batch in (FakeBatch(queue=False), FakeBatch(status="DELETED")):
        def create(batch=batch):
            batch.calls.append("create")
            batch.items[JOB_QUEUE.key] = [shared_item(JOB_QUEUE)]

        assert ensure_shared(batch, JOB_QUEUE, create) == "arn:jobQueue/pdt"
        assert batch.calls == ["create"]


def test_ensure_shared_waits_for_a_created_queue_to_be_listed():
    batch = FakeBatch(queue=False)
    describes = []
    original = batch.describe_job_queues

    def describe_job_queues(jobQueues):
        describes.append(1)
        if len(describes) == 2:
            batch.items[JOB_QUEUE.key] = [shared_item(JOB_QUEUE)]
        return original(jobQueues)

    batch.describe_job_queues = describe_job_queues
    assert ensure_shared(batch, JOB_QUEUE, lambda: None) == "arn:jobQueue/pdt"


def test_ensure_shared_refuses_an_invalid_environment_and_an_unmanaged_one():
    with pytest.raises(SystemExit):
        ensure_shared(FakeBatch(status="INVALID"), COMPUTE_ENVIRONMENT, lambda: None)
    with pytest.raises(SystemExit):
        ensure_shared(FakeBatch(tags={}), COMPUTE_ENVIRONMENT, lambda: None)


class FakeEcs:
    def __init__(self, families=(), cluster=True, running=False):
        self.families = list(families)
        self.cluster = cluster
        self.running = running
        self.deregistered = []
        self.deleted_clusters = []

    def list_task_definitions(self, familyPrefix, status):
        arns = [f"arn:aws:ecs:us-east-1:1:task-definition/{family}:1"
                for family in self.families if family.startswith(familyPrefix)]
        return {"taskDefinitionArns": arns}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": [{"key": "managed-by", "value": "pdt"}]}

    def deregister_task_definition(self, taskDefinition):
        self.deregistered.append(taskDefinition)

    def describe_clusters(self, clusters):
        return {"clusters": [{"status": "ACTIVE"}] if self.cluster else []}

    def list_tasks(self, cluster):
        return {"taskArns": ["arn:task"] if self.running else []}

    def list_task_definition_families(self, familyPrefix, status):
        return {"families": [family for family in self.families
                             if family.startswith(familyPrefix)]}

    def delete_cluster(self, cluster):
        self.deleted_clusters.append(cluster)


class FakeLogs:
    def __init__(self, groups=()):
        self.groups = list(groups)

    def describe_log_groups(self, logGroupNamePrefix):
        return {"logGroups": [{"logGroupName": name} for name in self.groups
                              if name.startswith(logGroupNamePrefix)]}


class FakeIam:
    def __init__(self, roles=()):
        self.roles = set(roles)

    def get_role(self, RoleName):
        if RoleName not in self.roles:
            error = type("ClientError", (Exception,), {})()
            error.response = {"Error": {"Code": "NoSuchEntity"}}
            raise error
        return {"Role": {"RoleName": RoleName}}


def lines(plan):
    return [line for line, _action in plan]


def test_no_fargate_leftovers_means_no_legacy_plan_lines():
    assert legacy_fargate_cleanup(FakeEcs(cluster=False), FakeLogs(), FakeIam(), NAMES) == []


def test_every_fargate_leftover_of_the_app_gets_its_own_plan_line():
    ecs = FakeEcs(families=["pdt-my-app"])
    plan = legacy_fargate_cleanup(
        ecs, FakeLogs(["/ecs/pdt-my-app"]), FakeIam(["pdt-my-app-task"]), NAMES)
    assert lines(plan) == [
        "deregister 1 tagged ECS task definition(s) in family pdt-my-app "
        "(older Fargate deployment)",
        "delete tagged log group /ecs/pdt-my-app (older Fargate deployment)",
        "delete tagged IAM role pdt-my-app-task (older Fargate deployment)",
        "delete ECS cluster pdt (older Fargate deployment, no other apps use it)",
    ]
    plan[0][1]()
    plan[3][1]()
    assert ecs.deregistered == ["arn:aws:ecs:us-east-1:1:task-definition/pdt-my-app:1"]
    assert ecs.deleted_clusters == ["pdt"]


def test_a_task_definition_of_another_app_with_the_same_prefix_is_not_this_apps():
    ecs = FakeEcs(families=["pdt-my-app-two"])
    plan = legacy_fargate_cleanup(ecs, FakeLogs(), FakeIam(), NAMES)
    assert lines(plan) == []


def test_the_ecs_cluster_stays_while_another_app_still_runs_on_fargate():
    ecs = FakeEcs(families=["pdt-my-app", "pdt-other"])
    plan = legacy_fargate_cleanup(ecs, FakeLogs(), FakeIam(), NAMES)
    assert lines(plan) == [
        "deregister 1 tagged ECS task definition(s) in family pdt-my-app "
        "(older Fargate deployment)"]


def test_the_ecs_cluster_stays_while_a_task_is_running():
    ecs = FakeEcs(running=True)
    assert lines(legacy_fargate_cleanup(ecs, FakeLogs(), FakeIam(), NAMES)) == []
