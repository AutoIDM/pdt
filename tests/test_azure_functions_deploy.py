import sys
import time

import pytest

from pdt import deploy_azure_functions as azf

CRON = "0 0 * * * *"
KUDU_FAIL = """\
import os, sys
state = os.environ["FAKE_AZ_STATE"]
with open(state, "a") as handle:
    handle.write("x")
if len(open(state).read()) <= int(os.environ.get("FAKE_AZ_FAILURES", "99")):
    sys.stderr.write("ERROR: Zip deployment failed. {'status_text': "
                     "'[KuduSpecializer] Kudu has been restarted during "
                     "deployment d1. Kudu Restart Count: 1.'}\\n")
    sys.exit(1)
sys.exit(0)
"""
OTHER_FAIL = """\
import os, sys
state = os.environ["FAKE_AZ_STATE"]
with open(state, "a") as handle:
    handle.write("x")
sys.stderr.write("ERROR: Zip deployment failed. Oryx build exited with 1.\\n")
sys.exit(1)
"""


def fake_az(monkeypatch, tmp_path, script, failures=None):
    state = tmp_path / "calls"
    state.write_text("")
    monkeypatch.setenv("FAKE_AZ_STATE", str(state))
    if failures is not None:
        monkeypatch.setenv("FAKE_AZ_FAILURES", str(failures))
    monkeypatch.setattr(azf, "AZ", [sys.executable, "-c", script])
    return state


def test_unchanged_settings_need_no_write():
    existing = {"PDT_SCHEDULE": CRON, "PDT_ENV_JSON": "@Microsoft.KeyVault(x)"}
    assert azf.settings_changes(existing, dict(existing)) == (False, [])


def test_a_changed_schedule_needs_a_write():
    assert azf.settings_changes(
        {"PDT_SCHEDULE": CRON}, {"PDT_SCHEDULE": "0 30 * * * *"}) == (True, [])


def test_a_removed_secret_is_deleted_only_when_present():
    desired = {"PDT_SCHEDULE": CRON}
    with_secret = {"PDT_SCHEDULE": CRON, "PDT_ENV_JSON": "x"}
    assert azf.settings_changes(with_secret, desired) == (False, ["PDT_ENV_JSON"])
    assert azf.settings_changes(desired, desired) == (False, [])


def test_settings_azure_manages_are_left_alone():
    existing = {"PDT_SCHEDULE": CRON, "AzureWebJobsStorage": "connection"}
    assert azf.settings_changes(existing, {"PDT_SCHEDULE": CRON}) == (False, [])


def test_the_upload_retries_after_a_kudu_restart(tmp_path, monkeypatch):
    state = fake_az(monkeypatch, tmp_path, KUDU_FAIL, failures=1)
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    azf.upload_package("app", "rg", tmp_path / "function.zip")
    assert state.read_text() == "xx"
    assert waits == [30]


def test_the_upload_gives_up_after_three_kudu_restarts(tmp_path, monkeypatch):
    state = fake_az(monkeypatch, tmp_path, KUDU_FAIL)
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    with pytest.raises(SystemExit):
        azf.upload_package("app", "rg", tmp_path / "function.zip")
    assert state.read_text() == "xxx"
    assert waits == [30, 60]


def test_any_other_upload_failure_does_not_retry(tmp_path, monkeypatch):
    state = fake_az(monkeypatch, tmp_path, OTHER_FAIL)
    monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(SystemExit):
        azf.upload_package("app", "rg", tmp_path / "function.zip")
    assert state.read_text() == "x"
