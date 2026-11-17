import subprocess

import pytest

from pdt import deploy_google_cloud
from pdt.deploy_google_cloud import not_found


def test_a_missing_cloud_run_job_counts_as_not_found():
    assert not_found("ERROR: (gcloud.run.jobs.describe) Cannot find job [pdt-app].")


def test_the_not_found_wordings_still_count():
    assert not_found("ERROR: (gcloud.secrets.describe) NOT_FOUND: Secret [x] not found.")


def test_a_permission_error_still_raises():
    assert not not_found("ERROR: (gcloud.run.jobs.describe) PERMISSION_DENIED")


def test_read_json_or_none_answers_none_for_a_missing_resource(monkeypatch):
    monkeypatch.setattr(deploy_google_cloud, "gcloud", lambda *args: subprocess.CompletedProcess(
        args, 1, "", "ERROR: (gcloud.artifacts.repositories.describe) NOT_FOUND: Requested entity was not found."))
    assert deploy_google_cloud.read_json_or_none("artifacts", "repositories", "describe", "pdt") is None


def test_read_json_or_none_shows_the_gcloud_error_and_the_full_command(monkeypatch, capsys):
    stderr = ('ERROR: (gcloud.artifacts.repositories.describe) INVALID_ARGUMENT: '
              'Request contains an invalid argument.')
    monkeypatch.setattr(deploy_google_cloud, "gcloud",
                        lambda *args: subprocess.CompletedProcess(args, 1, "", stderr))
    with pytest.raises(SystemExit):
        deploy_google_cloud.read_json_or_none(
            "artifacts", "repositories", "describe", "pdt",
            "--location", "eastus2", "--project", "my-project")
    out = capsys.readouterr().out
    assert stderr in out
    assert ("pdt gcloud artifacts repositories describe pdt --location eastus2 "
            "--project my-project --format=json failed with exit code 1") in out
    assert "resource ownership" not in out
