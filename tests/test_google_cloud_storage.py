import json
import subprocess

from pdt import deploy_google_cloud
from pdt.deploy_common import store_plan_lines, store_suffix

SA = "pdt-runner@my-project.iam.gserviceaccount.com"
FOLDER = "gs://pdt-data-abc/hello-world/"


class Result:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def record_gcloud(monkeypatch, answers):
    calls = []

    def fake_run(command, *args, **kwargs):
        calls.append(list(command)[1:])
        for prefix, result in answers:
            if list(command)[1:1 + len(prefix)] == list(prefix):
                return result
        return Result(1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def policy(*bindings):
    return Result(0, json.dumps({"bindings": list(bindings)}))


def test_the_bucket_name_derives_from_the_project_id():
    assert deploy_google_cloud.store_bucket("my-project") == "pdt-data-" + store_suffix("my-project")
    assert deploy_google_cloud.store_bucket("my-project") != deploy_google_cloud.store_bucket("other")


def test_the_storage_url_is_the_app_folder_with_a_trailing_slash():
    assert deploy_google_cloud.store_url("pdt-data-abc", "hello-world") == "gs://pdt-data-abc/hello-world/"


def test_plan_lines_use_the_shared_words():
    bucket = deploy_google_cloud.store_bucket("my-project")
    create, grant = store_plan_lines(f"bucket {bucket}", False, "pdt-runner", "hello-world")
    assert create == f"create bucket {bucket} (kept after destroy)"
    assert grant == f"grant pdt-runner write access to hello-world/ in bucket {bucket}"


def test_the_legacy_bucket_condition_names_the_app_folder():
    condition = deploy_google_cloud.store_condition("pdt-data-abc", "hello-world")
    assert condition == (
        'expression=resource.name.startsWith("projects/_/buckets/pdt-data-abc/objects/hello-world/")'
        ",title=pdt-hello-world")


def test_the_folder_grant_is_found_by_role_and_member(monkeypatch):
    get_policy = ["storage", "managed-folders", "get-iam-policy", FOLDER]
    binding = {"role": deploy_google_cloud.STORE_ROLE, "members": [f"serviceAccount:{SA}"]}
    record_gcloud(monkeypatch, [(get_policy, policy(binding))])
    assert deploy_google_cloud.folder_grant_exists("pdt-data-abc", "hello-world", SA)
    record_gcloud(monkeypatch, [(get_policy, Result(0, "{}"))])
    assert not deploy_google_cloud.folder_grant_exists("pdt-data-abc", "hello-world", SA)
    record_gcloud(monkeypatch, [])
    assert not deploy_google_cloud.folder_grant_exists("pdt-data-abc", "hello-world", SA)


def test_the_legacy_bucket_grant_is_found_by_title_role_and_member(monkeypatch):
    get_policy = ["storage", "buckets", "get-iam-policy", "gs://pdt-data-abc"]
    binding = {"role": deploy_google_cloud.STORE_ROLE, "members": [f"serviceAccount:{SA}"],
               "condition": {"title": "pdt-hello-world"}}
    record_gcloud(monkeypatch, [(get_policy, policy(binding))])
    assert deploy_google_cloud.bucket_grant_exists("pdt-data-abc", "hello-world", SA)
    assert not deploy_google_cloud.bucket_grant_exists("pdt-data-abc", "other-app", SA)
    record_gcloud(monkeypatch, [(get_policy, policy({**binding, "condition": None}))])
    assert not deploy_google_cloud.bucket_grant_exists("pdt-data-abc", "hello-world", SA)


GRANT = ["storage", "managed-folders", "add-iam-policy-binding", FOLDER,
         "--member", f"serviceAccount:{SA}", "--role", deploy_google_cloud.STORE_ROLE]


def test_granting_store_access_binds_the_existing_folder(monkeypatch):
    calls = record_gcloud(monkeypatch, [
        (["storage", "managed-folders", "describe"], Result(0, "{}")),
        (["storage", "managed-folders", "add-iam-policy-binding"], Result(0)),
    ])
    deploy_google_cloud.grant_store_access("pdt-data-abc", "hello-world", SA)
    assert calls == [["storage", "managed-folders", "describe", FOLDER, "--format=json"], GRANT]


def test_granting_store_access_creates_a_missing_folder_first(monkeypatch):
    calls = record_gcloud(monkeypatch, [
        (["storage", "managed-folders", "create"], Result(0)),
        (["storage", "managed-folders", "add-iam-policy-binding"], Result(0)),
    ])
    deploy_google_cloud.grant_store_access("pdt-data-abc", "hello-world", SA)
    assert calls == [["storage", "managed-folders", "describe", FOLDER, "--format=json"],
                     ["storage", "managed-folders", "create", FOLDER], GRANT]


def test_revoking_store_access_removes_the_folder_and_legacy_bucket_bindings(monkeypatch):
    calls = record_gcloud(monkeypatch, [(["storage"], Result(0))])
    deploy_google_cloud.revoke_store_access("pdt-data-abc", "hello-world", SA,
                                            folder=True, bucket_binding=True)
    member = ["--member", f"serviceAccount:{SA}", "--role", deploy_google_cloud.STORE_ROLE]
    assert calls == [
        ["storage", "managed-folders", "remove-iam-policy-binding", FOLDER, *member],
        ["storage", "buckets", "remove-iam-policy-binding", "gs://pdt-data-abc", *member,
         "--condition", deploy_google_cloud.store_condition("pdt-data-abc", "hello-world")],
    ]


def test_revoking_store_access_touches_only_the_bindings_that_exist(monkeypatch):
    calls = record_gcloud(monkeypatch, [(["storage"], Result(0))])
    deploy_google_cloud.revoke_store_access("pdt-data-abc", "hello-world", SA,
                                            folder=True, bucket_binding=False)
    assert [call[:3] for call in calls] == [["storage", "managed-folders", "remove-iam-policy-binding"]]
    calls.clear()
    deploy_google_cloud.revoke_store_access("pdt-data-abc", "hello-world", SA,
                                            folder=False, bucket_binding=True)
    assert [call[:3] for call in calls] == [["storage", "buckets", "remove-iam-policy-binding"]]
