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

from pdt.deploy_aws_fargate import list_runs, read_lines, resource_names
from pdt.runs_cli import Run

NAMES = resource_names("my-app")
STREAM = f"ecs/{NAMES['family']}/task-1"


class FakeLogs:
    def __init__(self, streams=(), tail=(), pages=None):
        self.streams = list(streams)
        self.tail = list(tail)
        self.pages = pages or {}

    def describe_log_streams(self, **kwargs):
        return {"logStreams": self.streams}

    def get_log_events(self, logGroupName, logStreamName, startFromHead, nextToken=None,
                       limit=None):
        if not startFromHead:
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


def stream(started_ms: int, ended_ms: int) -> dict:
    return {"logStreamName": STREAM, "firstEventTimestamp": started_ms,
            "lastEventTimestamp": ended_ms}


def test_a_missing_log_group_is_no_runs():
    assert list_runs(MissingLogs(), FakeEcs(), NAMES) == []


def test_started_and_ended_come_from_the_stream_timestamps():
    logs = FakeLogs(streams=[stream(1_700_000_000_000, 1_700_000_010_000)],
                    tail=[{"message": "pdt: exit 0"}])
    [run] = list_runs(logs, FakeEcs(), NAMES)
    assert run.started == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert run.ended == datetime.fromtimestamp(1_700_000_010, tz=timezone.utc)


def test_a_zero_exit_marker_succeeds():
    logs = FakeLogs(streams=[stream(0, 1)], tail=[{"message": "pdt: exit 0"}])
    [run] = list_runs(logs, FakeEcs(), NAMES)
    assert run.status == "succeeded"


def test_a_nonzero_exit_marker_fails():
    logs = FakeLogs(streams=[stream(0, 1)], tail=[{"message": "pdt: exit 1"}])
    [run] = list_runs(logs, FakeEcs(), NAMES)
    assert run.status == "failed"


def test_no_marker_and_a_running_task_is_running():
    logs = FakeLogs(streams=[stream(0, 1)], tail=[{"message": "still going"}])
    [run] = list_runs(logs, FakeEcs(running_task_ids=["task-1"]), NAMES)
    assert run.status == "running"
    assert run.ended is None


def test_no_marker_and_no_running_task_fails():
    logs = FakeLogs(streams=[stream(0, 1)], tail=[{"message": "still going"}])
    [run] = list_runs(logs, FakeEcs(), NAMES)
    assert run.status == "failed"


def test_read_lines_pages_through_get_log_events_until_the_token_repeats():
    logs = FakeLogs(pages={
        0: ([{"message": "10:00:00 INFO   starting", "timestamp": 1_700_000_000_000}], 1),
        1: ([{"message": "pdt: exit 0", "timestamp": 1_700_000_001_000}], 1),
    })
    run = Run(STREAM, datetime.fromtimestamp(0, tz=timezone.utc), None, "succeeded")
    lines = read_lines(logs, NAMES["log_group"], run)
    assert [line.message for line in lines] == ["starting", "pdt: exit 0"]
