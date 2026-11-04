import ast
from pathlib import Path

import pytest

from pdt import deploy_common


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setenv(deploy_common.KEEP_SECRET_ENV, "1")


def test_a_runner_deploy_keeps_a_paused_job_paused(runner):
    app = {"pause": False}
    deploy_common.keep_paused(app, True, lambda: True)
    assert app["pause"] is True


def test_a_runner_deploy_of_a_running_job_leaves_it_running(runner):
    app = {"pause": False}
    deploy_common.keep_paused(app, True, lambda: False)
    assert app["pause"] is False


def test_a_new_job_follows_the_config_and_reads_nothing(runner):
    app = {"pause": False}
    deploy_common.keep_paused(app, False, lambda: pytest.fail("read the cloud"))
    assert app["pause"] is False


def test_a_config_that_pauses_needs_no_read(runner):
    app = {"pause": True}
    deploy_common.keep_paused(app, True, lambda: pytest.fail("read the cloud"))
    assert app["pause"] is True


def test_a_deploy_from_a_person_follows_config_yml(monkeypatch):
    monkeypatch.delenv(deploy_common.KEEP_SECRET_ENV, raising=False)
    app = {"pause": False}
    deploy_common.keep_paused(app, True, lambda: pytest.fail("read the cloud"))
    assert app["pause"] is False


@pytest.mark.parametrize("module", ["deploy_aws_batch.py", "deploy_azure_container_apps.py",
                                    "deploy_google_cloud.py"])
def test_each_cloud_deploy_applies_the_rule(module):
    source = (Path(deploy_common.__file__).parent / module).read_text(encoding="utf-8")
    deploy = next(node for node in ast.parse(source).body
                  if isinstance(node, ast.FunctionDef) and node.name == "deploy")
    calls = [node for node in ast.walk(deploy) if isinstance(node, ast.Call)
             and getattr(node.func, "id", None) == "keep_paused"]
    assert len(calls) == 1
