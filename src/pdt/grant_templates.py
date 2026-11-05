"""`python -m pdt.grant_templates DIR`: write the three grant templates from pdt's own policy code.

- `aws-grant.yaml`: a CloudFormation template (JSON is YAML) with the role `pdt-autoidm`, which
  the broker role of AutoIDM may assume with the customer's external ID for at most one hour, the
  policies of `deploy_aws.grant_policy_parts` for every region, and the boundary
  `pdt-autoidm-boundary` that each role it makes must carry.
- `azure-grant.json`: an ARM template at subscription scope with the resource groups `pdt` and
  `pdt-shared`, the external ID tag on `pdt`, and the role assignments of the grant on both groups
  for the service principal `principalObjectId`.
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


def azure_assignments(group: str) -> dict:
    roles = [(role, None) for role in deploy_azure.GRANT_ROLES]
    roles.append(("Role Based Access Control Administrator", deploy_azure.grant_condition()))
    resources = []
    for role, condition in roles:
        definition = ("[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
                      f"'{deploy_azure.ROLE_IDS[role]}')]")
        properties = {"roleDefinitionId": definition,
                      "principalId": "[parameters('principalObjectId')]",
                      "principalType": "ServicePrincipal"}
        if condition:
            properties.update({"condition": condition, "conditionVersion": "2.0"})
        resources.append({
            "type": "Microsoft.Authorization/roleAssignments", "apiVersion": "2022-04-01",
            "name": f"[guid(resourceGroup().id, parameters('principalObjectId'), "
                    f"'{deploy_azure.ROLE_IDS[role]}')]",
            "properties": properties})
    return {
        "type": "Microsoft.Resources/deployments", "apiVersion": "2022-09-01",
        "name": f"pdt-autoidm-{group}", "resourceGroup": group,
        "dependsOn": [f"[subscriptionResourceId('Microsoft.Resources/resourceGroups', '{group}')]"],
        "properties": {
            "mode": "Incremental", "expressionEvaluationOptions": {"scope": "inner"},
            "parameters": {"principalObjectId": {"value": "[parameters('principalObjectId')]"}},
            "template": {
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/"
                           "deploymentTemplate.json#",
                "contentVersion": "1.0.0.0",
                "parameters": {"principalObjectId": {"type": "string"}},
                "resources": resources}}}


def azure_template() -> dict:
    groups = []
    for group in ("pdt", deploy_azure.SHARED_GROUP):
        tags = {"managed-by": "pdt"}
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
            "location": {"type": "string", "defaultValue": "[deployment().location]"}},
        "resources": [*groups, *(azure_assignments(group)
                                 for group in ("pdt", deploy_azure.SHARED_GROUP))]}


def shell_quoted(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    return escaped.replace("$", "\\$").replace("PROJECT_NUMBER", "$NUMBER")


def google_script() -> str:
    roles = " ".join(deploy_google_cloud.GRANT_ROLES)
    conditional = "\n".join(
        f'grant {role} "{title}" "{shell_quoted(expression)}"'
        for role, title, expression in deploy_google_cloud.GRANT_CONDITIONAL_ROLES)
    name = deploy_google_cloud.GRANT_NAME
    runner = deploy_google_cloud.GRANT_RUNNER
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
