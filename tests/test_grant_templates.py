import json
import subprocess
import sys

from pdt import deploy_aws, deploy_azure, deploy_azure_container_apps, deploy_google_cloud, grant_templates


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


def test_the_azure_template_assigns_the_roles_on_the_three_groups_only():
    template = grant_templates.azure_template()
    groups = [item for item in template["resources"]
              if item["type"] == "Microsoft.Resources/resourceGroups"]
    assert [item["name"] for item in groups] == [
        "pdt", deploy_azure.SHARED_GROUP, deploy_azure.STORE_GROUP]
    assert groups[0]["tags"]["autoidm-external-id"] == "[parameters('externalId')]"
    for group in groups:
        assert deploy_azure.retained(group)
    deployments = [item for item in template["resources"]
                   if item["type"] == "Microsoft.Resources/deployments"]
    assert [item["resourceGroup"] for item in deployments] == [
        "pdt", "pdt", deploy_azure.SHARED_GROUP, deploy_azure.STORE_GROUP]
    for deployment in deployments[1:]:
        assignments = deployment["properties"]["template"]["resources"]
        roles = [assignment["properties"]["roleDefinitionId"] for assignment in assignments]
        locks = deployment["resourceGroup"] in deploy_azure.GRANT_LOCK_GROUPS
        assert len(roles) == len(deploy_azure.GRANT_ROLES) + 1 + locks
        assert any(grant_templates.LOCK_ROLE_ID in role for role in roles) == locks
        assert any(deploy_azure.ROLE_IDS["Key Vault Secrets Officer"] in role for role in roles)
        assert {assignment["properties"]["principalId"] for assignment in assignments} == {
            "[parameters('identityPrincipalId')]"}
        assert assignments[-1]["properties"]["condition"] == deploy_azure.grant_condition()
        assert deployment["properties"]["parameters"]["identityPrincipalId"]["value"] == \
            grant_templates.IDENTITY_OUTPUT.format("principalId")


def test_the_azure_lock_role_holds_only_lock_rights_on_the_groups_that_deploy_locks():
    [role] = [item for item in grant_templates.azure_template()["resources"]
              if item["type"] == "Microsoft.Authorization/roleDefinitions"]
    assert role["name"] == f"[{grant_templates.LOCK_ROLE_ID}]"
    assert "subscriptionId" in role["properties"]["roleName"]
    assert role["properties"]["permissions"] == [{"actions": [
        "Microsoft.Authorization/locks/read", "Microsoft.Authorization/locks/write",
        "Microsoft.Authorization/locks/delete"], "notActions": []}]
    assert role["properties"]["assignableScopes"] == [
        "[format('{0}/resourceGroups/pdt', subscription().id)]",
        f"[format('{{0}}/resourceGroups/{deploy_azure.SHARED_GROUP}', subscription().id)]"]
    settings = {"registry": "r", "resource_group": "pdt", "subscription": "s",
                "environment": deploy_azure.parse_environment("", "eastus")}
    groups = {lock.resource.split("/resourceGroups/")[1].split("/")[0]
              for lock in deploy_azure_container_apps.shared_locks(settings, "pdt-app")}
    assert groups == set(deploy_azure.GRANT_LOCK_GROUPS)


def test_the_azure_identity_trusts_only_this_customers_subject_and_the_app_only_reads():
    template = grant_templates.azure_template()
    assert {"pdtIssuer", "pdtSubject", "principalObjectId"} <= set(template["parameters"])
    identity = next(item for item in template["resources"]
                    if item.get("name") == grant_templates.IDENTITY_DEPLOYMENT)
    assert identity["properties"]["parameters"]["subject"] == {"value": "[parameters('pdtSubject')]"}
    assert identity["properties"]["parameters"]["issuer"] == {"value": "[parameters('pdtIssuer')]"}
    resources = identity["properties"]["template"]["resources"]
    created, credential, reader = resources
    assert created["type"] == "Microsoft.ManagedIdentity/userAssignedIdentities"
    assert created["name"] == "pdt-autoidm"
    assert credential["properties"] == {"issuer": "[parameters('issuer')]",
                                        "subject": "[parameters('subject')]",
                                        "audiences": ["api://AzureADTokenExchange"]}
    assert reader["properties"]["principalId"] == "[parameters('principalObjectId')]"
    assert deploy_azure.ROLE_IDS["Reader"] in reader["properties"]["roleDefinitionId"]
    app_assignments = [
        assignment for item in template["resources"]
        if item["type"] == "Microsoft.Resources/deployments"
        for assignment in item["properties"]["template"]["resources"]
        if assignment["type"] == "Microsoft.Authorization/roleAssignments"
        and assignment["properties"]["principalId"] == "[parameters('principalObjectId')]"]
    assert app_assignments == [reader]
    assert template["outputs"]["identityClientId"]["value"] == \
        grant_templates.IDENTITY_OUTPUT.format("clientId")


def test_the_google_script_binds_each_role_and_limits_the_secret_roles_to_pdt():
    script = grant_templates.google_script()
    for role in deploy_google_cloud.GRANT_ROLES:
        assert role.replace("PROJECT_ID", "$PDT_PROJECT") in script
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
        *['title=pdt secrets,expression=resource.name.startsWith('
          '"projects/123456789012/secrets/pdt-")'] * 3]


def test_the_google_grant_creates_and_writes_pdt_secrets_and_reads_no_other(tmp_path):
    script = grant_templates.google_script()
    start = script.index("custom_role() {")
    lines = script[start:].splitlines()
    end = next(index for index, line in enumerate(lines)
               if line.startswith('if ! gcloud iam service-accounts describe "$RUNNER"'))
    harness = tmp_path / "roles.sh"
    harness.write_text(
        'gcloud() { [ "$1 $2 $3" = "iam roles describe" ] && return 1; echo "$*"; }\n'
        "PDT_PROJECT=my-project\nACCOUNT=pdt-autoidm@my-project.iam.gserviceaccount.com\n"
        "NUMBER=123456789012\n" + "\n".join(lines[:end]) + "\n")
    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    calls = proc.stdout.splitlines()
    created = {call.split()[3]: call.split("--permissions ")[1].split()[0]
               for call in calls if call.startswith("iam roles create")}
    assert created == {
        "pdtAutoidmSecretCreate": "secretmanager.secrets.create",
        "pdtAutoidmSecretPolicy": "secretmanager.secrets.getIamPolicy,"
                                  "secretmanager.secrets.setIamPolicy"}
    bindings = {call.split("--role ")[1].split()[0]: call.split("--condition")[1]
                for call in calls if call.startswith("projects add-iam-policy-binding")}
    secrets_only = ' title=pdt secrets,expression=resource.name.startsWith(' \
                   '"projects/123456789012/secrets/pdt-") --format=none'
    assert bindings["projects/my-project/roles/pdtAutoidmSecretCreate"] == "=None --format=none"
    assert bindings["roles/secretmanager.editor"] == secrets_only
    assert bindings["projects/my-project/roles/pdtAutoidmSecretPolicy"] == secrets_only
    assert bindings["roles/secretmanager.secretAccessor"] == secrets_only
    assert "roles/secretmanager.admin" not in bindings
    unconditional = [role for role, condition in bindings.items() if condition.startswith("=None")]
    assert not any("secretAccessor" in role or role.endswith("Policy") for role in unconditional)
