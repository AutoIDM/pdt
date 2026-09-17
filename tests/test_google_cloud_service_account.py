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
