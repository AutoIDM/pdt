import subprocess

from pdt import deploy_google_cloud


def test_a_missing_service_account_is_found_by_listing(monkeypatch):
    calls = []

    def fake_list_json(*args):
        calls.append(args)
        return [{"email": "other@p.iam.gserviceaccount.com"}]

    def no_describe(*args):
        raise AssertionError("describe was called")

    monkeypatch.setattr(deploy_google_cloud, "list_json", fake_list_json)
    monkeypatch.setattr(deploy_google_cloud, "read_json_or_none", no_describe)
    assert deploy_google_cloud.service_account_or_none(
        "p", "pdt-runner@p.iam.gserviceaccount.com") is None
    assert calls == [("iam", "service-accounts", "list", "--project", "p")]


def test_an_existing_service_account_is_returned(monkeypatch):
    wanted = {"email": "pdt-runner@p.iam.gserviceaccount.com", "displayName": "pdt job runner"}
    monkeypatch.setattr(deploy_google_cloud, "list_json", lambda *a: [wanted])
    assert deploy_google_cloud.service_account_or_none("p", wanted["email"]) is wanted


def test_wait_for_service_account_polls_describe_until_it_is_ready(monkeypatch):
    attempts = iter([
        subprocess.CompletedProcess([], 1, "", "PERMISSION_DENIED"),
        subprocess.CompletedProcess([], 0, "{}", ""),
    ])
    calls = []
    sleeps = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return next(attempts)

    monkeypatch.setattr(deploy_google_cloud.subprocess, "run", run)
    monkeypatch.setattr(deploy_google_cloud.time, "sleep", sleeps.append)

    email = "pdt-runner@p.iam.gserviceaccount.com"
    deploy_google_cloud.wait_for_service_account("p", email)

    command = [deploy_google_cloud.GCLOUD, "iam", "service-accounts", "describe", email,
               "--project", "p", "--format=json"]
    assert [call[0] for call in calls] == [command, command]
    assert all(call[1]["stdin"] is subprocess.DEVNULL for call in calls)
    assert sleeps == [1]
