# Live verification

A pdt project used to prove that deploy and destroy do what they say on a real cloud account. It is not a unit test: every run creates and deletes real resources, and it costs real money.

One run covers one provider and runs the scenarios in `scripts/verify.py` in order. `coverage.yml` lists every pdt behavior and the scenario or app that proves it; `scripts/coverage.py --check` fails when the code has a behavior the file does not name. `AGENTS.md` in this folder holds the rules for keeping both in step.

| Scenario | What it proves |
| --- | --- |
| `deploy` | The account is empty first. Every app deploys. Every resource carries `managed-by=pdt` and belongs to one app or to the shared set. |
| `schedule-fires` | The a app's schedule, set to a few minutes after the run starts, fires once and the run succeeds. The run reads the provider's own run record: CloudWatch log events on AWS, job executions on Azure and Google Cloud, `Get-ScheduledTaskInfo` on Windows. |
| `redeploy` | A second deploy of the a app exits 0 and changes no resource. |
| `storage` | `pdt storage <app> ls`, `get`, `query`, and `destroy` read and remove `verify.csv` from the a app's data store. Storage is refused for the b app, which sets `storage: false`. |
| `destroy` | Destroying one app removes its resources and leaves the other app and shared resources in place. The last destroy removes deploy resources while retaining storage by design. |
| `destroy-absent` | Destroying an app that is no longer deployed exits 0 and creates nothing. |
| `storage-cleanup` | The run deletes its exact, managed physical data store and confirms that the store group and account are gone. |
| `local` | `pdt validate`, `list`, `examples`, `completion --script`, and `run` work in this project, and `pdt init`, `new`, `list`, and `validate` work in a new folder. Runs in `verify:check` with no account. |

The `apps:` list in `pdt.yml` is the matrix and the single source of truth. Every provider gets two apps, so a shared resource always has a second owner while the first one is destroyed. The a app uses the `daily` shorthand, storage, and an optional environment value. The b app carries a cron expression, `storage: false`, an alternate `env.one_of` branch, its own `Dockerfile` on the container providers, and `timezone: America/New_York` where the provider allows a zone (Azure evaluates cron in UTC only; Windows uses `local`).

CI sets `PDT_RESOURCE_NAMESPACE` to `CI_PIPELINE_ID`. Every resource name and inventory query uses that namespace, so pipelines can run concurrently. A retried job reconciles and cleans the original pipeline's resources.

| App | Provider | Runs on |
| --- | --- | --- |
| `aws-fargate-a`, `aws-fargate-b` | aws | Fargate |
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
export PDT_RESOURCE_NAMESPACE=local-run
export PDT_VERIFY_OPTIONAL=optional
export PDT_VERIFY_ALT=alternate
uv run --no-project --with pyyaml python verify/scripts/verify.py aws
```

Add `--report verify-aws.xml` to write a JUnit XML report. The runner prints one `PASS` or `FAIL` line per step and stops at the first failure. If the initial check fails, the run exits without changing resources. After that check passes, a failure attempts to destroy every app and exits 1. The run writes the a app's fire-time schedule into its `config.yml` and restores the file when the run ends. `verify.py local` runs the `local` scenario alone and needs no account.

The `windows` provider deploys to the computer you run it on, so run it only on a Windows machine you are willing to add scheduled tasks to.

The Azure job builds container images with Docker on the GitLab runner and uploads them to Azure Container Registry. It does not use ACR Tasks.

## CI/CD variables

Set these in the project's CI/CD settings.

| Variable | Job | Value | Setting |
| --- | --- | --- | --- |
| `AWS_ROLE_ARN` | verify:aws | ARN of an IAM role that trusts this project's GitLab OIDC token | Not protected |
| `AZURE_CLIENT_ID` | verify:azure | Service principal client ID | Masked; not protected |
| `AZURE_CLIENT_SECRET` | verify:azure | Service principal secret | Masked; not protected |
| `AZURE_TENANT_ID` | verify:azure | Azure tenant ID | Masked; not protected |
| `GOOGLE_APPLICATION_CREDENTIALS` | verify:google-cloud | Service account key | Masked; not protected; file type |
| `GOOGLE_CLOUD_PROJECT` | verify:google-cloud | Google Cloud project ID | Not protected |

Every cloud job is required. A missing credential makes its job fail, so a pipeline cannot pass without testing every provider.

The AWS job stores no key. It sends the job's OIDC token to `sts assume-role-with-web-identity` and receives credentials that expire after one hour. The role's trust policy must allow `sts:AssumeRoleWithWebIdentity` from the `gitlab.com` identity provider when `gitlab.com:sub` matches `project_path:autoidm/pdt:ref_type:branch:ref:*`, and its permission policy needs the actions pdt prints in `deployer_policy` plus the inventory and cleanup actions: `tag:GetResources`, `lambda:ListFunctions`, `iam:ListRoles`, `secretsmanager:ListSecrets`, `scheduler:ListScheduleGroups`, `scheduler:ListTagsForResource`, `ecs:ListClusters`, `ecr:ListTagsForResource`, `s3:GetBucketTagging`, `s3:DeleteBucket`, and `sts:GetCallerIdentity`.

The Azure service principal holds `Contributor` on the subscription, and `Role Based Access Control Administrator` with a condition that limits the roles it may assign to the ones pdt assigns: Key Vault Secrets Officer (`b86a8fe4-44ce-4948-aee5-eccb2c155cd7`), Key Vault Secrets User (`4633458b-17de-408a-b874-0445c86b69e6`), AcrPull (`7f951dda-4ed3-4680-a7ca-43fe172d538d`), and Storage Blob Data Contributor (`ba92f5b4-2d11-453d-a403-e96b0029c9fe`). When pdt starts assigning a new role, add its id to that condition, or the deploy fails at `role assignment create` with `AuthorizationFailed`.

`PDT_SMOKE_TOKEN` is set in `verify/.gitlab-ci.yml`, so it needs no CI/CD variable. Each app declares it as required, so a deployed job fails unless pdt delivered it through `PDT_ENV_JSON`.

`PDT_INSTALL` is optional. Each job installs the wheel the `build` job produced. Set `PDT_INSTALL` to a git ref or to `pdt-cli` to verify a different build instead.

## What the listings read

The raw listing includes retained data stores for tag and ownership checks. App-destroy checks omit them because `pdt destroy` retains storage by design. The final verification cleanup deletes the run-specific test store and requires an empty raw listing.

| Provider | Listing |
| --- | --- |
| aws | `resourcegroupstaggingapi get-resources`, plus one list per kind filtered on the `pdt` name prefix |
| azure | `az resource list` for the run's resource group, plus the resource group itself. The Container Apps environment `pdt-shared/pdt-eastus2` is named in `PDT_AZURE_CONTAINER_APPS_ENVIRONMENT`, so the run treats it as the user's own and never creates, lists, or deletes it. Create it once by hand before the first run |
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
