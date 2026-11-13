# Types of apps

An app's profile sets which services pdt picks for it on each cloud.

| Profile | What it is | Where it runs |
| --- | --- | --- |
| `job` | A script that runs on a schedule and exits. The default. | AWS, Azure, Google Cloud, Windows |
| `web` | An HTTP server behind a Google sign-in. | Google Cloud |

## Scheduled Jobs

| | AWS | Azure | Google Cloud | Windows |
| --- | --- | --- | --- | --- |
| **Compute** | AWS Batch (Fargate) | Container Apps Jobs | Cloud Run Jobs | Task Scheduler |
| **Schedule** | EventBridge Scheduler | Container Apps Jobs | Cloud Scheduler | Task Scheduler |
| **Secrets** | Secrets Manager | Key Vault | Secret Manager | `.env` file, read at each run |
| **Storage** (job files and state) | S3 | Blob Storage | Cloud Storage | `%ProgramData%\pdt` |
| **Logs** | CloudWatch Logs | Log Analytics | Cloud Logging | Log files |
| **Alerts** | Slack, Teams, email | Slack, Teams, email | Slack, Teams, email | Slack, Teams, email |

## Web Apps

| | Google Cloud |
| --- | --- |
| **Compute** | Cloud Run services |
| **Sign-in** | Identity-Aware Proxy |
| **Secrets** | Secret Manager |
| **Storage** (app files) | Cloud Storage |
| **Logs** | Cloud Logging |
| **Alerts** | Slack, Teams, email |
