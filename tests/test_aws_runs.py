from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from pdt import deploy_aws_batch
from pdt.deploy_aws_batch import job_run, list_runs, read_lines, resource_names, stream_lines
from pdt.runs_cli import Run, StreamError

NAMES = resource_names("my-app")
STREAM = "pdt-my-app/default/task-1"
GROUP_ARN = "arn:aws:logs:us-east-1:123456789012:log-group:/pdt/my-app"


def job(job_id="job-1", status="SUCCEEDED", created=0, started=1_000, stopped=2_000,
        exit_code=0):
    summary = {"jobId": job_id, "jobName": "pdt-my-app", "status": status, "createdAt": created}
    if started is not None:
        summary["startedAt"] = started
    if stopped is not None:
        summary["stoppedAt"] = stopped
    if exit_code is not None:
        summary["container"] = {"exitCode": exit_code}
    return summary


class FakeBatch:
    def __init__(self, pages=None, stream=STREAM):
        self.pages = pages or {None: ([], None)}
        self.stream = stream
        self.list_calls = []

    def list_jobs(self, nextToken=None, **kwargs):
        self.list_calls.append(kwargs)
        jobs, token = self.pages[nextToken]
        return {"jobSummaryList": jobs, **({"nextToken": token} if token else {})}

    def describe_jobs(self, jobs):
        if self.stream is None:
            return {"jobs": [{"jobId": jobs[0], "container": {}}]}
        return {"jobs": [{"jobId": jobs[0], "container": {"logStreamName": self.stream}}]}


class MissingQueue:
    def list_jobs(self, **kwargs):
        error = type("ClientError", (Exception,), {})()
        error.response = {"Error": {"Code": "ClientException",
                                    "Message": "Job queue pdt does not exist"}}
        raise error


class FakeLogs:
    def __init__(self, pages, tail=None, calls=None):
        self.pages = pages
        self.tail = tail
        self.calls = [] if calls is None else calls
        self.streams_read = []
        self.start_times = []

    def describe_log_groups(self, logGroupNamePrefix):
        return {"logGroups": [{"logGroupName": NAMES["log_group"], "logGroupArn": GROUP_ARN}]}

    def start_live_tail(self, logGroupIdentifiers, logStreamNames=None,
                        logStreamNamePrefixes=None):
        if logStreamNames is not None:
            raise client_error("ResourceNotFoundException")
        self.calls.append(("start_live_tail", logGroupIdentifiers, logStreamNamePrefixes))
        if isinstance(self.tail, Exception):
            raise self.tail
        return {"responseStream": self.tail}

    def get_log_events(self, logGroupName, logStreamName, startFromHead, nextToken=None,
                       startTime=None):
        self.calls.append("get_log_events")
        self.streams_read.append(logStreamName)
        self.start_times.append(startTime)
        events, token = self.pages[0 if nextToken is None else nextToken]
        return {"events": events, "nextForwardToken": token}


def test_a_succeeded_job_maps_with_its_times_and_exit_code():
    run = job_run(job(started=1_700_000_000_000, stopped=1_700_000_010_000))
    assert run.id == "job-1"
    assert run.status == "succeeded"
    assert run.exit_code == 0
    assert run.started == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert run.ended == datetime.fromtimestamp(1_700_000_010, tz=timezone.utc)


def test_a_failed_job_keeps_its_nonzero_exit_code():
    run = job_run(job(status="FAILED", exit_code=1))
    assert run.status == "failed"
    assert run.exit_code == 1


def test_a_running_job_has_no_end_and_no_exit_code():
    run = job_run(job(status="RUNNING", stopped=None, exit_code=None))
    assert run.status == "running"
    assert run.ended is None
    assert run.exit_code is None


def test_a_queued_job_is_running_from_its_submission_time():
    run = job_run(job(status="RUNNABLE", created=5_000, started=None, stopped=None,
                      exit_code=None))
    assert run.status == "running"
    assert run.started == datetime.fromtimestamp(5, tz=timezone.utc)


def test_a_job_that_failed_before_starting_is_failed_from_its_submission_time():
    run = job_run(job(status="FAILED", created=5_000, started=None, stopped=6_000,
                      exit_code=None))
    assert run.status == "failed"
    assert run.started == datetime.fromtimestamp(5, tz=timezone.utc)
    assert run.ended == datetime.fromtimestamp(6, tz=timezone.utc)


def test_a_missing_queue_is_no_runs():
    assert list_runs(MissingQueue(), NAMES["job_definition"]) == []


def test_list_runs_filters_on_the_job_name_follows_every_page_and_is_newest_first():
    batch = FakeBatch(pages={None: ([job("job-1", started=1_000)], "page-2"),
                             "page-2": ([job("job-2", started=3_000)], None)})
    found = list_runs(batch, NAMES["job_definition"])
    assert [run.id for run in found] == ["job-2", "job-1"]
    assert batch.list_calls[0]["jobQueue"] == "pdt"
    assert batch.list_calls[0]["filters"] == [{"name": "JOB_NAME", "values": ["pdt-my-app"]}]


def test_read_lines_reads_the_job_log_stream_until_the_token_repeats():
    logs = FakeLogs(pages={
        0: ([{"message": "10:00:00 INFO   starting", "timestamp": 1_700_000_000_000}], 1),
        1: ([{"message": "pdt: exit 0", "timestamp": 1_700_000_001_000}], 1),
    })
    run = Run("job-1", datetime.fromtimestamp(0, tz=timezone.utc), None, "succeeded")
    lines = read_lines(FakeBatch(), logs, NAMES["log_group"], run)
    assert [line.message for line in lines] == ["starting", "pdt: exit 0"]
    assert logs.streams_read == [STREAM, STREAM]
    assert logs.start_times == [None, None]


def test_read_lines_from_a_time_asks_only_for_the_events_from_it():
    logs = FakeLogs(pages={0: ([], 0)})
    run = Run("job-1", datetime.fromtimestamp(0, tz=timezone.utc), None, "running")
    read_lines(FakeBatch(), logs, NAMES["log_group"], run,
               datetime.fromtimestamp(1_700_000_001.25, tz=timezone.utc))
    assert logs.start_times == [1_700_000_001_250]


def test_a_job_without_a_log_stream_has_no_lines():
    run = Run("job-1", datetime.fromtimestamp(0, tz=timezone.utc), None, "failed")
    assert read_lines(FakeBatch(stream=None), FakeLogs({}), NAMES["log_group"], run) == []


class FakeEventStream:
    """A StartLiveTail response stream; an exception in `events` raises when it is reached."""

    def __init__(self, events, calls):
        self.events = events
        self.calls = calls
        self.closed = False

    def __iter__(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            self.calls.append(next(iter(event)))
            yield event

    def close(self):
        self.closed = True


def update(*results):
    return {"sessionUpdate": {"sessionResults": [
        {"timestamp": stamp, "message": message} for stamp, message in results]}}


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": f"{code} message"}}, "StartLiveTail")


STARTED = (1_700_000_000_000, "10:00:00 INFO   starting")
RUNNING = Run("job-1", datetime.fromtimestamp(0, tz=timezone.utc), None, "running")


def live(events):
    """A FakeLogs whose store holds STARTED, and whose live tail sends `events`."""
    calls = []
    stream = FakeEventStream(events, calls)
    pages = {0: ([{"message": STARTED[1], "timestamp": STARTED[0]}], 1), 1: ([], 1)}
    return FakeLogs(pages, stream, calls), stream


def test_follow_tails_the_stream_before_it_exists_then_streams_each_new_line():
    logs, stream = live([{"sessionStart": {}}, update(),
                         update(STARTED, (1_700_000_001_000, "row")),
                         update((1_700_000_002_000, "pdt: exit 0")), update((0, "never"))])
    lines = list(stream_lines(FakeBatch(), logs, NAMES["log_group"], RUNNING, True))
    assert [line.message for line in lines] == ["starting", "row", "pdt: exit 0"]
    # The session has started before the store read, so no line falls between them.
    assert logs.calls[:3] == [("start_live_tail", [GROUP_ARN], [STREAM]), "sessionStart",
                              "get_log_events"]
    assert stream.closed


def test_the_session_time_limit_ends_the_tail_normally():
    logs, stream = live([{"sessionStart": {}}, update((1_700_000_001_000, "row")),
                         client_error("SessionTimeoutException")])
    lines = list(stream_lines(FakeBatch(), logs, NAMES["log_group"], RUNNING, True))
    assert [line.message for line in lines] == ["starting", "row"]
    assert stream.closed


def test_another_error_in_the_tail_is_a_stream_error():
    logs, stream = live([{"sessionStart": {}}, client_error("ServiceUnavailableException")])
    with pytest.raises(StreamError, match="ServiceUnavailableException"):
        list(stream_lines(FakeBatch(), logs, NAMES["log_group"], RUNNING, True))
    assert stream.closed


def test_a_login_that_may_not_tail_is_told_the_action_it_needs():
    logs = FakeLogs({}, client_error("AccessDeniedException"))
    with pytest.raises(StreamError, match="logs:StartLiveTail"):
        list(stream_lines(FakeBatch(), logs, NAMES["log_group"], RUNNING, True))


def test_a_quiet_tail_ends_after_the_quiet_limit(monkeypatch):
    clock = iter([0, 30, deploy_aws_batch.runs_cli.STREAM_QUIET])
    monkeypatch.setattr(deploy_aws_batch.time, "monotonic", lambda: next(clock))
    logs, stream = live([{"sessionStart": {}}, update(), update(), update((0, "never"))])
    lines = list(stream_lines(FakeBatch(), logs, NAMES["log_group"], RUNNING, True))
    assert [line.message for line in lines] == ["starting"]
    assert stream.closed


def test_a_job_with_no_log_stream_yet_has_no_live_lines():
    logs, stream = live([{"sessionStart": {}}])
    assert list(stream_lines(FakeBatch(stream=None), logs, NAMES["log_group"], RUNNING, True)) == []
    assert logs.calls == []


def test_without_follow_the_stream_reads_the_store_only():
    logs, stream = live([])
    lines = list(stream_lines(FakeBatch(), logs, NAMES["log_group"], RUNNING, False))
    assert [line.message for line in lines] == ["starting"]
    assert logs.calls == ["get_log_events", "get_log_events"]


def test_logs_list_names_the_log_group_once_and_each_stream_from_one_describe(
        capsys, monkeypatch, tmp_path, waits):
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))

    class Batch(FakeBatch):
        def describe_jobs(self, jobs):
            self.described = getattr(self, "described", []) + [jobs]
            self.waits = list(waits)
            return {"jobs": [{"jobId": job_id, "container": {"logStreamName": f"s/{job_id}"}}
                             for job_id in jobs]}

    batch = Batch({None: ([job("job-1", started=2_000), job("job-2", started=1_000)], None)})
    session = type("Session", (), {"region_name": "us-east-1", "profile_name": "default",
                                   "client": lambda self, name, config=None:
                                   batch if name == "batch" else None})()
    assert deploy_aws_batch.logs({"name": "report", "platform": {}}, session, ["--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"CloudWatch log group: {resource_names('report')['log_group']}"
    assert lines[2].split()[:2] == ["1", "s/job-1"]
    assert lines[3].split()[:2] == ["2", "s/job-2"]
    assert batch.described == [["job-1", "job-2"]]
    assert batch.waits == ["reading: run list"]
