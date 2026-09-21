"""Give a merge request labelled ``force-merge`` a pipeline without verify jobs.

The verify jobs in verify/.gitlab-ci.yml block a merge until a person runs
them. The label ``force-merge`` turns them off through a ``when: never``
rule. GitLab reads labels once, when it creates a pipeline, so adding the
label to an MR that already has a pipeline changes nothing on its own. This
script closes that gap. The project webhook triggers a master pipeline on
every merge request event, and the ``force-merge`` job runs this script for
each update event. It looks at the one MR in MR_IID:

- no ``force-merge`` label, or the MR is not open: nothing to do.
- the head pipeline has no job named VERIFY_GATE: the bypass already
  happened, or the MR touches no verify path. Nothing to do.
- otherwise: create a new merge request pipeline. GitLab reads the label
  this time, the verify jobs stay out, and the merge unblocks when the test
  stage passes.

The check on the head pipeline makes a repeat run harmless, so a second label
event, an edit, or a push after the label never creates a pipeline twice.

Environment:
  GITLAB_TOKEN    project access token with the ``api`` scope.
  MR_IID          the merge request number, from the webhook template.
  DRY_RUN         anything but the exact word ``false`` means print the
                  decision and create nothing.
  CI_API_V4_URL, CI_PROJECT_ID   set by GitLab CI.

Stdlib only, so CI runs it with no extra install.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

LABEL = "force-merge"
VERIFY_GATE = "verify"
TIMEOUT = 60
DEFAULT_API = "https://gitlab.com/api/v4"


class HttpFailure(Exception):
    """An HTTP call answered with an error status."""


@dataclass
class Settings:
    api_url: str
    project_id: str
    token: str
    iid: str
    dry_run: bool


def decision(mr: dict, jobs: list[dict]) -> str:
    """Why the MR gets a new pipeline, or "" when it does not need one."""
    if mr.get("state") != "opened":
        return ""
    if LABEL not in (mr.get("labels") or []):
        return ""
    if not mr.get("head_pipeline"):
        return "no pipeline yet"
    if any(job.get("name") == VERIFY_GATE for job in jobs):
        return f"head pipeline still has the {VERIFY_GATE} job"
    return ""


def http(method: str, url: str, headers: dict, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, method=method, data=data,
        headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise HttpFailure(f"{method} {url} returned {error.code}: {detail}") from None
    return json.loads(body) if body.startswith((b"{", b"[")) else body.decode()


def api(settings: Settings, method: str, path: str, payload: dict | None = None):
    return http(method, f"{settings.api_url}{path}",
                {"Authorization": f"Bearer {settings.token}"}, payload)


def settings_from_env(env) -> Settings | None:
    missing = [name for name in ("GITLAB_TOKEN", "CI_PROJECT_ID", "MR_IID") if not env.get(name)]
    if missing:
        print(f"set {', '.join(missing)} before running this script")
        return None
    return Settings(
        api_url=env.get("CI_API_V4_URL", DEFAULT_API).rstrip("/"),
        project_id=env["CI_PROJECT_ID"],
        token=env["GITLAB_TOKEN"],
        iid=env["MR_IID"],
        dry_run=env.get("DRY_RUN", "true").strip().lower() != "false",
    )


def main() -> int:
    settings = settings_from_env(os.environ)
    if settings is None:
        return 1
    base = f"/projects/{settings.project_id}/merge_requests/{settings.iid}"
    mr = api(settings, "GET", base)
    pipeline = mr.get("head_pipeline") or {}
    jobs = []
    if pipeline:
        jobs = api(settings, "GET",
                   f"/projects/{settings.project_id}/pipelines/{pipeline['id']}/jobs?per_page=100")
    why = decision(mr, jobs)
    if not why:
        print(f"!{settings.iid} {mr.get('title', '')}: no new pipeline needed")
        return 0
    if settings.dry_run:
        print(f"dry run: !{settings.iid} would get a new merge request pipeline ({why})")
        return 0
    created = api(settings, "POST", f"{base}/pipelines")
    print(f"!{settings.iid} {mr.get('title', '')}: created pipeline "
          f"{created.get('web_url')} ({why})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HttpFailure as failure:
        print(failure)
        sys.exit(1)
