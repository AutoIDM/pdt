# Live verification

A pdt project used to prove that deploy and destroy do what they say on a real cloud account. It is not a unit test: every run creates and deletes real resources, and it costs real money.

One run covers one provider. It asserts the account is empty, deploys every app the provider owns, lists the account through the provider's own API, and checks two things: every resource pdt made carries `managed-by=pdt`, and every resource belongs either to one app or to the set the apps share. It then destroys the apps one at a time. After each destroy it checks that the destroyed app's resources are gone and that every other app's resources, and the shared ones, are still there. The last destroy must leave the account empty again.

The `apps:` list in `pdt.yml` is the matrix and the single source of truth. Every provider and runtime gets two apps, so a shared resource always has a second owner while the first one is destroyed.

| App | Provider | Runtime |
| --- | --- | --- |
| `aws-lambda-a`, `aws-lambda-b` | aws | lambda |
| `aws-fargate-a`, `aws-fargate-b` | aws | fargate |
| `azure-functions-a`, `azure-functions-b` | azure | functions |
| `azure-container-apps-a`, `azure-container-apps-b` | azure | container_apps |
| `google-cloud-a`, `google-cloud-b` | google-cloud | Cloud Run job |
| `windows-a`, `windows-b` | windows | Task Scheduler |

The app directories are generated. Edit `scripts/templates/`, then run `uv run --with pyyaml python verify/scripts/sync_apps.py` from the repository root.

## Running one provider locally

`pdt` must be on the PATH, because both the deploys and the listings go through it.

```sh
uv tool install .
export PDT_SMOKE_TOKEN=pdt-verify
uv run --no-project --with pyyaml python verify/scripts/verify.py aws
```

Add `--report verify-aws.xml` to write a JUnit XML report. The runner prints one `PASS` or `FAIL` line per step and stops at the first failure. Any failure or exception destroys every app again before the run exits 1, so a failed run leaves nothing behind.

The `windows` provider deploys to the computer you run it on, so run it only on a Windows machine you are willing to add scheduled tasks to.

## CI/CD variables

Set these in the project's CI/CD settings.

| Variable | Job | Value | Setting |
| --- | --- | --- | --- |
| `AWS_ACCESS_KEY_ID` | verify:aws | AWS access key ID | Masked; protected |
| `AWS_SECRET_ACCESS_KEY` | verify:aws | AWS secret access key | Masked; protected |
| `AWS_DEFAULT_REGION` | verify:aws | `us-east-1` | Protected |
| `AZURE_CLIENT_ID` | verify:azure | Service principal client ID | Masked; protected |
| `AZURE_CLIENT_SECRET` | verify:azure | Service principal secret | Masked; protected |
| `AZURE_TENANT_ID` | verify:azure | Azure tenant ID | Masked; protected |
| `GOOGLE_APPLICATION_CREDENTIALS` | verify:google-cloud | Service account key | Masked; protected; file type |
| `GOOGLE_CLOUD_PROJECT` | verify:google-cloud | Google Cloud project ID | Protected |

A cloud job whose variables are absent becomes a manual job that is allowed to fail. The pipeline stays green and shows the job as not run, so a project without an account for that provider still merges. Add the variables and the job runs on every merge request.

`PDT_SMOKE_TOKEN` is set in `verify/.gitlab-ci.yml`, so it needs no CI/CD variable. Each app declares it as required, so a deployed job fails unless pdt delivered it through `PDT_ENV_JSON`.

`PDT_INSTALL` is optional. Each job installs the wheel the `build` job produced. Set `PDT_INSTALL` to a git ref or to `pdt-cli` to verify a different build instead.

## What the listings read

| Provider | Listing |
| --- | --- |
| aws | `resourcegroupstaggingapi get-resources`, plus one list per kind filtered on the `pdt` name prefix |
| azure | `az resource list --resource-group pdt-verify`, plus the resource group itself |
| google-cloud | `gcloud asset search-all-resources`, plus `scheduler jobs list` and `iam service-accounts list` |
| windows | `Get-ScheduledTask` filtered on `pdt-` task names |

### Google Cloud needs the Cloud Asset API

`gcloud asset search-all-resources` needs `cloudasset.googleapis.com`. Enable it once in the test project, and give the CI service account `roles/cloudasset.viewer`:

```sh
pdt gcloud services enable cloudasset.googleapis.com --project <project>
```

pdt itself never needs this API. It exists only so verification can ask the project what it holds instead of asking pdt.

### The AWS tagging API omits untagged resources

`resourcegroupstaggingapi get-resources` returns a resource only when it carries a tag, so a leftover that lost its tags would be invisible to it. That is the failure the "every resource is tagged" step exists to catch, so the AWS listing also asks each service directly for everything named `pdt` or `pdt-*`, and uses the tagging API only to add kinds those lists do not cover.

### Resources that carry no tag

Four kinds cannot hold a tag, so pdt marks each one another way and the listing reads that marker as `managed-by=pdt`:

- An EventBridge Scheduler schedule. pdt tags the `pdt` schedule group instead; membership in it is the marker.
- A Cloud Scheduler job. pdt writes `Managed by PDT app <name>` in its description.
- A Google Cloud service account. pdt sets its display name to `pdt job runner`.
- A Windows scheduled task. pdt starts its description with `Managed by pdt;`.

An Azure Key Vault secret is not a resource either. `az resource list` returns the vault, not what is inside it, so the vault stands for the secrets pdt put in it.
