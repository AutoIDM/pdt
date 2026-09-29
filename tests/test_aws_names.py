from pdt import deploy_aws_batch


def test_every_per_app_aws_resource_carries_the_pdt_app_name():
    names = deploy_aws_batch.resource_names("x")
    for key, name in names.items():
        if key in ("image_tag", "log_group"):
            continue
        assert "pdt-x" in name, f"{key} is {name}"


def test_the_log_group_and_the_job_definition_follow_the_project_shape():
    names = deploy_aws_batch.resource_names("x")
    assert names["log_group"] == "/pdt/x"
    assert names["job_definition"] == names["schedule"] == "pdt-x"


def test_the_shared_batch_resources_are_both_named_pdt():
    assert deploy_aws_batch.JOB_QUEUE.name == "pdt"
    assert deploy_aws_batch.COMPUTE_ENVIRONMENT.name == "pdt"
