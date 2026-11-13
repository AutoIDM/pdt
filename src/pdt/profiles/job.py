"""The `job` profile: a process that starts on a schedule, does its work, and exits.

This is what pdt deployed before profiles existed. An app with no `app_profile`
key is a job.
"""

from __future__ import annotations

NAME = "job"
DESCRIPTION = "a process that starts on a schedule, does its work, and exits"

# The app keys only this profile uses.
KEYS = {"schedule"}

# What each pdt command means for a job. The docs generator prints this table.
COMMANDS = {
    "run": "run the app once on this computer",
    "run --deployed": "start one run of the deployed job now",
    "runs": "one row per run, with its exit code",
    "logs": "the log of one run; the newest by default",
    "health": "whether the last run succeeded",
    "pause": "stop the schedule from starting runs",
    "deploy": "build the app, create or update the job and its schedule, then offer a run",
    "destroy": "remove the job, its schedule, and its logs; keep its storage",
}

# One answer per concern per provider. `pdt deploy --plan` and docs/profiles.md read these.
ANSWERS = {
    "aws": {
        "compute": "AWS Batch job on Fargate",
        "lifecycle": "EventBridge Scheduler starts a run; the run ends when the process exits; "
                     "no retry",
        "ingress": "none; the process accepts no connections",
        "identity": "an IAM job role per app",
        "secrets": "Secrets Manager",
        "storage": "an S3 bucket folder per app",
        "logs": "CloudWatch Logs, one stream per run",
        "health": "the exit code of the last run",
        "build": "a container image built from the app folder, pushed to ECR",
        "cost": "Fargate seconds per run and the S3 bucket",
    },
    "azure": {
        "compute": "Container Apps Job",
        "lifecycle": "the job's own cron trigger starts a run; replica timeout 30 minutes; "
                     "no retry",
        "ingress": "none; the process accepts no connections",
        "identity": "a managed identity per app",
        "secrets": "Key Vault",
        "storage": "a Blob Storage container per app",
        "logs": "Log Analytics, one execution per run",
        "health": "the exit code of the last run",
        "build": "a container image built from the app folder, pushed to ACR",
        "cost": "Container Apps vCPU seconds per run and the storage account",
    },
    "google-cloud": {
        "compute": "Cloud Run Job",
        "lifecycle": "Cloud Scheduler starts a run; the run ends when the process exits; "
                     "no retry",
        "ingress": "none; the process accepts no connections",
        "identity": "a service account per app",
        "secrets": "Secret Manager",
        "storage": "a Cloud Storage bucket folder per app",
        "logs": "Cloud Logging, one execution per run",
        "health": "the exit code of the last run",
        "build": "a container image built from the app folder, pushed to Artifact Registry",
        "cost": "Cloud Run vCPU seconds per run and the bucket",
    },
    "windows": {
        "compute": "a Windows Task Scheduler task on this PC",
        "lifecycle": "the task's calendar trigger starts a run; a run is stopped after "
                     "30 minutes; a run still going when the next is due skips the new start",
        "ingress": "none; the process accepts no connections",
        "identity": "the SYSTEM account",
        "secrets": "the project's .env file, read at each run",
        "storage": "%ProgramData%\\pdt\\<app>\\storage",
        "logs": "%ProgramData%\\pdt\\<app>\\logs, one file per run",
        "health": "the exit code of the last run, from the last line of its log",
        "build": "none; uv runs run.py from the project folder",
        "cost": "none",
    },
}


def problems(app: dict, provider: str) -> list[str]:
    from pdt import config

    name = app["name"]
    problems = []
    if app["schedule"] is not None:
        try:
            cron = config.cron_expression(app["schedule"])
        except config.ConfigError as e:
            problems.append(f"{name}: {e}")
        else:
            problem = config.windows_schedule_problem(cron) if provider == "windows" else ""
            if problem != "":
                problems.append(f"{name}: schedule: {problem}")
    for key in ("web",):
        if app[key]:
            problems.append(f"{name}: {key}: only an app with app_profile: web uses this "
                            "section; remove it, or set app_profile: web")
    return problems
