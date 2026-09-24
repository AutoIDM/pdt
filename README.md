# pdt - Process Deploy Tool

**Scheduled jobs for IT teams, without a cloud engineer.** Integrations, reports, and alerts run in your own AWS, Azure, or Google Cloud account, or on a Windows PC, and you deploy each one with a single command.

<!-- TODO: replace VIDEO_URL and the thumbnail with the announcement video. Use absolute URLs; PyPI shows this file too. -->
[![Watch the 3-minute demo](https://gitlab.com/autoidm/pdt/-/raw/master/docs/images/demo-thumbnail.png)](VIDEO_URL)

[Website](https://autoidm.com/#services?utm_source=readme&utm_content=top) · [Book a 30-minute walkthrough](BOOKING_URL?utm_source=readme&utm_content=top) · [Docs](https://gitlab.com/autoidm/pdt/-/blob/master/docs/guide.md)

## Why pdt

Most IT teams have a folder of scripts that someone runs by hand, or a server under a desk. pdt turns each script into a scheduled job in the cloud account you already have.

- **One command to deploy.** `pdt deploy my-report` builds the job, schedules it, stores its secrets, and signs you in if you need to be. It installs every tool it needs by itself.
- **Cost is purely cloud costs, a few dollars a year.** Before it changes anything, deploy lists what it will create and what it will cost each month, using your cloud provider's price list. Most jobs cost cents a month.
- **It runs in your account.** Your data and credentials stay in your own cloud account. Nothing passes through ours.
- **The same steps on every cloud.** AWS, Azure, and Google Cloud ask the same questions. Windows Task Scheduler asks none.
- **Best practices from AutoIDM.** We've been doing data itegrations for years, this gets you to production and answers your questions of how to deploy, schedule, secure, and monitor your jobs
- **It cleans up after itself.** `pdt destroy my-report` removes everything deploy created.

## Let your AI agent write the job

`pdt init` writes an `AGENTS.md` into your project that tells Claude Code, Cursor, Codex, or any other coding agent how the project works. Describe the job in plain words:

> Every Monday at 8am, email me a list of Entra ID users who haven't signed in for 90 days.

The agent starts from one of the bundled examples, writes the job, and checks it with `pdt validate` and `pdt run`. You look over the result and run `pdt deploy`.

## Quick start

pdt needs [uv](https://docs.astral.sh/uv/). If you don't have it, install it first:

```
# macOS and Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

On Windows you can skip that step: `winget install AutoIDM.pdt` installs `uv` if you don't have it and puts the same `pdt` command on your PATH.

Then install pdt, create a project, and run the starter job:

```
uv tool install pdt-cli
pdt init my-jobs
cd my-jobs
pdt run hello-world
```

`pdt init` asks two things: which cloud to use and which region. It creates a working `hello-world` app so you have something to run straight away.

Put it on a schedule in your cloud:

```
pdt deploy hello-world
```

Before it changes anything, deploy shows you the plan and the monthly cost, then asks you to agree. Here is a real deploy to Azure of a Meltano job that syncs Salesforce accounts and contacts into NetSuite every hour:

```
$ pdt deploy salesforce-netsuite-customer-sync
Fetching list prices from the Azure Retail Prices API...
Plan:
  register required Azure resource providers
  use existing resource group pdt-shared (shared by every pdt project in this subscription)
  use existing Log Analytics workspace pdt-logs in pdt-shared
  use existing Container Apps environment pdt-shared/pdt-eastus2 (shared by every pdt project in this subscription)
  use existing resource group pdt
  use existing ACR pdt232959dc77 (Basic)
  keep ACR authentication-as-arm on pdt232959dc77 (required for managed-identity image pulls)
  use existing managed identity pdt-runner
  use existing Key Vault pdt-232959dc77 (RBAC)
  ensure scoped Key Vault secret permissions for the deployer and managed identity
  use existing storage account pdtdatacfa3bd4e8d, container pdt-data-cfa3bd4e8d (kept after destroy)
  grant pdt-salesforce-netsuite-a60138e write access to salesforce-netsuite-customer-sync/ in storage account pdtdatacfa3bd4e8d, container pdt-data-cfa3bd4e8d
  grant the signed-in Azure account write access to pdt-data-cfa3bd4e8d (for pdt storage)
  build and push image pdt232959dc77.azurecr.io/salesforce-netsuite-customer-sync:latest (from salesforce-netsuite-customer-sync/Dockerfile)
  update Key Vault secret pdt-salesforce-netsuite-customer-sync-env (11 env vars)
  allow pdt-salesforce-netsuite-a60138e to update its own Key Vault secret pdt-salesforce-netsuite-customer-sync-env
  update Container Apps Job pdt-salesforce-netsuite-a60138e: "0 * * * *" (UTC)
Estimated monthly cost (eastus2 list prices, before free grants):
  Container Apps job: ~730 runs x 2.1 min avg of recent runs x 0.5 vCPU / 1 GiB  $   1.38
  Container Registry (Basic, shared)                                             $   5.07
  Key Vault: 1 secret, ~730 reads                                                $   0.00
  storage: 1154 objects (0.01 GB)                                                $   0.00
  total                                                                          $   6.45
  excludes ACR image builds/storage and Log Analytics ingestion
Proceed? [y/N]
```

Running the job itself every hour costs $1.38 a month. Most of the total is the container registry, which all of your jobs share, so each job you add costs only its own run time.

That's it: your first job is live. Remove it with `pdt destroy hello-world` when you're finished.

> **Have 20 more scripts to move?** We help IT teams move their scheduled scripts onto pdt, and we can build the first few with you. [Book a call](BOOKING_URL?utm_source=readme&utm_content=after-deploy).

## What you can build

pdt comes with examples. List them with `pdt examples` and copy one with `pdt new my-report --from <example>`.

| Example | What it does |
| --- | --- |
| `impossible-travel-report` | Checks recent Entra ID sign-ins and emails you when one person signs in from two places too far apart for the time between them: Dallas an hour ago, Paris now. |
| `monday-orphaned-account-report` | Lists active Monday users whose email address matches no active Entra ID user. |
| `hello-world` | The smallest possible app. `pdt init` puts one in every new project as a place to start. |

Jobs can send email through Microsoft 365, Gmail, Microsoft Graph, Amazon SES, or Resend, and keep files from one run to the next in storage pdt creates for them.

**Don't see the job you need?** Tell us what you want automated and we'll help you build it. [Book a call](BOOKING_URL?utm_source=readme&utm_content=examples).

## Everyday commands

| Command | What it does |
| --- | --- |
| `pdt new APP --from EXAMPLE` | add an app to the project |
| `pdt validate` | check the config files and the required env vars |
| `pdt run APP` | run an app on this machine |
| `pdt deploy APP` | deploy an app to its configured platform |
| `pdt deploy --all` | deploy every enabled app, in order |
| `pdt health` | show whether each app's last run succeeded |
| `pdt logs APP` | read the log of the newest run |
| `pdt destroy APP` | remove everything deploy created |

The full list, including `runs`, `secrets`, and `storage`, is in [Commands](https://gitlab.com/autoidm/pdt/-/blob/master/docs/commands.md).

## Documentation

- [Guide](https://gitlab.com/autoidm/pdt/-/blob/master/docs/guide.md): project layout, keeping files between runs, running pdt from CI, and writing your own app.
- [Choosing where jobs run](https://gitlab.com/autoidm/pdt/-/blob/master/docs/providers.md): settings for AWS, Azure, Google Cloud, and Windows, and building from your own Dockerfile.
- [Commands](https://gitlab.com/autoidm/pdt/-/blob/master/docs/commands.md): every command and option.
- [Utilities for your apps](https://gitlab.com/autoidm/pdt/-/blob/master/docs/utilities.md): sending email and logging.

## Who builds pdt

pdt is built by [AutoIDM](https://autoidm.com/?utm_source=readme&utm_content=footer). We help IT teams automate identity and operations work. pdt is free and open source under the MIT license.

- Found a bug or want a feature? [Open an issue](https://gitlab.com/autoidm/pdt/-/issues).
- Want help getting started, or want us to build jobs for you? [Book a 30-minute call](BOOKING_URL?utm_source=readme&utm_content=footer).
