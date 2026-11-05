import json
import subprocess
import sys

from pdt import deploy_aws, deploy_azure, deploy_google_cloud, grant_templates


def actions(template: dict) -> dict[str, list]:
    found: dict[str, list] = {}
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::IAM::ManagedPolicy" or resource["Properties"][
                "ManagedPolicyName"] == deploy_aws.GRANT_BOUNDARY:
            continue
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            if statement["Effect"] == "Allow":
                for action in statement["Action"]:
                    found.setdefault(action, []).extend(statement["Resource"])
    return found


def test_the_module_writes_three_files(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "pdt.grant_templates", str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "aws-grant.yaml", "azure-grant.json", "google-cloud-grant.sh"]
    json.loads((tmp_path / "aws-grant.yaml").read_text())
    json.loads((tmp_path / "azure-grant.json").read_text())


def test_the_aws_role_trusts_only_the_broker_with_the_external_id():
    role = grant_templates.aws_template()["Resources"]["Role"]["Properties"]
    assert role["RoleName"] == "pdt-autoidm" and role["MaxSessionDuration"] == 3600
    [statement] = role["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"AWS": {"Ref": "BrokerRoleArn"}}
    assert statement["Condition"] == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}


def test_the_aws_grant_can_kill_and_read_and_write_only_pdt_secrets():
    found = actions(grant_templates.aws_template())
    assert "batch:TerminateJob" in found
    for action in ("secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"):
        assert found[action] == [{"Fn::Sub": "arn:aws:secretsmanager:*:${AWS::AccountId}:secret:pdt-*"}]
    assert "iam:SimulatePrincipalPolicy" not in found
    assert {"logs:GetLogEvents", "s3:GetObject"} <= set(found)


def test_each_account_value_is_a_substitution():
    text = json.dumps(grant_templates.aws_template())
    assert "${AWS::AccountId}" in text
    for resource in grant_templates.aws_template()["Resources"].values():
        for statement in resource["Properties"].get("PolicyDocument", {}).get("Statement", []):
            for item in statement["Resource"]:
                assert isinstance(item, dict) or "${" not in item


def test_the_azure_template_assigns_the_roles_on_the_two_groups_only():
    template = grant_templates.azure_template()
    groups = [item for item in template["resources"]
              if item["type"] == "Microsoft.Resources/resourceGroups"]
    assert [item["name"] for item in groups] == ["pdt", deploy_azure.SHARED_GROUP]
    assert groups[0]["tags"]["autoidm-external-id"] == "[parameters('externalId')]"
    deployments = [item for item in template["resources"]
                   if item["type"] == "Microsoft.Resources/deployments"]
    assert [item["resourceGroup"] for item in deployments] == ["pdt", deploy_azure.SHARED_GROUP]
    roles = [assignment["properties"]["roleDefinitionId"]
             for assignment in deployments[0]["properties"]["template"]["resources"]]
    assert len(roles) == len(deploy_azure.GRANT_ROLES) + 1
    assert any(deploy_azure.ROLE_IDS["Key Vault Secrets Officer"] in role for role in roles)
    administrator = deployments[0]["properties"]["template"]["resources"][-1]["properties"]
    assert administrator["condition"] == deploy_azure.grant_condition()


def test_the_google_script_binds_each_role_and_limits_the_secret_roles_to_pdt():
    script = grant_templates.google_script()
    for role in deploy_google_cloud.GRANT_ROLES:
        assert role in script
    assert "roles/logging.viewer" in script
    assert 'resource.name.startsWith(\\"projects/$NUMBER/secrets/pdt-\\")' in script
    assert "secretmanager.secretAccessor" in script
    assert "create-aws autoidm" in script and "roles/iam.workloadIdentityUser" in script
    assert "autoidm-external-id=$PDT_EXTERNAL_ID" in script
    assert "Project number: $NUMBER" in script and "$PDT_AWS_ROLE" in script


def test_the_shell_puts_the_project_number_in_each_secret_condition(tmp_path):
    script = grant_templates.google_script()
    start = script.index("grant() {")
    lines = script[start:].splitlines()
    end = next(index for index, line in enumerate(lines) if line.startswith("if ! gcloud iam"))
    harness = tmp_path / "conditions.sh"
    harness.write_text(
        'gcloud() { printf "%s\\n" "$@" | grep "^title="; }\n'
        "PDT_PROJECT=my-project\nACCOUNT=pdt-autoidm@my-project.iam.gserviceaccount.com\n"
        "NUMBER=123456789012\n" + "\n".join(lines[:end]) + "\n")
    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    conditions = proc.stdout.splitlines()
    assert conditions == [
        'title=pdt buckets,expression=resource.name.startsWith("projects/_/buckets/pdt-")',
        'title=pdt secrets,expression=resource.name.startsWith('
        '"projects/123456789012/secrets/pdt-")',
        'title=pdt secrets,expression=resource.name.startsWith('
        '"projects/123456789012/secrets/pdt-")']
