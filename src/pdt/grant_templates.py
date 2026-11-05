"""`python -m pdt.grant_templates DIR`: write the three grant templates from pdt's own policy code.

- `aws-grant.yaml`: a CloudFormation template (JSON is YAML) with the role `pdt-autoidm`, which
  the broker role of AutoIDM may assume with the customer's external ID for at most one hour, the
  policies of `deploy_aws.grant_policy_parts` for every region, and the boundary
  `pdt-autoidm-boundary` that each role it makes must carry.
- `azure-grant.json`: an ARM template at subscription scope with the resource groups `pdt`,
  `pdt-shared`, and `pdt-data`, tagged `pdt-lifecycle=retain` so that no destroy deletes them, the
  external ID tag on `pdt`, the managed identity `pdt-autoidm` in `pdt` with a federated credential
  for AutoIDM's token of this customer only (`pdtIssuer`, `pdtSubject`), and the role assignments
  of the grant on the three groups for that identity. The custom role
  `pdt-autoidm-locks-<subscription>` (role names are unique in a tenant) adds the management
  lock rights on `pdt` and `pdt-shared` only, the groups of the locks deploy holds. AutoIDM's multi-tenant
  service principal `principalObjectId` gets only Reader on `pdt`, so a token for another
  customer cannot act here. The output `identityClientId` names the identity.
- `google-cloud-grant.sh`: a Cloud Shell script that makes the service account `pdt-autoidm` with
  the external ID in its description, a workload identity pool with a provider for AutoIDM's AWS
  account, the binding that lets AutoIDM's credential role act as the account, and `GRANT_ROLES`.

The backend holds the generated files, and the portal links them from the Connect page.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pdt import deploy_aws, deploy_azure, deploy_google_cloud, grants

ACCOUNT = "${AWS::AccountId}"
AWS_FILE = "aws-grant.yaml"
AZURE_FILE = "azure-grant.json"
GOOGLE_FILE = "google-cloud-grant.sh"
MANAGED_TAG = {"Key": "managed-by", "Value": "pdt"}
IDENTITY = "pdt-autoidm"
IDENTITY_DEPLOYMENT = "pdt-autoidm-identity"
LOCK_ROLE = "pdt-autoidm-locks"
LOCK_ROLE_ID = f"guid(subscription().id, '{LOCK_ROLE}')"
IDENTITY_OUTPUT = ("[reference(resourceId('pdt', 'Microsoft.Resources/deployments', "
                   f"'{IDENTITY_DEPLOYMENT}'), '2022-09-01').outputs.{{}}.value]")
NESTED_SCHEMA = "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"


def substituted(value: object) -> object:
    if isinstance(value, dict):
        return {key: substituted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [substituted(item) for item in value]
    if isinstance(value, str) and ACCOUNT in value:
        return {"Fn::Sub": value}
    return value


def aws_template() -> dict:
    parts = deploy_aws.grant_policy_parts(ACCOUNT, "*")
    policies = {f"GrantPolicy{index + 1}": {
        "Type": "AWS::IAM::ManagedPolicy",
        "Properties": {"ManagedPolicyName": f"{deploy_aws.GRANT_ROLE}-{index + 1}",
                       "Description": "Managed by PDT",
                       "PolicyDocument": substituted(document)}}
        for index, document in enumerate(parts)}
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Lets AutoIDM deploy and run the jobs of pdt in this account (managed-by=pdt).",
        "Parameters": {
            "ExternalId": {"Type": "String", "AllowedPattern": "[0-9a-f]{32}",
                           "Description": "The external ID of your AutoIDM customer"},
            "BrokerRoleArn": {"Type": "String", "AllowedPattern": "arn:aws:iam::[0-9]{12}:role/.+",
                              "Description": "The AutoIDM role that may assume pdt-autoidm"}},
        "Resources": {
            "Boundary": {"Type": "AWS::IAM::ManagedPolicy", "Properties": {
                "ManagedPolicyName": deploy_aws.GRANT_BOUNDARY, "Description": "Managed by PDT",
                "PolicyDocument": substituted(deploy_aws.grant_boundary(ACCOUNT))}},
            **policies,
            "Role": {"Type": "AWS::IAM::Role", "Properties": {
                "RoleName": deploy_aws.GRANT_ROLE, "Description": "Managed by PDT",
                "MaxSessionDuration": deploy_aws.GRANT_SESSION_SECONDS,
                "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{
                    "Effect": "Allow", "Principal": {"AWS": {"Ref": "BrokerRoleArn"}},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}}]},
                "ManagedPolicyArns": [{"Ref": name} for name in policies],
                "Tags": [MANAGED_TAG, {"Key": grants.EXTERNAL_ID_TAG,
                                       "Value": {"Ref": "ExternalId"}}]}}},
        "Outputs": {"RoleArn": {"Value": {"Fn::GetAtt": ["Role", "Arn"]}}}}


def lock_role() -> dict:
    scopes = [f"[format('{{0}}/resourceGroups/{group}', subscription().id)]"
              for group in deploy_azure.GRANT_LOCK_GROUPS]
    return {
        "type": "Microsoft.Authorization/roleDefinitions", "apiVersion": "2022-04-01",
        "name": f"[{LOCK_ROLE_ID}]",
        "dependsOn": [f"[subscriptionResourceId('Microsoft.Resources/resourceGroups', '{group}')]"
                      for group in deploy_azure.GRANT_LOCK_GROUPS],
        "properties": {
            "roleName": f"[format('{LOCK_ROLE}-{{0}}', subscription().subscriptionId)]",
            "description": "Lets pdt-autoidm hold the delete locks of pdt deploys (managed-by=pdt)",
            "type": "CustomRole",
            "permissions": [{"actions": list(deploy_azure.GRANT_LOCK_ACTIONS), "notActions": []}],
            "assignableScopes": scopes}}


def role_assignment(role: str, principal: str, name: str, condition: str | None = None) -> dict:
    role_id = LOCK_ROLE_ID if role == LOCK_ROLE else f"'{deploy_azure.ROLE_IDS[role]}'"
    properties = {
        "roleDefinitionId": f"[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
                            f"{role_id})]",
        "principalId": principal, "principalType": "ServicePrincipal"}
    if condition:
        properties.update({"condition": condition, "conditionVersion": "2.0"})
    return {"type": "Microsoft.Authorization/roleAssignments", "apiVersion": "2022-04-01",
            "name": f"[guid(resourceGroup().id, {name}, {role_id})]", "properties": properties}


def nested(name: str, group: str, depends: list[str], parameters: dict[str, str],
           resources: list[dict], outputs: dict | None = None) -> dict:
    template = {"$schema": NESTED_SCHEMA, "contentVersion": "1.0.0.0",
                "parameters": {key: {"type": "string"} for key in parameters},
                "resources": resources}
    if outputs:
        template["outputs"] = outputs
    return {
        "type": "Microsoft.Resources/deployments", "apiVersion": "2022-09-01",
        "name": name, "resourceGroup": group, "dependsOn": depends,
        "properties": {
            "mode": "Incremental", "expressionEvaluationOptions": {"scope": "inner"},
            "parameters": {key: {"value": value} for key, value in parameters.items()},
            "template": template}}


def azure_identity() -> dict:
    identity_id = f"[resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', '{IDENTITY}')]"
    return nested(
        IDENTITY_DEPLOYMENT, "pdt",
        ["[subscriptionResourceId('Microsoft.Resources/resourceGroups', 'pdt')]"],
        {"issuer": "[parameters('pdtIssuer')]", "subject": "[parameters('pdtSubject')]",
         "principalObjectId": "[parameters('principalObjectId')]"},
        [{"type": "Microsoft.ManagedIdentity/userAssignedIdentities", "apiVersion": "2023-01-31",
          "name": IDENTITY, "location": "[resourceGroup().location]",
          "tags": {"managed-by": "pdt"}},
         {"type": "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials",
          "apiVersion": "2023-01-31", "name": f"{IDENTITY}/autoidm", "dependsOn": [identity_id],
          "properties": {"issuer": "[parameters('issuer')]", "subject": "[parameters('subject')]",
                         "audiences": ["api://AzureADTokenExchange"]}},
         role_assignment("Reader", "[parameters('principalObjectId')]",
                         "parameters('principalObjectId')")],
        {"principalId": {"type": "string",
                         "value": f"[reference({identity_id[1:-1]}, '2023-01-31').principalId]"},
         "clientId": {"type": "string",
                      "value": f"[reference({identity_id[1:-1]}, '2023-01-31').clientId]"}})


def azure_assignments(group: str) -> dict:
    roles = [(role, None) for role in deploy_azure.GRANT_ROLES]
    depends = [f"[subscriptionResourceId('Microsoft.Resources/resourceGroups', '{group}')]",
               f"[resourceId('pdt', 'Microsoft.Resources/deployments', '{IDENTITY_DEPLOYMENT}')]"]
    if group in deploy_azure.GRANT_LOCK_GROUPS:
        roles.append((LOCK_ROLE, None))
        depends.append(f"[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
                       f"{LOCK_ROLE_ID})]")
    roles.append(("Role Based Access Control Administrator", deploy_azure.grant_condition()))
    return nested(
        f"pdt-autoidm-{group}", group, depends,
        {"identityPrincipalId": IDENTITY_OUTPUT.format("principalId")},
        [role_assignment(role, "[parameters('identityPrincipalId')]", f"'{IDENTITY}'", condition)
         for role, condition in roles])


def azure_template() -> dict:
    groups = []
    for group in deploy_azure.GRANT_GROUPS:
        tags = dict(deploy_azure.STORE_TAGS)
        if group == "pdt":
            tags[grants.EXTERNAL_ID_TAG] = "[parameters('externalId')]"
        groups.append({"type": "Microsoft.Resources/resourceGroups", "apiVersion": "2022-09-01",
                       "name": group, "location": "[parameters('location')]", "tags": tags})
    return {
        "$schema": "https://schema.management.azure.com/schemas/2018-05-01/"
                   "subscriptionDeploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "principalObjectId": {"type": "string", "metadata": {
                "description": "The object ID of AutoIDM's service principal in your tenant"}},
            "externalId": {"type": "string", "metadata": {
                "description": "The external ID of your AutoIDM customer"}},
            "pdtIssuer": {"type": "string", "metadata": {
                "description": "The issuer of AutoIDM's AWS identity tokens"}},
            "pdtSubject": {"type": "string", "metadata": {
                "description": "The subject of AutoIDM's identity tokens for your customer"}},
            "location": {"type": "string", "defaultValue": "[deployment().location]"}},
        "resources": [*groups, lock_role(), azure_identity(),
                      *(azure_assignments(group) for group in deploy_azure.GRANT_GROUPS)],
        "outputs": {"identityClientId": {"type": "string",
                                         "value": IDENTITY_OUTPUT.format("clientId")}}}


def shell_quoted(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    return escaped.replace("$", "\\$").replace("PROJECT_NUMBER", "$NUMBER")


def google_script() -> str:
    roles = " ".join(deploy_google_cloud.GRANT_ROLES).replace("PROJECT_ID", "$PDT_PROJECT")
    conditional = "\n".join(
        f'grant {role.replace("PROJECT_ID", "$PDT_PROJECT")} "{title}" "{shell_quoted(expression)}"'
        for role, title, expression in deploy_google_cloud.GRANT_CONDITIONAL_ROLES)
    custom = "\n".join(
        f"custom_role {role} {','.join(permissions)}"
        for role, permissions in deploy_google_cloud.GRANT_CUSTOM_ROLES.items())
    name = deploy_google_cloud.GRANT_NAME
    runner = deploy_google_cloud.GRANT_RUNNER
    builder = deploy_google_cloud.BUILDER
    return f"""#!/usr/bin/env bash
# Lets AutoIDM deploy and run the jobs of pdt in one Google Cloud project (managed-by=pdt).
# Run it in Cloud Shell with the values from the AutoIDM Connect page:
#   PDT_PROJECT=... PDT_EXTERNAL_ID=... PDT_AWS_ACCOUNT=... PDT_AWS_ROLE=... bash {GOOGLE_FILE}
set -euo pipefail
: "${{PDT_PROJECT:?set PDT_PROJECT to the Google Cloud project ID}}"
: "${{PDT_EXTERNAL_ID:?set PDT_EXTERNAL_ID to the external ID of your AutoIDM customer}}"
: "${{PDT_AWS_ACCOUNT:?set PDT_AWS_ACCOUNT to the AWS account of AutoIDM}}"
: "${{PDT_AWS_ROLE:?set PDT_AWS_ROLE to the name of the AutoIDM credential role}}"
NUMBER="$(gcloud projects describe "$PDT_PROJECT" --format='value(projectNumber)')"
ACCOUNT="{name}@$PDT_PROJECT.iam.gserviceaccount.com"
RUNNER="{runner}@$PDT_PROJECT.iam.gserviceaccount.com"
BUILDER="{builder}@$PDT_PROJECT.iam.gserviceaccount.com"
POOL="{name}"
DESCRIPTION="managed-by=pdt {grants.EXTERNAL_ID_TAG}=$PDT_EXTERNAL_ID"
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \\
  cloudresourcemanager.googleapis.com --project "$PDT_PROJECT"
if gcloud iam service-accounts describe "$ACCOUNT" --project "$PDT_PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts update "$ACCOUNT" --project "$PDT_PROJECT" --description "$DESCRIPTION"
else
  gcloud iam service-accounts create {name} --project "$PDT_PROJECT" \\
    --display-name "AutoIDM access for pdt" --description "$DESCRIPTION"
fi
if ! gcloud iam workload-identity-pools describe "$POOL" --project "$PDT_PROJECT" \\
    --location global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL" --project "$PDT_PROJECT" --location global \\
    --display-name "AutoIDM" --description "managed-by=pdt"
fi
if ! gcloud iam workload-identity-pools providers describe autoidm --project "$PDT_PROJECT" \\
    --location global --workload-identity-pool "$POOL" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-aws autoidm --project "$PDT_PROJECT" \\
    --location global --workload-identity-pool "$POOL" --account-id "$PDT_AWS_ACCOUNT" \\
    --attribute-mapping "google.subject=assertion.arn,attribute.aws_role=assertion.arn.extract('assumed-role/{{role}}/')" \\
    --attribute-condition "attribute.aws_role == '$PDT_AWS_ROLE'"
fi
gcloud iam service-accounts add-iam-policy-binding "$ACCOUNT" --project "$PDT_PROJECT" \\
  --role roles/iam.workloadIdentityUser \\
  --member "principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.aws_role/$PDT_AWS_ROLE"
custom_role() {{
  if gcloud iam roles describe "$1" --project "$PDT_PROJECT" >/dev/null 2>&1; then
    gcloud iam roles update "$1" --project "$PDT_PROJECT" --permissions "$2" --quiet --format=none
  else
    gcloud iam roles create "$1" --project "$PDT_PROJECT" --title "$1" \\
      --description "managed-by=pdt" --permissions "$2" --stage GA --format=none
  fi
}}
{custom}
for role in {roles}; do
  gcloud projects add-iam-policy-binding "$PDT_PROJECT" --member "serviceAccount:$ACCOUNT" \\
    --role "$role" --condition=None --format=none
done
grant() {{
  gcloud projects add-iam-policy-binding "$PDT_PROJECT" --member "serviceAccount:$ACCOUNT" \\
    --role "$1" --condition "title=$2,expression=$3" --format=none
}}
{conditional}
if ! gcloud iam service-accounts describe "$RUNNER" --project "$PDT_PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create {runner} --project "$PDT_PROJECT" \\
    --display-name "pdt job runner" --description "managed-by=pdt"
fi
gcloud iam service-accounts add-iam-policy-binding "$RUNNER" --project "$PDT_PROJECT" \\
  --member "serviceAccount:$ACCOUNT" --role roles/iam.serviceAccountUser --format=none
if ! gcloud iam service-accounts describe "$BUILDER" --project "$PDT_PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create {builder} --project "$PDT_PROJECT" \\
    --display-name "{deploy_google_cloud.BUILDER_DISPLAY_NAME}" --description "managed-by=pdt"
fi
gcloud iam service-accounts add-iam-policy-binding "$BUILDER" --project "$PDT_PROJECT" \\
  --member "serviceAccount:$ACCOUNT" --role roles/iam.serviceAccountUser --format=none
echo "AutoIDM can now act as $ACCOUNT in $PDT_PROJECT."
echo "Project number: $NUMBER. Enter it on the AutoIDM Connect page."
"""


def write(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    files = {AWS_FILE: json.dumps(aws_template(), indent=2) + "\n",
             AZURE_FILE: json.dumps(azure_template(), indent=2) + "\n",
             GOOGLE_FILE: google_script()}
    written = []
    for name, text in files.items():
        path = folder / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m pdt.grant_templates DIR\n")
        return 2
    for path in write(Path(argv[0])):
        sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
