# How pdt works

## Practices

1. **One folder per app** - Each app is built, deployed, and destroyed on its own.
2. **Run it locally first** - `pdt run` runs the app the same way the cloud does, with the same config and storage.
3. **You own everything** - Your apps live in your own git repo, and they run in your own cloud account. There is no pdt server.
4. **Secrets stay secret** - Kept in `.env` on your computer and in the cloud's secret store, never in git or the image.
5. **Nothing left behind** - Everything pdt makes is tagged `managed-by=pdt`, and `pdt destroy` removes all of it.
6. **Cheapest by default** - Jobs run only when scheduled, on the lowest-cost option that works (for example arm64 on AWS). Web apps scale to zero instances when no one uses them.
7. **One run at a time** - A job's saved state is locked during a run, so two runs never overwrite each other.
8. **Signed in by default** - A web app accepts no visitor until the visitor signs in. Only `web.auth: public` opens it to everyone.
9. **Least access** - Each web app runs as its own identity. It can read its own secret and the storage it names, and nothing else.


## Definitions

A **project** is a directory holding `pdt.yml`. It has one or more **apps** in subdirectories. An **app** is what pdt runs or deploys.

An **app** is a **scheduled job** (**job** for short) or a **web app**. A **job** has a `run.py` or `run.ps1` that defines what it does. A **web app** has a `run.py` that runs an HTTP server, or a `Dockerfile` that builds one.

pdt chooses how to deploy your app based on what kind of app it is, its settings, and where you're deploying it. The kind of app is its **profile**. pdt reads `app_profile` from the app's `config.yml` to see what kind it is. It is `job` or `web`. `job` is the default. See [App profiles](profiles.md).


## Primitives

Every app gets the same building blocks. pdt picks the service on each cloud for you.

### Job apps

| | AWS | Azure | Google Cloud | Windows |
| --- | --- | --- | --- | --- |
| **Compute** | AWS Batch (Fargate) | Container Apps Jobs | Cloud Run Jobs | Task Scheduler |
| **Schedule** | EventBridge Scheduler | Container Apps Jobs | Cloud Scheduler | Task Scheduler |
| **Secrets** | Secrets Manager | Key Vault | Secret Manager | `.env` file, read at each run |
| **Storage** (job files and state) | S3 | Blob Storage | Cloud Storage | `%ProgramData%\pdt` |
| **Logs** | CloudWatch Logs | Log Analytics | Cloud Logging | Log files |
| **Alerts** | Slack, Teams, email | Slack, Teams, email | Slack, Teams, email | Slack, Teams, email |

### Web apps

| | What pdt uses |
| --- | --- |
| **Compute** | A Cloud Run service `pdt-<app>`: 0 to 3 instances of 1 vCPU and 512 MiB. Billed only while a request runs. |
| **Address** | `https://pdt-<app>-<project number>.<region>.run.app`, with a Google certificate. pdt knows the address before the first deploy. |
| **Sign-in** | Identity-Aware Proxy. A visitor signs in with Google. `web.auth.allowed_domains` and `web.auth.allowed_groups` say who may enter. |
| **Identity** | A service account `pdt-web-<app>` for this app only. |
| **Secrets** | Secret Manager secret `pdt-<app>-env`, given to the app as `PDT_ENV_JSON`. |
| **Storage** (its own files) | The app's folder in the project bucket, as for a job. |
| **Storage** (another app's files) | Each app in `storage.read`, as read-only files at `/pdt/storage/<app>/`. A new file shows within about 60 seconds. |
| **Logs** | Cloud Logging: the app's output and Cloud Run's system log, from the last deploy. |
| **Health** | A startup check on `web.health_path` every 5 seconds, for at most 90 seconds. `pdt health` shows `ok` while the service serves. |
| **Alerts** | The deploy and destroy events. |

### Web app pieces that are missing

| Piece | What happens now | What it needs |
| --- | --- | --- |
| **Size and scale** | Every web app gets 1 vCPU, 512 MiB, 80 requests per instance, at most 3 instances, and a 300-second request limit. | Settings in `config.yml` for each value. |
| **Your own domain** | Visitors use the `run.app` address. | A domain setting, and a load balancer or a domain mapping. |
| **Down alert** | Nothing checks the app after the deploy. Nothing tells you when it stops answering. | An uptime check on `web.health_path`, and an `app:down` event for the alerts. |
| **Measured cost** | The deploy estimate assumes an instance runs 80% of the month. | An estimate from expected visits, and the real cost from Cloud Monitoring after the deploy. |
| **Sign-out** | The app must link to `?gcp-iap-mode=CLEAR_LOGIN_COOKIE` itself. Nobody tested that link on Cloud Run. | A test, then a documented link or a `pdt.utils.auth` helper. |
| **Non-root container** | The generated image runs the app as root. | A `USER` in the generated Dockerfile. |
| **Image clean-up** | Every old image stays in Artifact Registry. | A clean-up policy that keeps the newest images. |
| **Two projects in one cloud project** | Two pdt projects with an app of the same name deploy to the same service and folder. | Names that include the pdt project. |
