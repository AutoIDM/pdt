# How pdt works

## Primitives

Every job gets the same building blocks. pdt picks the service on each cloud for you.

| | AWS | Azure | Google Cloud | Windows |
| --- | --- | --- | --- | --- |
| **Compute** | AWS Batch (Fargate) | Container Apps Jobs | Cloud Run Jobs | Task Scheduler |
| **Schedule** | EventBridge Scheduler | Container Apps Jobs | Cloud Scheduler | Task Scheduler |
| **Secrets** | Secrets Manager | Key Vault | Secret Manager | `.env` file, read at each run |
| **Storage** (job files and state) | S3 | Blob Storage | Cloud Storage | `%ProgramData%\pdt` |
| **Logs** | CloudWatch Logs | Log Analytics | Cloud Logging | Log files |
| **Alerts** | Slack, Teams, email | Slack, Teams, email | Slack, Teams, email | Slack, Teams, email |

## Practices

1. **One folder per job** - Each job is built, deployed, and destroyed on its own.
2. **Run it locally first** - `pdt run` runs the job the same way the cloud does, with the same config and storage.
3. **You own everything** - Your jobs live in your own git repo, and they run in your own cloud account. There is no pdt server.
4. **Secrets stay secret** - Kept in `.env` on your computer and in the cloud's secret store, never in git or the image.
5. **Nothing left behind** - Everything pdt makes is tagged `managed-by=pdt`, and `pdt destroy` removes all of it.
6. **Cheapest by default** - Jobs run only when scheduled, on the lowest-cost option that works (for example arm64 on AWS).
7. **One run at a time** - A job's saved state is locked during a run, so two runs never overwrite each other.
