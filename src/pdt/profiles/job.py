"""The `job` profile: a process that starts on a schedule, does its work, and exits.

This is what pdt deployed before profiles existed. An app with no `app_profile`
key is a job.
"""

from __future__ import annotations

NAME = "job"

# The app keys only this profile uses.
KEYS = {"schedule", "job"}


def problems(app: dict, provider: str) -> list[str]:
    from pdt import config

    name = app["name"]
    where = f"{name}/{config.APP_FILE}"
    problems = config.key_problems(f"{where}: job", app["job"], config.JOB_KEYS)
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
