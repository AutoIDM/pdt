# Live verification

A pdt project used to prove that deploy and destroy do what they say on a real cloud account. It is not a unit test: every run creates and deletes real resources, and it costs real money.

One run covers one provider. The account may also hold other pdt apps, so the run checks only what its own apps make. It starts with `pdt destroy <app> --yes` for each of its apps, so a cancelled or failed earlier run never blocks the next one. The step lists every leftover it found and passes. Destroy removes a shared resource only when no app uses it, so a shared leftover of these apps goes too, and one that another app uses stays. The run then asserts that no resource of its apps exists, records everything else the account holds, deploys every app the provider owns, lists the account through the provider's own API, and checks two things: every resource pdt made carries `managed-by=pdt`, and every resource belongs either to one app or to the set the apps share. It then destroys the apps one at a time. After each destroy it checks that the destroyed app's resources are gone and that every other app's resources, and the shared ones, are still there. After the last destroy, the account must hold exactly what it held before the run: nothing the run made remains, and nothing that was there before is gone. A resource that existed before the run is not checked for a tag.

The `apps:` list in `pdt.yml` is the matrix and the single source of truth. Every provider gets two apps, so a shared resource always has a second owner while the first one is destroyed.

| App | Provider | Runs on |
| --- | --- | --- |
| `aws-fargate-a`, `aws-fargate-b` | aws | Batch job on Fargate |
| `azure-container-apps-a`, `azure-container-apps-b` | azure | Container Apps job |
| `google-cloud-a`, `google-cloud-b` | google-cloud | Cloud Run job |
| `windows-a`, `windows-b` | windows | Task Scheduler |

The app directories are generated. Edit `scripts/templates/`, then run `uv run --with pyyaml python verify/scripts/sync_apps.py` from the repository root.

## Running one provider locally

`pdt` must be on the PATH, because both the deploys and the listings go through it.

```sh
uv tool install .
export PDT_SMOKE_TOKEN=pdt-verify
export PDT_AZURE_CONTAINER_APPS_ENVIRONMENT=pdt-shared/pdt-eastus2  # azure only
uv run --no-project --with pyyaml python verify/scripts/verify.py aws
```

Add `--report verify-aws.xml` to write a JUnit XML report. The runner prints one `PASS` or `FAIL` line per step and stops at the first failure. If the first destroy fails, or a resource of its apps survives it, the run exits without deploying anything. A survivor is one destroy cannot reach, which is a bug in destroy or a resource pdt did not tag. After that check passes, a failure attempts to destroy every app and exits 1.

The `windows` provider deploys to the computer you run it on, so run it only on a Windows machine you are willing to add scheduled tasks to.

The Azure job builds container images with Docker on the GitLab runner and uploads them to Azure Container Registry. It does not use ACR Tasks.

## CI/CD variables

Set these in the project's CI/CD settings.

| Variable | Job | Value | Setting |
| --- | --- | --- | --- |
| `AWS_ROLE_ARN` | verify:aws | ARN of an IAM role that trusts this project's GitLab OIDC token | Not protected |
| `AZURE_CLIENT_ID` | verify:azure | Service principal client ID | Not protected |
| `AZURE_CLIENT_SECRET` | verify:azure | Service principal secret | Masked; not protected |
| `AZURE_TENANT_ID` | verify:azure | Azure tenant ID | Not protected |
| `GOOGLE_APPLICATION_CREDENTIALS` | verify:google-cloud | Service account key | File type; not protected |
| `GOOGLE_CLOUD_PROJECT` | verify:google-cloud | Google Cloud project ID | Not protected |

No variable is protected, because a merge request pipeline runs on an unprotected branch, and GitLab hides a protected variable from it. A cloud job whose variables are absent becomes a manual job that is allowed to fail. The pipeline stays green and shows the job as not run, so a project without an account for that provider still merges. Add the variables and the job runs on every merge request.

The AWS job stores no key. It sends the job's OIDC token to `sts assume-role-with-web-identity` and receives credentials that expire after one hour. The role's trust policy must allow `sts:AssumeRoleWithWebIdentity` from the `gitlab.com` identity provider when `gitlab.com:sub` matches `project_path:autoidm/pdt:ref_type:branch:ref:*`, and its permission policy needs the actions pdt prints in `deployer_policy` plus the read actions the inventory uses: `tag:GetResources`, `lambda:ListFunctions`, `lambda:ListTags`, `iam:ListRoles`, `secretsmanager:ListSecrets`, `scheduler:ListScheduleGroups`, `scheduler:ListTagsForResource`, `ecs:ListClusters`, `ecr:ListTagsForResource`, `sts:GetCallerIdentity`.

The Azure service principal holds `Contributor` and `Locks Contributor` on the subscription, because deploy puts a management lock on the shared environment and the ACR. It also holds `Role Based Access Control Administrator` with a condition that limits the roles it may assign to the ones pdt assigns: Key Vault Secrets Officer (`b86a8fe4-44ce-4948-aee5-eccb2c155cd7`), Key Vault Secrets User (`4633458b-17de-408a-b874-0445c86b69e6`), AcrPull (`7f951dda-4ed3-4680-a7ca-43fe172d538d`), and Storage Blob Data Contributor (`ba92f5b4-2d11-453d-a403-e96b0029c9fe`). When pdt starts assigning a new role, add its id to that condition, or the deploy fails at `role assignment create` with `AuthorizationFailed`.

`PDT_SMOKE_TOKEN` is set in `verify/.gitlab-ci.yml`, so it needs no CI/CD variable. Each app declares it as required, so a deployed job fails unless pdt delivered it through `PDT_ENV_JSON`.

`PDT_INSTALL` is optional. Each job installs the wheel the `build` job produced. Set `PDT_INSTALL` to a git ref or to `pdt-cli` to verify a different build instead.

## What the listings read

Every listing drops resources tagged `pdt-lifecycle: retain`. The data store (an S3 bucket, an Azure storage account, or a Cloud Storage bucket) outlives its apps by design, so the checks do not expect it to go.

| Provider | Listing |
| --- | --- |
| aws | `resourcegroupstaggingapi get-resources`, plus one list per kind filtered on the `pdt` name prefix |
| azure | `az resource list --resource-group pdt-verify`, plus the resource group itself. The Container Apps environment `pdt-shared/pdt-eastus2` is named in `PDT_AZURE_CONTAINER_APPS_ENVIRONMENT`, so the run treats it as the user's own and never creates, lists, or deletes it. Create it once by hand before the first run |
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
