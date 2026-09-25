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


class FakeBatch:
    def __init__(self, definitions=(), queue=True, environment=True, state="ENABLED"):
        self.definitions = list(definitions)
        self.items = {"jobQueues": [{"jobQueueName": "pdt", "jobQueueArn": "arn:queue/pdt",
                                     "status": "VALID", "state": state}] if queue else [],
                      "computeEnvironments": [{"computeEnvironmentName": "pdt",
                                               "status": "VALID",
                                               "state": "ENABLED"}] if environment else []}
        self.calls = []

    def describe_job_definitions(self, **kwargs):
        return {"jobDefinitions": self.definitions}

    def describe_job_queues(self, jobQueues):
        return {"jobQueues": self.items["jobQueues"]}

    def describe_compute_environments(self, computeEnvironments):
        return {"computeEnvironments": self.items["computeEnvironments"]}

    def update_job_queue(self, jobQueue, state):
        self.calls.append(("update_job_queue", state))
        self.items["jobQueues"][0]["state"] = state

    def delete_job_queue(self, jobQueue):
        self.calls.append(("delete_job_queue",))
        self.items["jobQueues"] = []


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


def test_remove_disables_then_deletes_then_waits_for_the_queue_to_disappear(monkeypatch):
    monkeypatch.setattr(deploy_aws_batch, "BATCH_WAIT_DELAYS", (0,))
    batch = FakeBatch()
    remove(batch, JOB_QUEUE)
    assert batch.calls == [("update_job_queue", "DISABLED"), ("delete_job_queue",)]


def test_remove_of_an_absent_queue_does_nothing():
    batch = FakeBatch(queue=False)
    remove(batch, JOB_QUEUE)
    assert batch.calls == []


def test_ensure_shared_reads_an_enabled_queue_without_touching_it():
    batch = FakeBatch()
    assert ensure_shared(batch, JOB_QUEUE, lambda: batch.calls.append("create")) == "arn:queue/pdt"
    assert batch.calls == []


def test_ensure_shared_re_enables_a_disabled_queue(monkeypatch):
    monkeypatch.setattr(deploy_aws_batch, "BATCH_WAIT_DELAYS", (0,))
    batch = FakeBatch(state="DISABLED")
    assert ensure_shared(batch, JOB_QUEUE, lambda: batch.calls.append("create")) == "arn:queue/pdt"
    assert batch.calls == [("update_job_queue", "ENABLED")]


def test_ensure_shared_creates_an_absent_queue_and_waits_for_it():
    batch = FakeBatch(queue=False)

    def create():
        batch.calls.append("create")
        batch.items["jobQueues"] = [{"jobQueueName": "pdt", "jobQueueArn": "arn:queue/pdt",
                                     "status": "VALID", "state": "ENABLED"}]

    assert ensure_shared(batch, JOB_QUEUE, create) == "arn:queue/pdt"
    assert batch.calls == ["create"]


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
