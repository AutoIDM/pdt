from pdt import deploy_aws_fargate


def test_every_per_app_aws_resource_carries_the_pdt_app_name():
    names = deploy_aws_fargate.resource_names("x")
    for key, name in names.items():
        if key in ("image_tag", "legacy_log_group"):
            continue
        assert "pdt-x" in name, f"{key} is {name}"
