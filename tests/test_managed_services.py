import io
import json
import subprocess
import sys
import zipfile

import pytest
from conftest import add_app
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_autoidm import backend, folder, pem, registered  # noqa: F401

from pdt import cli, config, deploy, lifecycle, managed_services
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


def test_a_refused_registration_completes_the_work_then_exits_with_its_own_code(
        project, backend, warned, capsys):
    url, seen, answers = backend
    project_with(project, ALL_ON)
    registered(url)
    answers.append((401, {"error": "unauthorized"}))
    assert run(project) == managed_services.UNAUTHORIZED_EXIT
    assert list((project / "report" / ".pdt" / "runs" / storage.RUN_ID / "output").iterdir())
    assert paths(seen) == ["/v1/events"]
    out = " ".join(capsys.readouterr().out.split())
    info = autoidm.files(CUSTOMER)[0]
    assert f"AutoIDM refused the registration ins_4f8a1c2e9b in {info}" in out
    assert "Run of report failed (unauthorized, exit code 77)" in out


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


def test_a_cloud_job_sends_with_the_registration_in_its_secret(project, backend, warned,
                                                               monkeypatch, capsys):
    url, seen, answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    monkeypatch.setenv("PDT_ENV_JSON", json.dumps({
        autoidm.ID_ENV: "ins_0000000007", autoidm.URL_ENV: url,
        autoidm.KEY_ENV: pem(Ed25519PrivateKey.generate()).decode()}))
    answers.append((401, {"error": "unauthorized"}))
    assert lifecycle.run_job("report", "aws", [sys.executable, "-c", WORK, "0"],
                             project / "report", deployed=True) == 77
    assert seen[0]["body"]["event"]["params"]["deployed"] is True
    out = " ".join(capsys.readouterr().out.split())
    assert ("AutoIDM refused the cloud registration ins_0000000007 of report "
            "(PDT_INSTALLATION_ID in the app's secret)") in out


def test_a_deploy_sends_app_deployed_with_the_schedule_and_the_place(project, backend, warned,
                                                                     monkeypatch):
    url, seen, _answers = backend
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    (project / "report" / "config.yml").write_text(
        "schedule: daily\ntimezone: America/New_York\nplatform:\n  provider: aws\n"
        "  region: us-east-2\n")
    registered(url)
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: 0)
    monkeypatch.setattr(config, "deployed_platform", lambda name: {
        "provider": "aws", "region": "us-east-2", "account": "123456789012"})
    asked = []
    monkeypatch.setattr(managed_services.subprocess, "run", lambda command, **kwargs: (
        asked.append(command) or subprocess.CompletedProcess(
            command, 0, "https://user:glpat-secret@gitlab.com/example/project.git\n", "")))
    assert deploy.provider_step("deploy", "aws", "report", True) == 0
    event = seen[0]["body"]["event"]
    assert event["name"] == "app:deployed"
    assert event["params"] == {"project": project.name, "app": "report", "provider": "aws",
                               "schedule": "0 0 * * *", "timezone": "America/New_York",
                               "place": "aws:123456789012:us-east-2",
                               "repository": "gitlab.com/example/project"}
    assert asked == [["git", "-C", str(project), "remote", "get-url", "origin"]]
    assert "glpat-secret" not in str(seen)


@pytest.mark.parametrize(("url", "found"), [
    ("git@gitlab.com:example/project.git", "gitlab.com/example/project"),
    ("https://user:token@gitlab.com/example/project.git", "gitlab.com/example/project"),
    ("ssh://git@gitlab.com:22/example/project.git", "gitlab.com/example/project"),
    ("https://github.com/example/project/", "github.com/example/project"),
    ("gitlab.com:group/sub/project", "gitlab.com/group/sub/project"),
    ("/srv/git/project.git", None),
    ("file:///srv/git/project.git", None),
])
def test_the_repository_is_the_origin_host_and_path_with_no_credentials(project, monkeypatch,
                                                                         url, found):
    monkeypatch.setattr(managed_services.subprocess, "run", lambda command, **kwargs: (
        subprocess.CompletedProcess(command, 0, url + "\n", "")))
    assert managed_services.repository(project) == found


def test_no_git_no_repository_or_no_origin_means_no_repository(project, monkeypatch):
    def no_git(command, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(managed_services.subprocess, "run", no_git)
    assert managed_services.repository(project) is None
    monkeypatch.setattr(managed_services.subprocess, "run", lambda command, **kwargs: (
        subprocess.CompletedProcess(command, 2, "", "error: No such remote 'origin'\n")))
    assert managed_services.repository(project) is None


def test_a_windows_app_has_no_place_and_its_time_zone_is_the_computers(project, monkeypatch):
    project_with(project, f"managed_services:\n  customer: {CUSTOMER}\n")
    sent = []
    sender = managed_services.Sender("report", managed_services.parse(
        {"customer": CUSTOMER}, "pdt.yml"), None)
    monkeypatch.setattr(sender, "event", lambda name, at, params: sent.append(params))
    monkeypatch.setattr("pdt.regions.local_timezone", lambda: "Europe/Berlin")
    monkeypatch.setattr(managed_services, "repository", lambda folder: None)
    sender.deployed({"app": "report"}, config.merged_app("report"), {"provider": "windows"})
    assert sent == [{"app": "report", "schedule": None, "timezone": "Europe/Berlin",
                     "place": None, "repository": None}]


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
    monkeypatch.setattr(lifecycle, "copy_output", lambda child, log: None)
    Started.calls = []
    assert lifecycle.run_job("report", "windows", ["app"], project / "report") == 0
    piped = "stdout" in Started.calls[0]
    assert piped == (case == "registered")


def test_autoidm_is_not_an_alerts_type():
    assert "autoidm" not in lifecycle.TRANSPORTS
    with pytest.raises(config.ConfigError, match="alerts type 'autoidm' is not known"):
        lifecycle.parse_entry({"type": "autoidm", "events": ["all"]}, "pdt.yml")
