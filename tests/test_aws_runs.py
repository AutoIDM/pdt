import sys
import types
from datetime import datetime, timezone

# deploy_aws imports boto3 at module level; these shapes need no AWS SDK.
if "botocore.exceptions" not in sys.modules:
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = type("ClientError", (Exception,), {})
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions
    sys.modules.setdefault("botocore", botocore)
    sys.modules["botocore.exceptions"] = exceptions

from pdt.deploy_aws_fargate import list_runs, read_lines, resolve, resource_names
from pdt.runs_cli import Run

NAMES = resource_names("my-app")
STREAM = f"ecs/{NAMES['family']}/task-1"


class FakeLogs:
    def __init__(self, streams=(), tail=(), pages=None, stream_pages=None):
        self.stream_pages = stream_pages or {None: (list(streams), None)}
        self.tail = list(tail)
        self.pages = pages or {}
        self.tails_read = []

    def describe_log_streams(self, nextToken=None, **kwargs):
        streams, token = self.stream_pages[nextToken]
        return {"logStreams": streams, **({"nextToken": token} if token else {})}

    def get_log_events(self, logGroupName, logStreamName, startFromHead, nextToken=None,
                       limit=None):
        if not startFromHead:
            self.tails_read.append(logStreamName)
            return {"events": self.tail}
        index = 0 if nextToken is None else nextToken
        events, token = self.pages[index]
        return {"events": events, "nextForwardToken": token}


class MissingLogs:
    def describe_log_streams(self, **kwargs):
        error = type("ClientError", (Exception,), {})()
        error.response = {"Error": {"Code": "ResourceNotFoundException"}}
        raise error


class FakeEcs:
    def __init__(self, running_task_ids=()):
        self.running = running_task_ids

    def list_tasks(self, cluster, desiredStatus):
        return {"taskArns": [f"arn:aws:ecs:us-east-1:1:task/pdt/{tid}" for tid in self.running]}


def stream(started_ms: int, ended_ms: int, name: str = STREAM) -> dict:
    return {"logStreamName": name, "firstEventTimestamp": started_ms,
            "lastEventTimestamp": ended_ms}


def resolved_run(logs, ecs=None):
    [run] = list_runs(logs, NAMES)
    resolve(logs, ecs or FakeEcs(), NAMES["log_group"], [run])
    return run


def test_a_missing_log_group_is_no_runs():
    assert list_runs(MissingLogs(), NAMES) == []


def test_list_runs_follows_every_page_and_reads_no_tail():
    logs = FakeLogs(stream_pages={None: ([stream(2, 3, "ecs/a/task-2")], "page-2"),
                                  "page-2": ([stream(0, 1, "ecs/a/task-1")], None)})
    found = list_runs(logs, NAMES)
    assert [run.id for run in found] == ["ecs/a/task-2", "ecs/a/task-1"]
    assert all(run.status == "running" and run.ended is None for run in found)
    assert logs.tails_read == []


def test_resolve_reads_tails_only_for_the_runs_it_is_given():
    logs = FakeLogs(stream_pages={None: ([stream(2, 3, "ecs/a/task-2"),
                                          stream(0, 1, "ecs/a/task-1")], None)},
                    tail=[{"message": "pdt: exit 0", "timestamp": 3}])
    newest, older = list_runs(logs, NAMES)
    resolve(logs, FakeEcs(), NAMES["log_group"], [newest])
    assert logs.tails_read == ["ecs/a/task-2"]
    assert newest.status == "succeeded"
    assert older.status == "running"


def test_started_and_ended_come_from_the_stream_and_its_last_event():
    logs = FakeLogs(streams=[stream(1_700_000_000_000, 1_700_000_010_000)],
                    tail=[{"message": "pdt: exit 0", "timestamp": 1_700_000_010_000}])
    run = resolved_run(logs)
    assert run.started == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert run.ended == datetime.fromtimestamp(1_700_000_010, tz=timezone.utc)


def test_a_zero_exit_marker_succeeds():
    logs = FakeLogs(streams=[stream(0, 1)], tail=[{"message": "pdt: exit 0", "timestamp": 1}])
    run = resolved_run(logs)
    assert run.status == "succeeded"
    assert run.exit_code == 0


def test_a_nonzero_exit_marker_fails():
    logs = FakeLogs(streams=[stream(0, 1)], tail=[{"message": "pdt: exit 1", "timestamp": 1}])
    run = resolved_run(logs)
    assert run.status == "failed"
    assert run.exit_code == 1


def test_no_marker_and_a_running_task_is_running():
    logs = FakeLogs(streams=[stream(0, 1)], tail=[{"message": "still going", "timestamp": 1}])
    run = resolved_run(logs, FakeEcs(running_task_ids=["task-1"]))
    assert run.status == "running"
    assert run.ended is None
    assert run.exit_code is None


def test_no_marker_and_no_running_task_fails():
    logs = FakeLogs(streams=[stream(0, 1)], tail=[{"message": "still going", "timestamp": 1}])
    assert resolved_run(logs).status == "failed"


def test_read_lines_pages_through_get_log_events_until_the_token_repeats():
    logs = FakeLogs(pages={
        0: ([{"message": "10:00:00 INFO   starting", "timestamp": 1_700_000_000_000}], 1),
        1: ([{"message": "pdt: exit 0", "timestamp": 1_700_000_001_000}], 1),
    })
    run = Run(STREAM, datetime.fromtimestamp(0, tz=timezone.utc), None, "succeeded")
    lines = read_lines(logs, NAMES["log_group"], run)
    assert [line.message for line in lines] == ["starting", "pdt: exit 0"]
