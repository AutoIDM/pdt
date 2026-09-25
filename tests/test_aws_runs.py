from datetime import datetime, timezone

from pdt.deploy_aws_batch import job_run, list_runs, read_lines, resource_names
from pdt.runs_cli import Run

NAMES = resource_names("my-app")
STREAM = "pdt-my-app/default/task-1"


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
    def __init__(self, pages):
        self.pages = pages
        self.streams_read = []

    def get_log_events(self, logGroupName, logStreamName, startFromHead, nextToken=None):
        self.streams_read.append(logStreamName)
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


def test_a_job_without_a_log_stream_has_no_lines():
    run = Run("job-1", datetime.fromtimestamp(0, tz=timezone.utc), None, "failed")
    assert read_lines(FakeBatch(stream=None), FakeLogs({}), NAMES["log_group"], run) == []
