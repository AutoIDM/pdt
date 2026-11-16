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


def test_each_job_runs_as_its_own_service_account():
    assert (deploy_google_cloud.job_service_account("p", "Hello_World")
            == "pdt-job-hello-world@p.iam.gserviceaccount.com")
    long_a = deploy_google_cloud.job_service_account_id("a-very-long-application-name-one")
    long_b = deploy_google_cloud.job_service_account_id("a-very-long-application-name-two")
    assert len(long_a) == 30 and len(long_b) == 30 and long_a != long_b


def test_a_job_account_belongs_to_its_app_by_display_name():
    owned = deploy_google_cloud.job_service_account_owned
    assert owned({"displayName": "pdt job runner report"}, "report")
    assert not owned({"displayName": "pdt job runner other"}, "report")
    assert not owned({"displayName": "pdt job runner"}, "report")
    assert not owned(None, "report")


def test_a_jobs_account_is_one_that_pdt_made_for_apps_to_run_as():
    owned = deploy_google_cloud.run_account_owned
    assert owned({"displayName": "pdt job runner report"})
    assert owned({"displayName": "pdt job runner"})
    assert owned({"displayName": "pdt web app dashboard"})
    assert not owned({"displayName": "pdt job runners"})
    assert not owned({"displayName": "pdt image builder"})
