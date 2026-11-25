# Concepts

This page explains how pdt organizes your code, picks cloud services for each kind of app, and keeps your secrets and files safe.

## Projects, apps, and profiles

A **project** is a directory holding a top-level `pdt.yml` file. It holds shared platform defaults (`platform:`), project-wide alerts (`alerts:`), and one or more **apps** in subdirectories.

An **app** is a single folder inside the project that pdt validates, builds, runs, and deploys on its own. Each app has its own `pdt.yml` plus either a `run.py`, one or more `.ps1` scripts (for PowerShell), or a `Dockerfile` (for a web app).

An app's **profile** (`app_profile` in the app's `pdt.yml`) tells pdt what shape of workload it is and which services to provision on the target platform:

- **`job` (default):** A **scheduled job** that runs on a schedule (or on demand with `pdt run APP --deployed`) and exits when finished. Jobs run on AWS, Azure, Google Cloud, or Windows Task Scheduler.
- **`web`:** A **web app** that runs an HTTP server behind a platform sign-in gate. Web apps run on Google Cloud Run behind Identity-Aware Proxy (IAP).

As new profiles are added, every profile uses the same project layout, config merging, secret handling, and CLI lifecycle (`pdt validate`, `pdt run`, `pdt deploy`, `pdt logs`, `pdt health`, `pdt pause`, `pdt destroy`). See [Types of apps](types-of-apps.md) for the exact service mapping per profile and cloud.

## No pdt server — you own everything

There is no pdt server, control plane, or vendor-hosted database. Your code stays in your own git repository, your `.env` secrets stay on your computer and in your own cloud secret store, and every deployed resource lives in your own cloud account (or on your own Windows PC) tagged `managed-by=pdt`. `pdt destroy` removes everything pdt created for an app, leaving only the app's stored data until you run `pdt storage APP destroy`.

## From local run to deployed app

1. **Configure:** Project-wide defaults live in the root `pdt.yml`; app-specific overrides live in `<app>/pdt.yml`. Secrets live in `.env` (never committed).
2. **Validate:** `pdt validate` checks YAML keys, scans Python (`os.environ`) and PowerShell (`$env:`) code for required env vars, and verifies email and alert config without touching any cloud.
3. **Run locally:** `pdt run APP` runs the app on your machine using the same config, `.env` values, and local storage folder (`.pdt/storage/<app>/`). For a job, it runs once and prints logs; for a web app, it serves on `http://127.0.0.1:<port>/` and opens your browser.
4. **Plan and deploy:** `pdt deploy APP --plan` previews every cloud resource action and a monthly cost estimate without changing anything. `pdt deploy APP` builds the container image (locally with Docker if running, otherwise with the cloud's native builder), uploads secrets, and reconciles the cloud resources—or registers a Windows Scheduled Task when `platform.provider: windows`.
5. **Operate:** Inspect status and history with `pdt health`, `pdt runs`, `pdt logs`, `pdt storage`, `pdt secrets`, and `pdt gui`, or receive Slack/Teams notifications via `pdt alerts setup`.

## How storage, state, and secrets stay separated

- **Secrets** live in `.env` locally (or CI environment variables) and are synced to the cloud secret store on `pdt deploy` or `pdt secrets APP save`. They are never baked into container images or committed to git.
- **Storage** is one persistent folder per app (S3, Azure Blob Storage, Google Cloud Storage, `%ProgramData%\pdt\<app>\storage` on Windows, or `.pdt/storage/<app>/` locally). Destroying and redeploying an app preserves its storage folder.
- **State locking** protects scheduled jobs that carry state between runs (`storage.sync()` in Python or `$env:PDT_STATE_DIR` in PowerShell) so two runs of the same job never overwrite each other's state.
- **Cross-app reads** (`storage: read: [other-app]`) let one app read another app's storage folder read-only without exposing write access.

## Next steps

- See which services each profile creates: [Types of apps](types-of-apps.md)
- Create a project and add your first app: [Projects and apps](projects.md)
- Write a Python, PowerShell, or web app: [Writing your own app](writing-apps.md)
