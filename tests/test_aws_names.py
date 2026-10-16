from pdt import deploy_aws_batch


def test_every_per_app_aws_resource_carries_the_pdt_app_name():
    names = deploy_aws_batch.resource_names("x")
    for key, name in names.items():
        if key in ("image_tag", "log_group"):
            continue
        assert "pdt-x" in name, f"{key} is {name}"
