# Terraform analysis

Analysis date: 2026-09-08. Branch: `terraform-analysis`. Base: refreshed `origin/master` at `d4ab654`. Worktree: `/Users/jgfaust/projects/pdt-projects/pdt/.worktrees/terraform-analysis`.

## Objective and scope

Standardize infrastructure definitions, state, and CLI operations on Terraform while users continue to use only the PDT CLI. Preserve the simplicity required for users who are not engineers or infrastructure specialists. Assess all current targets, including Windows Task Scheduler, and determine whether custom Terraform modules or providers can maintain parity.

This work is analysis only. No implementation changes or deployments occurred. The findings below come from repository code and primary documentation. The proposed feasibility tests remain unexecuted.

## Conclusion

Terraform can manage PDT's infrastructure definitions, state, planning, and reconciliation while users continue to run only `pdt` commands. PDT would retain authentication, packaging, pricing, and deployment sequencing.

A custom Terraform module combines existing resources and packages PDT's conventions. A custom provider implements resource operations that existing providers cannot perform. Windows could use a module around an existing shell provider. A dedicated Windows provider becomes necessary only if that approach cannot meet PDT's requirements.

Infrastructure-management parity is achievable. Identical runtime behavior requires additional work.

## Current implementation

The current `.pdt/state` contains only deployed app names. The CLI uses those names to suppress its starter deployment suggestion. It tracks no resource identifiers, dependencies, or drift. Terraform would introduce that inventory. See [config.py](src/pdt/config.py), functions `read_state`, `write_state`, and `mark_deployed`, and [cli.py](src/pdt/cli.py).

Today, each provider discovers resources through cloud APIs or Windows commands, reconciles them, and checks ownership before deletion. Terraform could replace much of that resource-management code. The provider-neutral entry point is [deploy.py](src/pdt/deploy.py).

## Platform assessment

| Target | What Terraform can manage | What PDT must retain or prove |
|---|---|---|
| AWS Lambda | Functions, IAM roles, logs, secrets, and EventBridge schedules. | Python dependency packaging, pricing, and secret delivery. [Lambda resource](https://raw.githubusercontent.com/hashicorp/terraform-provider-aws/main/website/docs/r/lambda_function.html.markdown). |
| AWS Fargate | ECS infrastructure, task definitions, ECR, permissions, and schedules. | Container builds and publishing, including registry creation before publishing. [Task definition resource](https://raw.githubusercontent.com/hashicorp/terraform-provider-aws/main/website/docs/r/ecs_task_definition.html.markdown). |
| Google Cloud Run Jobs | Jobs, Scheduler resources, Artifact Registry, service accounts, permissions, and API enablement. | Cloud Build orchestration, pricing, and runtime secret updates. [Cloud Run resource](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_run_v2_job). |
| Azure Functions | Flex Consumption apps, storage, identities, monitoring, and settings. | Equivalent Python package deployment and remote dependency builds require testing. [Flex Consumption resource](https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/main/website/docs/r/function_app_flex_consumption.html.markdown). |
| Azure Container Apps | Scheduled jobs, environments, registries, identities, and permissions. | Container builds, publishing, pricing, and coordination of shared-resource deletion. [Job resource](https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/main/website/docs/r/container_app_job.html.markdown). |
| Windows Task Scheduler | Task registration and settings through a qualified provider or scripted resource. | Drift detection, elevation, import, and dependence on the local machine and project directory. |

Azure Functions deserves an early test. PDT generates a Python shim and invokes `config-zip --build-remote true`. Terraform exposes `zip_deploy_file`, but its publishing and build behavior needs verification against PDT's actual package. See [deploy_azure_functions.py](src/pdt/deploy_azure_functions.py), functions `build_package` and `deploy`.

## Windows modules and providers

Windows is feasible, with qualifications. PDT currently registers a task running as `SYSTEM`. That task executes `uv run --script run.py` from the existing project directory. PDT requests elevation for registration and removal. It checks a description marker before changing an existing task. See [deploy_windows.py](src/pdt/deploy_windows.py), functions `task_xml`, `_run`, `_task_state`, `deploy`, and `destroy`.

The `terr4m/shell` provider documents PowerShell support, create/read/update/delete scripts, and explicit drift detection. A PDT module could supply those scripts for Task Scheduler. A custom Go provider is therefore optional, pending qualification. [Shell resource documentation](https://raw.githubusercontent.com/terr4m/terraform-provider-shell/main/docs/resources/script.md).

That qualification must prove that reads detect task deletion and settings changes. It must also cover ownership checks, import, elevation, partial failures, and recovery.

A module using only `null_resource` or `terraform_data` with `local-exec` would not provide equivalent resource management. Terraform would track the execution conditions without automatically reading the scheduled task. A proper provider read operation supplies that missing behavior. [Provisioner limitations](https://developer.hashicorp.com/terraform/language/provisioners), [provider read operations](https://developer.hashicorp.com/terraform/plugin/framework/resources/read).

Windows currently supports a subset of cron expressions and requires `timezone: local`. Additional XML triggers could expand that subset. A PDT dispatcher could evaluate arbitrary cron expressions and timezones during periodic wakeups. That introduces runtime code requiring daylight-saving, missed-run, and duplicate-run tests.

A Terraform module could install that dispatcher. Terraform itself would not evaluate schedules continuously. Neither a module nor a provider makes a powered-off PC available.

Windows also runs the live source directory. Editing `run.py` changes subsequent executions without another deployment. Terraform task tracking would preserve that behavior; immutable deployment packages would require a separate change.

Azure currently rejects non-UTC timezones in [deploy_azure.py](src/pdt/deploy_azure.py). Terraform adoption would not remove that restriction either.

## State ownership and shared resources

Recommend separate state for each app, plus separately owned state for shared infrastructure.

A single state containing every app complicates the promise that `pdt deploy one-app` changes only that app. Separate app states preserve that isolation. However, multiple app states must not each own the same registry, cluster, or resource group.

Shared ownership must follow the actual resource scope. Google's runner service account is project-wide, while its registry is regional. AWS also combines account-wide IAM resources with regional resources.

PDT would coordinate shared state and app state under a deployment lock. Deploy creates shared prerequisites before dependent resources. Destroy removes the app, checks remaining consumers, and removes unused shared resources. Terraform's backend locks protect individual state operations; coordination across states remains PDT's responsibility.

This also preserves the existing checks for unmanaged resources. An empty app state does not prove that a shared resource group contains nothing else. Routine `-target` use would not solve this problem; HashiCorp recommends it only for exceptional operations. [Terraform targeting guidance](https://developer.hashicorp.com/terraform/cli/commands/plan).

The current deletion checks live in [deploy_aws_fargate.py](src/pdt/deploy_aws_fargate.py), [deploy_aws_lambda.py](src/pdt/deploy_aws_lambda.py), [deploy_azure.py](src/pdt/deploy_azure.py), and [deploy_google_cloud.py](src/pdt/deploy_google_cloud.py).

## State storage and setup

Recommend automatically configured remote state for cloud targets:

- AWS uses S3 with `use_lockfile = true`.
- Azure uses Blob Storage with its native locking.
- Google Cloud uses GCS with locking.
- Windows uses protected local state identified by both project and machine.

These backends support the required locking mechanisms. Windows would require no cloud account merely to store state. [S3](https://developer.hashicorp.com/terraform/language/backend/s3), [Azure](https://developer.hashicorp.com/terraform/language/backend/azurerm), [GCS](https://developer.hashicorp.com/terraform/language/backend/gcs).

Remote storage introduces a setup dependency: the storage must exist before Terraform initializes that backend. PDT can create it through a separate Terraform configuration using local state, then migrate state automatically.

Final cleanup needs the reverse sequence. PDT must move the necessary state out of the backend before deleting its storage. This must include interrupted-operation recovery and protection against concurrent deployment. Otherwise, Terraform adoption would violate PDT's requirement to remove unused infrastructure. [State migration](https://developer.hashicorp.com/terraform/cli/commands/init).

## Preserving the PDT workflow

The user-facing workflow can remain unchanged:

1. The user runs `pdt deploy <app>`.
2. PDT validates YAML and completes any required login.
3. PDT installs its pinned Terraform version and initializes the required configuration.
4. PDT presents readable actions and its existing monthly cost estimate.
5. The user confirms once.
6. PDT coordinates builds and Terraform operations, then reports completion and log links.

Some first deployments require multiple internal stages because publishing needs a registry first. PDT should present those stages as one operation. Terraform supports noninteractive execution and applying saved plans. [Automation guidance](https://developer.hashicorp.com/terraform/tutorials/automation/automate-terraform).

Package versioned Terraform modules with PDT and generate their inputs from the existing YAML. Python can generate `.tf.json` directly. Users would maintain neither HCL nor backend configuration. Terraform, provider versions, module versions, and deployment artifacts should remain pinned. [Terraform JSON configuration](https://developer.hashicorp.com/terraform/language/syntax/json).

## Secrets and runtime data

Secrets need an explicit design. Terraform's `sensitive` flag hides display; it does not prevent state persistence. Avoiding persistence requires ephemeral values and supported write-only arguments in the exact resource schema. Otherwise, PDT must write secret values outside Terraform while Terraform manages containers and permissions. [Sensitive data handling](https://developer.hashicorp.com/terraform/language/manage-sensitive-data).

If every infrastructure mutation must use Terraform, that exception needs supported write-only resources or a custom provider implementation.

Runtime OAuth caches must also remain outside desired infrastructure state. PDT's [email authentication code](src/pdt/utils/email_auth.py), function `_save_cloud_cache`, can publish refreshed credentials as new secret versions. Terraform must not restore an older cache value. Run history and logs likewise remain runtime data.

## Adopting existing deployments

Existing deployments need an import process. The current deployed-name list cannot supply that inventory. PDT must discover owned resources, import their identifiers, and inspect the resulting plan before changing them. It must retain deployment identities so changing the configured account or runtime does not lose the previous deployment. [Terraform import](https://developer.hashicorp.com/terraform/language/import).

## Proposed feasibility tests

Before a full migration, run four bounded feasibility tests:

- Windows task creation, unchanged redeployment, external modification, deletion, import, elevation, and destroy.
- Two cloud apps sharing resources, including concurrent operations and cleanup after each destroy.
- Automatic backend creation, state migration, interrupted-operation recovery, and final backend removal.
- PDT's actual Azure Functions package, including remote dependency installation and a successful execution.

The recommendation is Terraform for infrastructure ownership and reconciliation, with PDT retaining the user workflow and deployment coordination. This analysis establishes feasibility from code and documentation; the proposed tests remain unexecuted.
