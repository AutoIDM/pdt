import signal
import sys

import pytest
from service_fixtures import COMMAND, CUSTOMER, FakePortal, copy_of

from pdt import hosted_runner, service_commands
from pdt.utils import autoidm

PLACE = "aws:123456789012:us-east-2"
CREDENTIALS = {"provider": "aws", "expires": "x", "aws": {
    "access_key_id": "AKIA", "secret_access_key": "secret-key-1", "session_token": "token-1",
    "region": "us-east-2"}}


@pytest.fixture
def portal(monkeypatch):
    found = FakePortal()
    monkeypatch.setenv(hosted_runner.CLAIM_ENV, "claim-1")
    monkeypatch.setenv(autoidm.URL_ENV, found.url)
    monkeypatch.setenv(hosted_runner.COMMAND_ENV, COMMAND)
    monkeypatch.setattr(hosted_runner, "OUTPUT_SECONDS", 0.2)
    monkeypatch.setattr(hosted_runner, "stop_at", lambda _seconds: None)
    yield found
    found.close()


def claim(portal, operation="list", args=None, value=None, limit=300):
    unit, data = copy_of("root", {"pdt.yml": b"platform:\n  provider: aws\n"})
    portal.zips[unit["sha256"]] = data
    unit["url"] = f"{portal.url}/zip/{unit['sha256']}"
    portal.routes["/v1/runner/claim"] = lambda _body: (200, {
        "customer_id": CUSTOMER, "place": PLACE, "operation": operation,
        "args": args or {"project": "example"}, "limit_seconds": limit, "run_token": "run-1",
        "credentials": CREDENTIALS, "copy": {"units": [unit]},
        "input": {"value": value} if value else None})
    portal.routes["/v1/runner/uploads"] = lambda body: (200, {
        "url": f"{portal.url}/bucket", "fields": {"key": f"k/{body['kind']}"},
        "key": f"k/{body['kind']}"})


def reports(portal):
    return portal.bodies("/v1/runner/report")


def result(portal):
    return next(body for body in reports(portal) if body["fact"] == "result")


def child(monkeypatch, code):
    monkeypatch.setattr(service_commands, "command_line",
                        lambda _operation, _args: [sys.executable, "-c", code])


def test_the_runner_claims_runs_list_with_the_credentials_and_reports(portal, monkeypatch):
    claim(portal)
    child(monkeypatch, "import os, pathlib\n"
                       "print(sorted(p.name for p in pathlib.Path('.').iterdir()))\n"
                       "print('key', os.environ['AWS_ACCESS_KEY_ID'])\n"
                       "print('claim', os.environ.get('PDT_RUNNER_CLAIM'))\n"
                       "print('boundary', os.environ['PDT_PERMISSIONS_BOUNDARY'])\n")
    assert hosted_runner.main([]) == 0
    claimed = portal.bodies("/v1/runner/claim")[0]
    assert claimed == {"version": 1, "command_id": COMMAND, "claim_token": "claim-1"}
    output = "".join(body["text"] for body in reports(portal) if body["fact"] == "output")
    assert "'pdt.yml'" in output and "key AKIA" in output and "claim None" in output
    assert "pdt-autoidm-boundary" in output
    found = result(portal)
    assert found["outcome"] == "done" and found["exit_code"] == 0 and found["log_key"] == "k/log"
    assert all(auth == "Bearer run-1" for path, _body, auth in portal.seen
               if path in ("/v1/runner/report", "/v1/runner/uploads"))
    assert hosted_runner.CLAIM_ENV not in __import__("os").environ


def test_a_secrets_set_value_goes_on_stdin_and_never_into_the_output(portal, monkeypatch):
    claim(portal, "secrets_set", {"project": "example", "app": "hr", "name": "TOKEN"},
          value="hunter2-value")
    child(monkeypatch, "import sys\nvalue = sys.stdin.read()\nprint('got', value)\n")
    assert hosted_runner.main([]) == 0
    output = "".join(body["text"] for body in reports(portal) if body["fact"] == "output")
    assert "got ***" in output and "hunter2" not in output
    assert result(portal)["outcome"] == "done"


def test_a_cancel_in_the_report_answer_stops_the_child(portal, monkeypatch):
    claim(portal, "run", {"project": "example", "app": "hr", "deployed": True})
    portal.routes["/v1/runner/report"] = lambda body: (200, {"cancel": body["fact"] == "output"})
    child(monkeypatch, "import time\nprint('working', flush=True)\ntime.sleep(60)\n")
    assert hosted_runner.main([]) == 0
    assert result(portal)["outcome"] == "cancelled"


@pytest.mark.parametrize("operation, args, says", [
    ("sync_up", {"project": "example"}, "does not run sync_up"),
    ("run", {"project": "example", "app": "hr", "deployed": False}, "only the deployed job"),
    ("list", {"project": "example", "extra": "x"}, "is not one this operation takes"),
])
def test_a_command_the_runner_must_not_run_is_refused(portal, monkeypatch, operation, args, says):
    claim(portal, operation, args)
    child(monkeypatch, "raise SystemExit('this must not run')")
    assert hosted_runner.main([]) == 0
    found = result(portal)
    assert found["outcome"] == "refused" and says in found["message"]


def test_a_claim_that_is_gone_exits_0(portal):
    portal.routes["/v1/runner/claim"] = lambda _body: (409, {"error": "already_claimed"})
    assert hosted_runner.main([]) == 0
    assert reports(portal) == []


def test_an_mr_run_runs_the_mr_child_with_the_output_folder(portal, monkeypatch):
    claim(portal, "run_mr", {"project": "example", "app": "hr", "mr_iid": "12", "sha": "a" * 40,
                             "app_args": "--full"})
    seen = {}
    real = hosted_runner.run_child

    def spy(runner, argv, project, env, *rest):
        seen["argv"], seen["env"] = argv, env
        return real(runner, [sys.executable, "-c",
                             "import os, pathlib\n"
                             "(pathlib.Path(os.environ['PDT_OUTPUT_DIR']) / 'r.csv').write_text('x')\n"],
                    project, env, *rest)

    monkeypatch.setattr(hosted_runner, "run_child", spy)
    assert hosted_runner.main([]) == 0
    assert seen["argv"][1:] == ["-m", "pdt.hosted_runner", "mr-run", "hr", "12"]
    assert seen["env"]["PDT_APP_ARGS"] == "--full" and seen["env"]["PDT_RUN_ID"] == COMMAND
    found = result(portal)
    assert found["outcome"] == "done" and found["artifact_key"] == "k/artifact"


@pytest.mark.parametrize("operation, args, kept", [
    ("deploy", {"project": "example", "app": "hr"}, "1"),
    ("plan", {"project": "example", "app": "hr"}, "1"),
    ("secrets_set", {"project": "example", "app": "hr", "name": "TOKEN"}, "None"),
    ("list", {"project": "example"}, "None"),
])
def test_only_deploy_and_plan_keep_the_deployed_secret(portal, monkeypatch, operation, args, kept):
    claim(portal, operation, args, value="hunter2-value" if operation == "secrets_set" else None)
    child(monkeypatch, "import os\nprint('keep', os.environ.get('PDT_KEEP_DEPLOYED_SECRET'))\n")
    assert hosted_runner.main([]) == 0
    output = "".join(body["text"] for body in reports(portal) if body["fact"] == "output")
    assert f"keep {kept}" in output


def test_the_child_user_gets_its_own_home_and_its_credential_folder(portal, monkeypatch, tmp_path):
    claim(portal)
    folder = tmp_path / "pdt-gcloud-1"
    folder.mkdir()
    monkeypatch.setattr(hosted_runner.grants, "credentials",
                        lambda place, answer: {hosted_runner.grants.GRANT_FOLDER_ENV: str(folder)})
    monkeypatch.setattr(hosted_runner, "run_user", lambda: "pdtjob")
    given = []
    monkeypatch.setattr(hosted_runner, "give_folder", lambda path, user: given.append((path, user)))
    monkeypatch.setattr(hosted_runner, "user_home", lambda user: f"/home/{user}")
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("UV_CACHE_DIR", "/root/.cache/uv")
    seen = {}
    real = hosted_runner.run_child

    def as_this_user(runner, argv, project, env, log, value, user, *rest):
        seen["env"] = env
        return real(runner, argv, project, env, log, value, None, *rest)

    monkeypatch.setattr(hosted_runner, "run_child", as_this_user)
    child(monkeypatch, "print('ok')")
    assert hosted_runner.main([]) == 0
    assert seen["env"]["HOME"] == "/home/pdtjob" and "UV_CACHE_DIR" not in seen["env"]
    assert (folder, "pdtjob") in given


def test_a_silent_child_still_learns_of_a_cancel(portal, monkeypatch):
    claim(portal, "run", {"project": "example", "app": "hr", "deployed": True})
    portal.routes["/v1/runner/report"] = lambda body: (200, {"cancel": body["fact"] == "output"})
    child(monkeypatch, "import time\ntime.sleep(5)\n")
    assert hosted_runner.main([]) == 0
    assert result(portal)["outcome"] == "cancelled"


def test_output_while_the_child_runs_is_one_report_per_turn(tmp_path, monkeypatch):
    log = tmp_path / "output.log"
    log.write_bytes(b"x" * (hosted_runner.service_runner.OUTPUT_LIMIT * 3))
    sent = []

    class Spy:
        seq = 0

        def report(self, **facts):
            sent.append(facts)
            return {"cancel": False}

    assert hosted_runner.send_output(Spy(), log, 0, [], False)[0] > 0
    assert len(sent) == 1


def test_a_log_over_the_upload_limit_is_not_read_whole(portal, monkeypatch):
    claim(portal, "secrets_set", {"project": "example", "app": "hr", "name": "TOKEN"},
          value="hunter2-value")
    monkeypatch.setattr(hosted_runner.managed_services, "MAX_UPLOAD_BYTES", 20000)
    child(monkeypatch, "import sys\nsys.stdin.read()\nprint('y' * 30000)\n"
                       "print('hunter2-value')\nprint('last line')\nraise SystemExit(3)\n")
    read = []
    real = hosted_runner.Path.read_bytes
    monkeypatch.setattr(hosted_runner.Path, "read_bytes",
                        lambda path: read.append(path.name) or real(path))
    assert hosted_runner.main([]) == 0
    found = result(portal)
    assert found["log_key"] is None and found["outcome"] == "failed"
    assert "last line" in found["message"] and "hunter2" not in found["message"]
    assert "output.log" not in read


def test_the_mr_child_runs_the_provider_verb_and_copies_its_output(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(hosted_runner.deploy, "mr_run", lambda app, iid: calls.append((app, iid)) or 0)
    monkeypatch.setattr(hosted_runner.run_output, "copy_run",
                        lambda app, identity, target: calls.append((app, identity, target)) or 1)
    monkeypatch.setenv("PDT_RUN_ID", COMMAND)
    monkeypatch.setenv("PDT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(hosted_runner.signal, "signal", lambda signum, handler: calls.append(signum))
    assert hosted_runner.main(["mr-run", "hr", "12"]) == 0
    assert calls == [signal.SIGTERM, ("hr", "12"), ("hr", COMMAND, tmp_path)]
