# App profiles

An app profile is the shape of what pdt deploys for an app. The `app_profile` key in an app's own `pdt.yml`, or in its `apps:` entry in the project's `pdt.yml`, picks one; an app that sets none is a `job`. The profile decides which config keys the app may set, what each `pdt` command means for it, and which resources each provider creates for it.

Every profile answers the same concerns on every provider:

- **compute**: What runs the app's process
- **lifecycle**: When the process starts, when it stops, and what restarts it
- **ingress**: Who can reach the process over the network, and how
- **identity**: Who the app runs as, and how a caller proves who they are
- **secrets**: Where the app's env values live
- **storage**: Where the app's files live, and which other apps may read them
- **logs**: Where each run's output goes
- **health**: How pdt tells whether the app is working
- **build**: How the app's code reaches the platform
- **cost**: What the platform charges for

## `job`

A process that starts on a schedule, does its work, and exits.

Config keys only this profile uses: `schedule`.

| Command | What it means |
| --- | --- |
| `pdt run` | run the app once on this computer |
| `pdt run --deployed` | start one run of the deployed job now |
| `pdt runs` | one row per run, with its exit code |
| `pdt logs` | the log of one run; the newest by default |
| `pdt health` | whether the last run succeeded |
| `pdt pause` | stop the schedule from starting runs |
| `pdt deploy` | build the app, create or update the job and its schedule, then offer a run |
| `pdt destroy` | remove the job, its schedule, and its logs; keep its storage |

| Concern | AWS | Azure | Google Cloud | Windows |
| --- | --- | --- | --- | --- |
| compute | AWS Batch job on Fargate | Container Apps Job | Cloud Run Job | a Windows Task Scheduler task on this PC |
| lifecycle | EventBridge Scheduler starts a run; the run ends when the process exits; no retry | the job's own cron trigger starts a run; replica timeout 30 minutes; no retry | Cloud Scheduler starts a run; the run ends when the process exits; no retry | the task's calendar trigger starts a run; a run is stopped after 30 minutes; a run still going when the next is due skips the new start |
| ingress | none; the process accepts no connections | none; the process accepts no connections | none; the process accepts no connections | none; the process accepts no connections |
| identity | an IAM job role per app | a managed identity per app | a service account per app | the SYSTEM account |
| secrets | Secrets Manager | Key Vault | Secret Manager | the project's .env file, read at each run |
| storage | an S3 bucket folder per app | a Blob Storage container per app | a Cloud Storage bucket folder per app | %ProgramData%\pdt\<app>\storage |
| logs | CloudWatch Logs, one stream per run | Log Analytics, one execution per run | Cloud Logging, one execution per run | %ProgramData%\pdt\<app>\logs, one file per run |
| health | the exit code of the last run | the exit code of the last run | the exit code of the last run | the exit code of the last run, from the last line of its log |
| build | a container image built from the app folder, pushed to ECR | a container image built from the app folder, pushed to ACR | a container image built from the app folder, pushed to Artifact Registry | none; uv runs run.py from the project folder |
| cost | Fargate seconds per run and the S3 bucket | Container Apps vCPU seconds per run and the storage account | Cloud Run vCPU seconds per run and the bucket | none |

## `web`

An HTTP server that runs all the time behind the platform's sign-in.

Config keys only this profile uses: `web`.

| Command | What it means |
| --- | --- |
| `pdt run` | start the app on this computer with no sign-in, from run.py or its Dockerfile; Ctrl+C stops |
| `pdt run --deployed` | open the deployed app in the browser |
| `pdt runs` | one row: the deployed service, since its last deploy |
| `pdt logs` | what the deployed service wrote since its last deploy |
| `pdt health` | whether the deployed service serves, is stopped, or failed |
| `pdt pause` | stop the server; `unpause` starts it again |
| `pdt deploy` | build the app, create or update the server and its sign-in, wait until health_path answers, print the URL |
| `pdt destroy` | remove the server, its sign-in, its image, and its service account; keep the storage and the logs |

| Concern | AWS | Azure | Google Cloud | Windows |
| --- | --- | --- | --- | --- |
| compute | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | a Cloud Run service pdt-<app>, by default 0 to 3 instances of 1 vCPU and 512 MiB, one container from the app's image | not yet available; the web profile runs on Google Cloud |
| lifecycle | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | scales to zero after 15 minutes with no request, starts on the next one; `pdt deploy` sends a new revision; `pdt pause` stops the service and `pdt unpause` starts it | not yet available; the web profile runs on Google Cloud |
| ingress | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | HTTPS at https://pdt-<app>-<project number>.<region>.run.app; Identity-Aware Proxy in front; no request reaches the app before the sign-in | not yet available; the web profile runs on Google Cloud |
| identity | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | the service runs as its own service account pdt-web-<app>, which may read only its secret and its storage.read folders and write only its own storage folder; a visitor signs in with Google, allowed_domains and allowed_groups are IAM bindings on the service, and the app sees X-Goog-Authenticated-User-Email; a user: or serviceAccount: binding added by hand stays | not yet available; the web profile runs on Google Cloud |
| secrets | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | Secret Manager secret pdt-<app>-env, mounted as PDT_ENV_JSON | not yet available; the web profile runs on Google Cloud |
| storage | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | gs://pdt-data-<suffix>/<app>/; each storage.read app's folder is a read-only Cloud Storage volume at /pdt/storage/<app>/, new files visible within about a minute | not yet available; the web profile runs on Google Cloud |
| logs | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | Cloud Logging; `pdt logs` reads what the deployed service wrote since its last deploy: its output and Cloud Run's system log; --follow tails them; IAP writes no per-sign-in entry on Cloud Run | not yet available; the web profile runs on Google Cloud |
| health | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | a startup probe on web.health_path every 5 s for up to 90 s; `pdt health` shows ok while the service serves, and `pdt runs` shows it as one row | not yet available; the web profile runs on Google Cloud |
| build | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | the app's Dockerfile, or the generated one, pushed to Artifact Registry | not yet available; the web profile runs on Google Cloud |
| cost | not yet available; the web profile runs on Google Cloud | not yet available; the web profile runs on Google Cloud | request-based billing: CPU and memory while a request runs, no charge at zero instances; IAP is free | not yet available; the web profile runs on Google Cloud |

This file is generated by `scripts/render_profiles_doc.py` from `src/pdt/profiles/`. Edit the profile modules, then run the script.
