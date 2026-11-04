import io
import json
import queue
import subprocess
import sys
import threading
import time
import zipfile

import pytest
from conftest import add_app
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_autoidm import backend, folder, pem, registered  # noqa: F401

from pdt import cli, config, console, deploy, lifecycle, managed_services
from pdt.utils import autoidm, storage

CUSTOMER = "cus_7a1c09e4b2"


def project_with(project, text):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n" + text)
    add_app(project, "report")
    add_app(project, "sync")
    return project


def test_the_defaults_send_run_history_only(project):
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    assert managed_services.settings() == {"customer": CUSTOMER, "send_run_history": True,
                                           "send_logs": False, "send_artifacts": False}


def test_no_managed_services_key_means_no_settings(project):
    project_with(project, "")
    assert managed_services.settings() is None


def test_a_send_key_takes_true_false_or_a_list_of_app_names(project):
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n"
                          "  send_run_history: false\n  send_logs: [report]\n"
                          "  send_artifacts: true\n")
    found = managed_services.settings()
    assert [managed_services.sends(found, key, "report") for key in managed_services.SEND_DEFAULTS
            ] == [False, True, True]
    assert [managed_services.sends(found, key, "sync") for key in managed_services.SEND_DEFAULTS
            ] == [False, False, True]


@pytest.mark.parametrize(("text", "said"), [
    ("managed_services: cus_7a1c09e4b2\n",
     "pdt.yml: managed_services must hold customer, for example customer: cus_7a1c09e4b2"),
    ("managed_services:\n  send_logs: true\n",
     "pdt.yml: managed_services: customer must be the customer ID that AutoIDM gave you"),
    ("managed_services:\n  customer: example\n",
     "pdt.yml: managed_services: customer must be the customer ID that AutoIDM gave you"),
    (f"managed_services:\n  customer: {CUSTOMER}\n  send_logs: yes please\n",
     "pdt.yml: managed_services: send_logs must be true, false, or a list of app names"),
    (f"managed_services:\n  customer: {CUSTOMER}\n  send_log: true\n",
     "pdt.yml: managed_services: unknown key 'send_log'; it takes: customer, send_run_history, "
     "send_logs, send_artifacts"),
    (f"managed_services:\n  customer: {CUSTOMER}\n  send_artifacts: [report, missing]\n",
     "pdt.yml: managed_services: send_artifacts names 'missing', which is not an app in this "
     "project"),
])
def test_validate_names_each_problem_with_managed_services(project, text, said):
    project_with(project, text)
    assert said in " ".join(config.validate())


def test_validate_accepts_a_valid_managed_services_block(project, capsys):
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n  send_logs: [sync]\n")
    assert config.validate() == []
    args = cli.build_parser().parse_args(["validate"])
    assert cli.cmd_validate(args) == 0
    assert "Configuration is valid." in capsys.readouterr().out


def test_managed_services_belongs_in_pdt_yml(project):
    project_with(project, "")
    (project / "report" / "config.yml").write_text(f"managed_services:\n  customer: {CUSTOMER}\n")
    assert ("report/config.yml: 'managed_services' belongs in the top level of pdt.yml, not here"
            in config.validate())


ALL_ON = (f"managed_services:\n  customer: {CUSTOMER}\n  send_logs: true\n"
          "  send_artifacts: true\n")
# Writes a file in the run's storage output folder, prints a line, and exits with argv[1].
WORK = ("import os, pathlib, sys\n"
        "out = pathlib.Path('.pdt/runs', os.environ['PDT_RUN_ID'], 'output')\n"
        "out.mkdir(parents=True)\n"
        "(out / 'report.csv').write_text('a,b\\n')\n"
        "print('hello from the app')\n"
        "sys.exit(int(sys.argv[1]))\n")


@pytest.fixture
def warned(monkeypatch):
    monkeypatch.setenv(managed_services.WARNED_ENV, "")


def run(project, code=0):
    return lifecycle.run_job("report", "windows", [sys.executable, "-c", WORK, str(code)],
                             project / "report")


def paths(seen):
    return [request["path"] for request in seen]


def test_a_manual_run_sends_its_events_log_and_artifacts(project, backend, warned, capfd):
    url, seen, _answers = backend
    project_with(project, ALL_ON)
    registered(url)
    assert run(project) == 0
    assert "hello from the app" in capfd.readouterr().out
    assert paths(seen) == ["/v1/events", "/v1/events", "/v1/uploads", "/bucket", "/v1/uploads",
                           "/bucket"]
    start, end = seen[0]["body"]["event"], seen[1]["body"]["event"]
    assert start["name"] == "job:run-started"
    assert start["params"] == {"project": project.name, "app": "report", "provider": "windows",
                               "run_id": storage.RUN_ID, "deployed": False, "trigger": "manual"}
    assert end["name"] == "job:run-completed"
    assert end["params"]["exit_code"] == 0
    assert isinstance(end["params"]["duration_seconds"], float)
    log, artifact = seen[2]["body"], seen[4]["body"]
    assert (log["kind"], log["filename"]) == ("log", f"{storage.RUN_ID}.log")
    assert b"hello from the app" in seen[3]["raw"]
    assert (artifact["kind"], artifact["filename"]) == ("artifact", f"{storage.RUN_ID}.zip")
    raw = seen[5]["raw"]
    data = raw[raw.index(b"PK\x03\x04"):raw.rindex(b"\r\n--")]
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["report.csv"]


def test_only_the_apps_a_list_names_send(project, backend, warned):
    url, seen, _answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n"
                          "  send_run_history: [sync]\n  send_logs: [report]\n")
    registered(url)
    assert run(project) == 0
    assert paths(seen) == ["/v1/uploads", "/bucket"]


def test_with_ci_set_a_run_sends_nothing_and_says_nothing(project, backend, warned, monkeypatch,
                                                          capsys):
    url, seen, _answers = backend
    project_with(project, ALL_ON)
    registered(url)
    monkeypatch.setenv("CI", "true")
    assert run(project) == 0
    assert seen == []
    assert "AutoIDM" not in capsys.readouterr().out


def test_a_project_with_no_managed_services_sends_nothing(project, backend, warned, capsys):
    url, seen, _answers = backend
    project_with(project, "")
    registered(url)
    assert run(project) == 0
    assert seen == []
    assert "AutoIDM" not in capsys.readouterr().out


def test_an_unregistered_computer_warns_once_per_command(project, folder, warned, capsys):
    project_with(project, ALL_ON)
    assert managed_services.sender("report") is None
    assert managed_services.sender("sync") is None
    out = " ".join(capsys.readouterr().out.split())
    assert out.count("not registered to AutoIDM") == 1
    assert f"This computer is not registered to AutoIDM for {CUSTOMER}" in out
    assert "pdt register --code CODE" in out


@pytest.mark.parametrize("saved", ["[]", '{"installation_id": 1, "api_url": "x"}',
                                   '{"installation_id": "ins_4f8a1c2e9b", "api_url": 5}',
                                   '{"installation_id": "ins_4f8a1c2e9b"}', "[" * 100000],
                         ids=["list", "number id", "number url", "no url", "nested too deep"])
def test_registration_metadata_of_the_wrong_shape_is_a_warning(project, folder, warned, capsys,
                                                               saved):
    project_with(project, ALL_ON)
    registered("https://api.example")
    autoidm.files(CUSTOMER)[0].write_text(saved)
    assert managed_services.sender("report") is None
    out = " ".join(capsys.readouterr().out.split())
    assert f"pdt cannot use the AutoIDM registration for {CUSTOMER}" in out


@pytest.mark.parametrize(("target", "warnings"), [
    ("find_sender", 1), ("start_run", 1), ("accept", 2), ("output_folder", 1), ("end_run", 1)])
def test_an_unexpected_error_in_monitoring_is_a_warning_and_keeps_the_exit_code(
        project, backend, warned, monkeypatch, capfd, target, warnings):
    url, _seen, _answers = backend
    project_with(project, ALL_ON)
    registered(url)

    def broken(*args, **kwargs):
        raise TypeError("an error no handler expects")

    monkeypatch.setattr(managed_services, target, broken)
    assert run(project, 3) == 3
    out, err = capfd.readouterr()
    assert "hello from the app" in out
    said = " ".join((out + err).split())
    assert said.count("TypeError: an error no handler expects") == warnings
    assert "Run of report failed" in out


def test_a_folder_pdt_cannot_name_costs_only_the_artifacts(project, backend, warned, monkeypatch):
    url, seen, _answers = backend
    project_with(project, ALL_ON)
    registered(url)

    def broken(*args, **kwargs):
        raise TypeError("an error no handler expects")

    monkeypatch.setattr(managed_services, "output_folder", broken)
    assert run(project) == 0
    assert [request["body"]["filename"] for request in seen
            if request["path"] == "/v1/uploads"] == [f"{storage.RUN_ID}.log"]


def test_an_unexpected_error_on_the_send_thread_is_a_warning_and_keeps_the_exit_code(
        project, backend, warned, monkeypatch, capfd):
    url, _seen, _answers = backend
    project_with(project, ALL_ON)
    registered(url)

    def broken(*args, **kwargs):
        raise TypeError("an error no handler expects")

    monkeypatch.setattr(managed_services.Sender, "event", broken)
    assert run(project, 3) == 3
    out, err = capfd.readouterr()
    said = " ".join((out + err).split())
    assert "AutoIDM did not get the job:run-started event: an error no handler expects" in said
    assert "AutoIDM did not get the job:run-failed event: an error no handler expects" in said


def test_an_event_with_no_app_is_a_warning_and_emit_returns(project, warned, capsys):
    project_with(project, ALL_ON)
    event = lifecycle.emit("job:run-started")
    assert event.name == "job:run-started"
    assert "AutoIDM did not get the job:run-started event: KeyError" in " ".join(
        capsys.readouterr().out.split())


def test_a_refused_registration_completes_the_work_and_keeps_its_exit_code(
        project, backend, warned, capsys):
    url, seen, answers = backend
    project_with(project, ALL_ON)
    registered(url)
    answers.append((401, {"error": "unauthorized"}))
    assert run(project) == 0
    assert list((project / "report" / ".pdt" / "runs" / storage.RUN_ID / "output").iterdir())
    assert paths(seen) == ["/v1/events"]
    out = " ".join(capsys.readouterr().out.split())
    info = autoidm.files(CUSTOMER)[0]
    assert f"AutoIDM refused the registration ins_4f8a1c2e9b in {info}" in out
    assert "failed" not in out


def test_a_refusal_at_the_first_upload_does_not_fail_the_run(project, backend, warned,
                                                                  monkeypatch, capsys):
    url, seen, answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n"
                          "  send_run_history: false\n  send_logs: true\n")
    registered(url)
    answers.append((401, {"error": "unauthorized"}))
    events = []
    emit = lifecycle.emit
    monkeypatch.setattr(lifecycle, "emit", lambda name, **params: events.append(
        (name, emit(name, **params).params)))
    assert run(project) == 0
    assert paths(seen) == ["/v1/uploads"]
    name, params = events[-1]
    assert (name, params["exit_code"]) == ("job:run-completed", 0)
    assert "AutoIDM refused the registration" in capsys.readouterr().out


def test_starting_a_run_does_not_wait_for_the_send_and_the_end_keeps_the_order(
        project, backend, warned, monkeypatch):
    url, seen, _answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    registered(url)
    release = threading.Event()
    post = autoidm.post

    def held(found, where, body):
        release.wait(5)
        return post(found, where, body)

    monkeypatch.setattr(autoidm, "post", held)
    began = time.monotonic()
    lifecycle.emit("job:run-started", project="example", app="report", run_id="r1")
    assert time.monotonic() - began < 1
    assert seen == []
    threading.Timer(0.1, release.set).start()
    found = managed_services.sender("report")
    assert not managed_services.end_run(
        managed_services.Run(found), "job:run-completed",
        {"project": "example", "app": "report", "run_id": "r1"}, 1.0)
    assert [request["body"]["event"]["name"] for request in seen] == [
        "job:run-started", "job:run-completed"]


def test_a_send_that_fails_after_the_limit_does_not_warn_a_second_time(project, backend, warned,
                                                                       monkeypatch, capsys):
    url, _seen, _answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    registered(url)
    release, finished = threading.Event(), threading.Event()

    def fails_late(found, where, body):
        release.wait(5)
        raise autoidm.AutoidmError("AutoIDM answered 500")

    monkeypatch.setattr(autoidm, "post", fails_late)
    found = managed_services.sender("report")
    managed_services.queue_send(found, "the slow event", lambda: found.event(
        "job:run-started", "2026-11-02T00:00:00Z", {"project": "example", "app": "report"}))
    managed_services.wait(0.1)
    managed_services.queue_send(found, "the next item", finished.set)
    release.set()
    assert finished.wait(5)
    managed_services.wait()
    out = " ".join(capsys.readouterr().out.split())
    assert out.count("AutoIDM did not get the slow event") == 1
    assert "pdt stopped waiting" in out


def test_a_late_log_and_artifact_are_warnings_inside_the_limit_and_keep_the_exit_code(
        project, backend, warned, monkeypatch, capsys):
    url, _seen, _answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n"
                          "  send_run_history: false\n  send_logs: true\n"
                          "  send_artifacts: true\n")
    registered(url)
    release = threading.Event()
    monkeypatch.setattr(autoidm, "upload", lambda found, body, data: release.wait(5))
    monkeypatch.setattr(managed_services, "EXIT_WAIT_SECONDS", 0.3)
    began = time.monotonic()
    assert run(project) == 0
    waited = time.monotonic() - began
    release.set()
    assert waited < 3
    out = " ".join(capsys.readouterr().out.split())
    assert f"AutoIDM did not get the log {storage.RUN_ID}.log: pdt stopped waiting" in out
    assert f"AutoIDM did not get the artifacts {storage.RUN_ID}.zip: pdt stopped waiting" in out
    assert "Run of report completed" in out


def test_a_refused_registration_keeps_the_exit_code_of_a_failed_run(project, backend, warned,
                                                                     capsys):
    url, _seen, answers = backend
    project_with(project, ALL_ON)
    registered(url)
    answers.append((401, {"error": "unauthorized"}))
    assert run(project, 3) == 3
    assert "AutoIDM refused the registration" in capsys.readouterr().out


def test_a_server_error_is_a_warning_and_the_run_goes_on(project, backend, warned, capsys):
    url, seen, answers = backend
    project_with(project, ALL_ON)
    registered(url)
    answers.append((503, {"error": "unavailable", "message": "try later"}))
    assert run(project) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert ("AutoIDM did not get the job:run-started event: AutoIDM answered HTTP 503: try later"
            in out)
    assert paths(seen)[1:] == ["/v1/events", "/v1/uploads", "/bucket", "/v1/uploads", "/bucket"]


def test_an_unreachable_backend_is_a_warning(project, folder, warned, capsys):
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    registered("http://127.0.0.1:9")
    assert run(project) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "AutoIDM did not get the job:run-started event: AutoIDM could not be reached" in out


def test_artifacts_over_the_limit_are_skipped_with_a_warning(project, backend, warned,
                                                             monkeypatch, capsys):
    url, seen, _answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n"
                          "  send_run_history: false\n  send_artifacts: true\n")
    registered(url)
    monkeypatch.setattr(managed_services, "MAX_UPLOAD_BYTES", 10)
    assert run(project) == 0
    assert seen == []
    out = " ".join(capsys.readouterr().out.split())
    assert f"AutoIDM did not get the artifacts {storage.RUN_ID}.zip" in out


@pytest.mark.parametrize("fields", [{"policy": "\ud800"}, {"policy": 1}, ["policy"]])
def test_upload_fields_pdt_cannot_send_are_a_warning_and_keep_the_exit_code(
        project, backend, warned, capsys, fields):
    url, seen, answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n"
                          "  send_run_history: false\n  send_logs: true\n")
    registered(url)
    answers.append((200, {"url": url + "/bucket", "fields": fields, "key": "uploads/x"}))
    assert run(project, 3) == 3
    assert paths(seen) == ["/v1/uploads"]
    out = " ".join(capsys.readouterr().out.split())
    assert f"AutoIDM did not get the log {storage.RUN_ID}.log: AutoIDM answered /v1/uploads" in out


def test_a_cloud_job_sends_with_the_registration_in_its_secret(project, backend, warned,
                                                               monkeypatch, capsys):
    url, seen, answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({
        autoidm.ID_ENV: "ins_0000000007", autoidm.URL_ENV: url, autoidm.CUSTOMER_ENV: CUSTOMER,
        autoidm.KEY_ENV: pem(Ed25519PrivateKey.generate()).decode()}))
    answers.append((401, {"error": "unauthorized"}))
    assert lifecycle.run_job("report", "aws", [sys.executable, "-c", WORK, "0"],
                             project / "report", deployed=True) == 0
    assert seen[0]["body"]["event"]["params"]["deployed"] is True
    out = " ".join(capsys.readouterr().out.split())
    assert ("AutoIDM refused the cloud registration ins_0000000007 of report "
            "(PDT_INSTALLATION_ID in the app's secret)") in out


def test_a_deploy_sends_app_deployed_with_the_schedule_and_the_place(project, backend, warned,
                                                                     monkeypatch, capsys):
    url, seen, _answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    (project / "report" / "config.yml").write_text(
        "schedule: daily\ntimezone: America/New_York\nplatform:\n  provider: aws\n"
        "  region: us-east-2\n")
    registered(url)
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: 0)
    monkeypatch.setattr(config, "deployed_platform", lambda name: {
        "provider": "aws", "region": "us-east-2", "account": "123456789012"})
    assert deploy.provider_step("deploy", "aws", "report", True) == 0
    assert len(seen) == 1
    event = seen[0]["body"]["event"]
    assert event["name"] == "app:deployed"
    assert capsys.readouterr().out.splitlines() == ["[alert] Deploying report to AWS",
                                                    "[alert] Deployed report to AWS"]
    assert event["params"] == {"project": project.name, "app": "report", "provider": "aws",
                               "schedule": "0 0 * * *", "timezone": "America/New_York",
                               "place": "aws:123456789012:us-east-2"}


def test_a_windows_app_has_no_place_and_its_time_zone_is_the_computers(project, monkeypatch):
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    monkeypatch.setattr("pdt.regions.local_timezone", lambda: "Europe/Berlin")
    monkeypatch.setattr(config, "deployed_platform", lambda name: {"provider": "windows"})
    assert managed_services.deployed_params("report") == {
        "schedule": None, "timezone": "Europe/Berlin", "place": None}


def test_a_deploy_without_managed_services_reads_no_deploy_fields(project, monkeypatch):
    project_with(project, "")
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: 0)
    monkeypatch.setattr(managed_services, "deployed_params",
                        lambda name: pytest.fail("read the deploy fields"))
    monkeypatch.setattr("pdt.regions.local_timezone", lambda: pytest.fail("read the time zone"))
    assert deploy.provider_step("deploy", "aws", "report", True) == 0


def test_a_cloud_job_in_workspace_reports_the_name_of_the_deploying_project(
        tmp_path, backend, warned, monkeypatch):
    import shutil
    import tempfile

    from pdt import deploy_common
    url, seen, _answers = backend
    deploying = tmp_path / "example"
    deploying.mkdir()
    monkeypatch.chdir(deploying)
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    project_with(deploying, ALL_ON)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    stage = deploy_common.stage_build_context(config.merged_app("report"))
    assert (stage / config.PROJECT_NAME_FILE).read_text() == "example\n"
    # The image copies the staged context to /workspace and sets PDT_PROJECT to it.
    workspace = tmp_path / "workspace"
    shutil.copytree(stage, workspace)
    monkeypatch.setenv("PDT_PROJECT", str(workspace))
    registered(url)
    assert lifecycle.run_job("report", "aws", [sys.executable, "-c", WORK, "0"],
                             workspace / "report", deployed=True) == 0
    events = [request["body"]["event"] for request in seen if request["path"] == "/v1/events"]
    uploads = [request["body"] for request in seen if request["path"] == "/v1/uploads"]
    assert {event["params"]["project"] for event in events} == {"example"}
    assert {upload["project"] for upload in uploads} == {"example"}
    monkeypatch.setenv("PDT_PROJECT", str(deploying))
    assert lifecycle.app_params("report", "aws")["project"] == "example"


class Started:
    """A subprocess.Popen for run_job that records how it started the command."""

    calls: list[dict] = []
    stdout = io.BytesIO()

    def __init__(self, command, **kwargs):
        Started.calls.append(kwargs)

    def wait(self):
        return 0


@pytest.mark.parametrize("case", ["unregistered", "ci", "not covered", "registered"])
def test_the_output_goes_through_a_pipe_only_when_a_sender_sends_logs(
        project, backend, warned, monkeypatch, case):
    url, _seen, _answers = backend
    covered = "[sync]" if case == "not covered" else "true"
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n"
                          f"  send_run_history: false\n  send_logs: {covered}\n")
    if case != "unregistered":
        registered(url)
    if case == "ci":
        monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(lifecycle.subprocess, "Popen", Started)
    Started.calls = []
    assert lifecycle.run_job("report", "windows", ["app"], project / "report") == 0
    piped = "stdout" in Started.calls[0]
    assert piped == (case == "registered")


def test_autoidm_is_not_an_alerts_type():
    assert "autoidm" not in lifecycle.TRANSPORTS
    with pytest.raises(config.ConfigError, match="alerts type 'autoidm' is not known"):
        lifecycle.parse_entry({"type": "autoidm", "events": ["all"]}, "pdt.yml")


@pytest.mark.parametrize(("name", "sent"), [
    ("jobs", "jobs"),
    ("My Jobs", "My-Jobs-"),
    ("Lønn", "L-nn-"),
    ("..", "..-"),
    ("a" * 129, "a" * 119 + "-"),
])
def test_a_name_autoidm_does_not_take_becomes_one_it_does(name, sent):
    encoded = managed_services.backend_name(name)
    assert encoded.startswith(sent)
    assert managed_services.BACKEND_NAME.fullmatch(encoded)
    assert encoded == managed_services.backend_name(name)


def test_two_names_that_differ_only_in_the_replaced_characters_stay_apart():
    assert managed_services.backend_name("My Jobs") != managed_services.backend_name("My-Jobs")


def test_events_and_uploads_name_a_project_with_spaces_the_same_way(tmp_path, backend, warned,
                                                                     monkeypatch):
    url, seen, _answers = backend
    spaced = tmp_path / "My Jobs"
    spaced.mkdir()
    monkeypatch.setenv("PDT_PROJECT", str(spaced))
    project_with(spaced, ALL_ON)
    registered(url)
    assert run(spaced) == 0
    names = {request["body"]["event"]["params"]["project"] for request in seen
             if request["path"] == "/v1/events"}
    names |= {request["body"]["project"] for request in seen if request["path"] == "/v1/uploads"}
    assert names == {managed_services.backend_name("My Jobs")}


def sender_for(project, url, text=ALL_ON):
    project_with(project, text)
    registered(url)
    return managed_services.sender("report")


PARAMS = {"project": "example", "app": "report", "run_id": "r1"}


def test_a_log_over_the_limit_is_skipped_without_reading_it(project, backend, warned,
                                                            monkeypatch, tmp_path, capsys):
    url, seen, _answers = backend
    sender = sender_for(project, url)
    log = tmp_path / "r1.log"
    log.write_bytes(b"x" * 11)
    monkeypatch.setattr(managed_services, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(type(log), "read_bytes", lambda self: pytest.fail("read the whole log"))
    sender.upload_log(log, "r1.log", PARAMS)
    assert seen == []
    assert "AutoIDM did not get the log r1.log" in " ".join(capsys.readouterr().out.split())


def test_a_link_out_of_the_output_folder_is_not_uploaded(project, backend, warned, tmp_path,
                                                         capsys):
    url, seen, _answers = backend
    sender = sender_for(project, url)
    secret = tmp_path / "credentials"
    secret.write_text("key")
    output = tmp_path / "output"
    output.mkdir()
    (output / "report.csv").write_text("a,b\n")
    (output / "creds").symlink_to(secret)
    sender.upload_artifacts(output, "r1.zip", PARAMS)
    raw = seen[1]["raw"]
    data = raw[raw.index(b"PK\x03\x04"):raw.rindex(b"\r\n--")]
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["report.csv"]
    out = " ".join(capsys.readouterr().out.split())
    assert "creds: it links to a place outside" in out.replace(str(output) + "/", "")


@pytest.mark.parametrize(("method", "error", "said"), [
    ("write", PermissionError(13, "Permission denied", "report.csv"), "[Errno 13]"),
    ("write", UnicodeEncodeError("utf-8", "\udc80", 0, 1, "surrogates not allowed"),
     "surrogates not allowed"),
    ("write", zipfile.LargeZipFile("Filesize would require ZIP64 extensions"), "ZIP64"),
    ("is_dir", PermissionError(13, "Permission denied", "output"), "[Errno 13]"),
])
def test_artifacts_that_cannot_be_zipped_are_a_warning_and_keep_the_exit_code(
        project, backend, warned, monkeypatch, capsys, method, error, said):
    url, _seen, _answers = backend
    project_with(project, ALL_ON)
    registered(url)
    owner = zipfile.ZipFile if method == "write" else type(project)
    real = getattr(owner, method)

    def refused(self, *args, **kwargs):
        if method == "write" or self.name == "output":
            raise error
        return real(self, *args, **kwargs)

    monkeypatch.setattr(owner, method, refused)
    assert run(project, 3) == 3
    out = " ".join(capsys.readouterr().out.split())
    assert f"AutoIDM did not get the artifacts {storage.RUN_ID}.zip: " in out
    assert said in out


def test_a_full_disk_stops_only_the_log_and_the_command_keeps_printing(tmp_path, monkeypatch,
                                                                       capfd):
    script = "import sys\nfor i in range(2000):\n    print('line', i)\nsys.exit(0)\n"
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    log = tmp_path / "run.log"
    real_open = type(log).open

    class Full(io.RawIOBase):
        def writable(self):
            return True

        def write(self, chunk):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(type(log), "open", lambda self, *args, **kwargs: (
        Full() if self == log else real_open(self, *args, **kwargs)))
    children = queue.Queue(1)
    children.put(child)
    copier = lifecycle.copy_output(children, log)
    assert child.wait(timeout=30) == 0
    copier.join(10)
    assert not copier.is_alive()
    console.drain()
    out, err = capfd.readouterr()
    assert "line 1999" in out
    assert (out + err).count("pdt stopped keeping the output of this run for AutoIDM") == 1


def test_a_broken_stdout_stops_only_the_printing_and_the_log_keeps_everything(
        tmp_path, monkeypatch, capfd):
    script = "import sys\nfor i in range(20000):\n    print('line', i)\nsys.exit(0)\n"
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    log = tmp_path / "run.log"

    class Broken(io.RawIOBase):
        def writable(self):
            return True

        def write(self, chunk):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(Broken()))
    children = queue.Queue(1)
    children.put(child)
    copier = lifecycle.copy_output(children, log)
    try:
        assert child.wait(timeout=20) == 0
    finally:
        child.kill()
    copier.join(10)
    console.drain()
    monkeypatch.undo()
    _out, err = capfd.readouterr()
    assert b"line 19999" in log.read_bytes()
    assert " ".join(err.split()).count("pdt stopped printing the output of this run") == 1


def test_a_closed_stdout_stops_only_the_printing_and_the_command_never_blocks(
        tmp_path, monkeypatch, capfd):
    script = "import sys\nfor i in range(20000):\n    print('line', i)\nsys.exit(0)\n"
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    log = tmp_path / "run.log"
    closed = io.TextIOWrapper(io.BytesIO())
    closed.close()
    monkeypatch.setattr(sys, "stdout", closed)
    try:
        children = queue.Queue(1)
        children.put(child)
        copier = lifecycle.copy_output(children, log)
        assert child.wait(timeout=20) == 0
    finally:
        child.kill()
    copier.join(10)
    console.drain()
    monkeypatch.undo()
    _out, err = capfd.readouterr()
    assert b"line 19999" in log.read_bytes()
    assert "pdt stopped printing the output of this run" in " ".join(err.split())


def test_the_log_stops_one_byte_over_the_upload_limit_and_the_output_goes_on(
        tmp_path, monkeypatch, capfd):
    script = "import sys\nfor i in range(20000):\n    print('line', i)\nsys.exit(0)\n"
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    log = tmp_path / "run.log"
    monkeypatch.setattr(managed_services, "MAX_UPLOAD_BYTES", 100)
    children = queue.Queue(1)
    children.put(child)
    copier = lifecycle.copy_output(children, log)
    try:
        assert child.wait(timeout=20) == 0
    finally:
        child.kill()
    copier.join(10)
    assert not copier.is_alive()
    out, _err = capfd.readouterr()
    assert "line 19999" in out
    assert log.stat().st_size == 101


def test_an_output_folder_that_is_a_link_sends_nothing(project, backend, warned, tmp_path,
                                                       capsys):
    url, seen, _answers = backend
    sender = sender_for(project, url)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "credentials").write_text("key")
    output = tmp_path / "output"
    output.symlink_to(elsewhere)
    sender.upload_artifacts(output, "r1.zip", PARAMS)
    assert seen == []
    assert "links to another place" in " ".join(capsys.readouterr().out.split())


def test_an_output_folder_that_is_a_junction_sends_nothing(project, backend, warned, tmp_path,
                                                           monkeypatch, capsys):
    url, seen, _answers = backend
    sender = sender_for(project, url)
    output = tmp_path / "output"
    output.mkdir()
    (output / "credentials").write_text("key")
    monkeypatch.setattr(type(output), "is_junction", lambda self: self == output)
    sender.upload_artifacts(output, "r1.zip", PARAMS)
    assert seen == []
    assert "links to another place" in " ".join(capsys.readouterr().out.split())


def test_an_entry_pdt_cannot_resolve_is_skipped_and_the_rest_is_sent(project, backend, warned,
                                                                      tmp_path, monkeypatch,
                                                                      capsys):
    url, seen, _answers = backend
    sender = sender_for(project, url)
    output = tmp_path / "output"
    output.mkdir()
    (output / "report.csv").write_text("a,b\n")
    (output / "loop").symlink_to(output / "loop")
    real_resolve = type(output).resolve

    def resolve(self, *args, **kwargs):
        if self.name == "loop":
            raise RuntimeError(f"Symlink loop from {self!r}")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(type(output), "resolve", resolve)
    sender.upload_artifacts(output, "r1.zip", PARAMS)
    raw = seen[1]["raw"]
    data = raw[raw.index(b"PK\x03\x04"):raw.rindex(b"\r\n--")]
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["report.csv"]
    assert "or into a loop" in " ".join(capsys.readouterr().out.split())


def test_a_log_pdt_cannot_make_leaves_the_output_unpiped_and_the_run_goes_on(
        project, backend, warned, monkeypatch, capfd):
    url, seen, _answers = backend
    project_with(project, ALL_ON)
    registered(url)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(managed_services.tempfile, "mkstemp", full)
    monkeypatch.setattr(managed_services.tempfile, "mkdtemp", full)
    assert run(project) == 0
    out, err = capfd.readouterr()
    assert "hello from the app" in out
    assert "pdt cannot keep the output" in " ".join((out + err).split())
    assert "/bucket" not in paths(seen)


def test_a_log_pdt_cannot_open_stops_only_the_log_and_the_command_never_blocks(
        tmp_path, monkeypatch, capfd):
    script = "import sys\nfor i in range(20000):\n    print('line', i)\nsys.exit(0)\n"
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    log = tmp_path / "run.log"
    real_open = type(log).open

    def refused(self, *args, **kwargs):
        if self == log:
            raise PermissionError(13, "Permission denied", str(log))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(log), "open", refused)
    children = queue.Queue(1)
    children.put(child)
    copier = lifecycle.copy_output(children, log)
    try:
        assert child.wait(timeout=20) == 0
    finally:
        child.kill()
    copier.join(10)
    console.drain()
    out, err = capfd.readouterr()
    assert "line 19999" in out
    assert "pdt cannot keep the output of this run for AutoIDM" in " ".join((out + err).split())


def test_a_reader_thread_pdt_cannot_start_leaves_the_output_unpiped_and_keeps_the_exit_code(
        project, backend, warned, monkeypatch, capfd):
    url, seen, _answers = backend
    project_with(project, ALL_ON)
    registered(url)
    real_start = threading.Thread.start

    def at_the_limit(self):
        if self._target.__qualname__ == "copy_output.<locals>.copy":
            raise RuntimeError("can't start new thread")
        real_start(self)

    real_popen = subprocess.Popen
    started = []

    def recorded(command, **kwargs):
        started.append(kwargs)
        return real_popen(command, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", at_the_limit)
    monkeypatch.setattr(lifecycle.subprocess, "Popen", recorded)
    script = "import sys\nfor i in range(20000):\n    print('line', i)\nsys.exit(3)\n"
    assert lifecycle.run_job("report", "windows", [sys.executable, "-c", script],
                             project / "report") == 3
    console.drain()
    out, err = capfd.readouterr()
    assert "line 19999" in out
    assert ("pdt cannot keep the output of this run for AutoIDM: can't start new thread"
            in " ".join((out + err).split()))
    assert [kwargs.get("stdout") for kwargs in started] == [None]
    assert "Run of report failed (exit code 3)" in out
    end = seen[1]["body"]["event"]
    assert (end["name"], end["params"]["exit_code"]) == ("job:run-failed", 3)


def failing_start(monkeypatch, *names, times=None):
    """Make Thread.start raise for a thread whose target has one of `names`, at most `times`
    times."""
    real_start = threading.Thread.start
    failed = []

    def at_the_limit(self):
        if (getattr(self._target, "__name__", "") in names
                and (times is None or len(failed) < times)):
            failed.append(self._target.__name__)
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", at_the_limit)
    return failed


def test_a_sender_thread_pdt_cannot_start_skips_that_send_and_starts_for_the_next(
        project, backend, warned, monkeypatch, capfd):
    url, seen, _answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    registered(url)
    monkeypatch.setattr(managed_services, "_thread", None)
    monkeypatch.setattr(managed_services, "_queue", queue.Queue())
    failed = failing_start(monkeypatch, "_send_each", times=1)
    began = time.monotonic()
    assert run(project, 3) == 3
    assert time.monotonic() - began < managed_services.EXIT_WAIT_SECONDS
    console.drain()
    out, err = capfd.readouterr()
    said = " ".join((out + err).split())
    assert failed == ["_send_each"]
    assert "AutoIDM did not get the job:run-started event: can't start new thread" in said
    assert "Run of report failed (exit code 3)" in out
    assert [(request["body"]["event"]["name"], request["body"]["event"]["params"]["exit_code"])
            for request in seen] == [("job:run-failed", 3)]


def test_a_wait_thread_pdt_cannot_start_waits_on_the_calling_thread_and_keeps_the_exit_code(
        project, backend, warned, monkeypatch, capfd):
    url, seen, _answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    registered(url)
    failed = failing_start(monkeypatch, "wait", "wait_alerts")
    assert run(project, 3) == 3
    console.drain()
    out, _err = capfd.readouterr()
    assert sorted(failed) == ["wait", "wait_alerts"]
    assert "Run of report failed (exit code 3)" in out
    end = seen[1]["body"]["event"]
    assert (end["name"], end["params"]["exit_code"]) == ("job:run-failed", 3)


@pytest.mark.skipif(sys.platform == "win32", reason="chmod does not refuse writes on Windows")
def test_an_app_folder_pdt_cannot_write_in_sends_the_output_folder_it_names(
        project, backend, warned, capfd):
    url, seen, _answers = backend
    project_with(project, ALL_ON)
    registered(url)
    work = ("import os, pathlib\n"
            "out = pathlib.Path(os.environ['PDT_OUTPUT_DIR'])\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            "(out / 'report.csv').write_text('a,b\\n')\n")
    app = project / "report"
    app.chmod(0o555)
    try:
        assert lifecycle.run_job("report", "windows", [sys.executable, "-c", work], app) == 0
    finally:
        app.chmod(0o755)
    raw = seen[-1]["raw"]
    data = raw[raw.index(b"PK\x03\x04"):raw.rindex(b"\r\n--")]
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["report.csv"]
