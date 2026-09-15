from pdt import deploy_google_cloud
from pdt.deploy_common import store_plan_lines, store_suffix


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


def test_the_grant_condition_limits_the_runner_to_the_app_folder():
    condition = deploy_google_cloud.store_condition("pdt-data-abc", "hello-world")
    assert condition == (
        'expression=resource.name.startsWith("projects/_/buckets/pdt-data-abc/objects/hello-world/")'
        ",title=pdt-hello-world")
