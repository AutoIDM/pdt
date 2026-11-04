"""Cloud grants: the places of a project, and the env a child gets from grant credentials.

A customer connects a place on the portal's Connect page, from a template that
`python -m pdt.grant_templates` writes. pdt makes no grant itself. The hosted runner gets
credentials for one place in the claim answer, and `credentials` turns them into the env of the
one child process that runs the command. A cloud CLI's own login files go in a memory folder,
which the runner deletes after the command.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pdt import config

PLACE_HELP = "<provider>:<account, subscription, or project>:<region>"
# Only a memory folder keeps a cloud login's own files off the disk.
MEMORY_FOLDER = Path("/dev/shm")
EXTERNAL_ID_TAG = "autoidm-external-id"
BOUNDARY_ENV = "PDT_PERMISSIONS_BOUNDARY"
GRANT_FOLDER_ENV = "PDT_GRANT_FOLDER"
AWS_BOUNDARY = "arn:aws:iam::{account}:policy/pdt-autoidm-boundary"


def split_place(place: str) -> tuple[str, str, str]:
    provider, _, rest = place.partition(":")
    scope, _, region = rest.partition(":")
    return provider, scope, region


def memory_folder(prefix: str) -> Path:
    """A new folder in memory for a cloud CLI's own login files. A runner with no such folder
    stops, because it never writes cloud credentials to disk."""
    import tempfile
    if not MEMORY_FOLDER.is_dir():
        raise RuntimeError(f"{MEMORY_FOLDER} does not exist, so the hosted runner cannot keep this "
                           "cloud's login in memory")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=MEMORY_FOLDER))


def credentials(place: str, answer: dict) -> dict[str, str]:
    """The env of a child from the credentials answer of AutoIDM for `place`. Raises
    RuntimeError when the answer is not for that place's provider, or a login fails."""
    provider, scope, region = split_place(place)
    found = answer.get(provider) if isinstance(answer, dict) else None
    if answer.get("provider") != provider or not isinstance(found, dict):
        raise RuntimeError(f"AutoIDM sent no {provider} credentials for {place}")
    if provider == "aws":
        return {"AWS_ACCESS_KEY_ID": found["access_key_id"],
                "AWS_SECRET_ACCESS_KEY": found["secret_access_key"],
                "AWS_SESSION_TOKEN": found["session_token"],
                "AWS_REGION": found.get("region") or region,
                "AWS_DEFAULT_REGION": found.get("region") or region,
                BOUNDARY_ENV: AWS_BOUNDARY.format(account=scope)}
    if provider == "azure":
        folder = memory_folder("pdt-az-")
        env = {"AZURE_CONFIG_DIR": str(folder), GRANT_FOLDER_ENV: str(folder)}
        proc = subprocess.run(
            ["az", "login", "--service-principal", "--username", found["client_id"],
             "--tenant", found["tenant"], "--federated-token", found["federated_token"],
             "--allow-no-subscriptions", "--output", "none"],
            env={**os.environ, **env}, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Azure refused the login as {found['client_id']}: "
                               f"{proc.stderr.strip()[-500:]}")
        return env
    folder = memory_folder("pdt-gcloud-")
    token_file = folder / "token"
    token_file.write_text(found["access_token"], encoding="utf-8")
    return {"CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": str(token_file),
            "CLOUDSDK_CORE_PROJECT": found.get("project") or scope,
            "CLOUDSDK_CONFIG": str(folder), GRANT_FOLDER_ENV: str(folder)}


def places_of_project() -> list[str]:
    """Each place the project's apps deploy to, or deployed to, with no question and no login."""
    from pdt import managed_services
    from pdt.service_commands import PLACE
    found = set()
    try:
        for name in config.app_folders():
            try:
                platforms = [config.merged_app(name)["platform"], config.deployed_platform(name) or {}]
            except config.ConfigError:
                continue
            for platform in platforms:
                place = managed_services.place(platform)
                if place and PLACE.fullmatch(place):
                    found.add(place)
    except config.ConfigError:
        return []
    return sorted(found)
