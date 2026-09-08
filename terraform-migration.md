# Terraform migration handoff

Implementation date: 2026-09-08. Worktree: `.worktrees/terraform-analysis`. Branch: `terraform-analysis`. Refreshed base: `origin/master` at `f69d594`. This document records the implementation and local verification for the draft merge request.

The migration implements all six existing deployment runtimes. The original assessment remains in [terraform-analysis.md](terraform-analysis.md). Live cloud deployment and native Windows execution remain unverified.

## Implementation

Users continue to run `pdt deploy` and `pdt destroy`. PDT handles Terraform installation, definitions, backends, imports, plans, and applies. Users maintain the existing YAML files. Cost estimates, authentication, packaging, and application secret delivery remain in PDT.

| Target | Terraform definitions | Resource implementation |
|---|---|---|
| AWS Lambda | `src/pdt/terraform_aws.py` | Native Lambda, IAM, logs, secret containers, and EventBridge resources |
| AWS Fargate | `src/pdt/terraform_aws.py` | Native ECS, ECR, IAM, logs, secret containers, and EventBridge resources |
| Azure Functions | `src/pdt/terraform_azure.py` | Native Flex Consumption, service plans, storage, monitoring, identities, and permissions |
| Azure Container Apps | `src/pdt/terraform_azure.py` | Native jobs, environments, registries, identities, monitoring, and permissions |
| Google Cloud Run Jobs | `src/pdt/terraform_google.py` | Native jobs, Scheduler, Artifact Registry, API enablement, accounts, secrets, and permissions |
| Windows Task Scheduler | `src/pdt/terraform_windows.py` | Shell-provider CRUD and drift detection through PowerShell |

The runner is `src/pdt/terraform.py`. Remote storage and operation locking are in `src/pdt/terraform_store.py`. Terraform is pinned to 1.16.1. Providers are AWS 6.63.0, AzureRM 5.4.0, Google 8.1.0, and `terr4m/shell` 0.9.1.

Cloud deployments use separate app states and shared-resource states. AWS stores them in S3, Google in GCS, and Azure in Blob Storage. Terraform creates the storage itself. A conditional operation lock coordinates the states. Terraform also uses each backend's native state locking.

Before deleting unused state storage, PDT marks its cloud metadata as deleting. New deployments reject that marker, including after acquiring the operation lock. Bootstrap snapshots remain available until native storage destruction succeeds. `--unlock` requires confirmation that the previous operation stopped; lock deletion uses the observed version.

Windows uses local state under `.pdt/terraform` and an operating-system file lock. A process exit releases that lock. Owned legacy tasks use read-only adoption because the shell provider does not support Terraform import. Scripts check ownership again inside the elevated process. They compare modeled XML fields while ignoring extra scheduler defaults.

PDT saves the deployment's provider, runtime, location, schedule, and timezone before applying app changes. Destroy uses that saved context. Shared-resource location checks prevent an app from relocating resources used by other apps.

Application secret payloads stay outside Terraform state. Native providers can persist generated infrastructure credentials, including Azure storage keys. State storage therefore remains private and versioned. Azure backend authentication retrieves account keys through the authenticated management API; keys reach Terraform through the process environment.

## Verification

- `uv run pytest -q`: 214 passed, 17 opt-in checks skipped.
- The complete local suite with opt-in checks enabled passed 229 tests. Subsequent base refreshes added an AWS permission and two scaffold tests; the offline suite passed again.
- Real Terraform exercised local create, unchanged apply, update, and destroy with its built-in `terraform_data` resource.
- Terraform validated both AWS runtimes, both Azure runtimes, six Google configurations, all three backend configurations, and the Windows shell configuration against the pinned providers.
- Fourteen PowerShell checks passed using PowerShell 7.5.2 on macOS. They cover syntax, XML drift, equivalent durations, ownership refusal, and elevated command payloads with mocked scheduler commands.
- The repository packaging smoke check passes on Python 3.12. The wheel and source distribution build. An isolated `uv tool install` ran `init`, `validate`, and `run hello-world`. All four installed provider scripts loaded through `uv run --script ... --help`, including automatic SDK installation. The clone shim passed its help and project-validation checks.
- `git diff --check` passes. No cloud resources or native scheduled tasks were created during verification.

To repeat the local integration checks in this worktree:

```sh
PDT_RUN_TERRAFORM_SMOKE=1 \
PDT_RUN_TERRAFORM_PROVIDER_VALIDATE=1 \
PDT_POWERSHELL="$PWD/.secrets/debug/terraform-migration/powershell/pwsh" \
uv run pytest -q
```

Provider-validation fixtures require the cached providers under `.secrets/debug/terraform-migration/schemas`. Generated configurations, the isolated installation, the PowerShell binary, a pre-refresh backup, and `decisions.tsv` remain in that ignored directory. The normal test suite requires no installed platform CLI or cloud account.

## Platform qualification still required

Run first deployment, unchanged redeployment, configuration update, interrupted-operation recovery, legacy adoption, and final destruction on each actual target before release. Test multiple apps sharing resources during those runs. Cloud API permissions, eventual consistency, remote package builds, and native scheduling cannot be proved by local schemas and mocks.

Windows still uses the local timezone, supports the existing cron subset, and runs the live project directory as SYSTEM. Native Task Scheduler, UAC, and SYSTEM access to the project's `uv` executable require Windows verification. Windows ARM uses amd64 Terraform through operating-system emulation because the pinned cloud providers do not publish Windows ARM binaries.

No custom Go provider was necessary for this implementation. The Windows shell provider supplies real read and drift operations; a module containing only command provisioners would not provide those operations. The implementation does not remove the underlying scheduling differences between Windows, Azure, AWS, and Google.
