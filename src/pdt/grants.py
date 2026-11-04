"""Cloud grants: let AutoIDM act in one place, with the customer's own login.

Every provider script takes `grant|grant-remove|grant-env <place> [--yes] [--plan]`. A grant
has three steps: `/v1/grants/start` names AutoIDM's principal and the external ID, the provider
script creates the identity, and `/v1/grants/finish` names it for the portal's check. `--plan`
prints the resources and creates nothing, with no sign-in and no request to AutoIDM.

A provider script calls `run_verb` with a table of its functions, so each one prints a plan and
asks the same way. `grant-env` is for a cloud worker: it turns a token's `grant` claim into env
values on stdout, which the worker passes only to one child process. The Windows provider has
no grant, because it acts on the computer it runs on.

GRANTS_FILE records each place this computer granted, for `pdt grant list`. It holds no secret.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pdt import config, console

GRANT_ENV = "PDT_GRANT_JSON"
COMMANDS = ("grant", "grant-remove", "grant-env")
GRANTS_FILE = "grants.json"
PLACE_HELP = "<provider>:<account, subscription, or project>:<region>"
# Only a memory folder keeps a cloud login's own files off the disk.
MEMORY_FOLDER = Path("/dev/shm")
# The AWS provider script's verb that reads a SecureString of AutoIDM's own account for a worker.
WORKER_PARAMETER = "worker-parameter"
REFUSED_KEY = "refused"
EXTERNAL_ID_TAG = "autoidm-external-id"


class Mismatch(Exception):
    """The identity in the place does not carry the grant's external ID."""


@dataclass(frozen=True)
class ProviderGrant:
    """What one provider script does. `plan` needs no sign-in and no network."""
    plan: Callable[[str, dict], list]
    remove_plan: Callable[[str, dict], list]
    create: Callable[[str, dict, bool], str]
    remove: Callable[[str, dict, bool], None]
    env: Callable[[str, dict], dict[str, str]]


def split_place(place: str) -> tuple[str, str, str]:
    provider, _, rest = place.partition(":")
    scope, _, region = rest.partition(":")
    return provider, scope, region


def grant_values() -> dict:
    """What the parent passes to a provider script: the principal, the external ID, the role."""
    try:
        found = json.loads(os.environ.get(GRANT_ENV) or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def run_verb(command: str, place: str, assume_yes: bool, plan_only: bool,
             provider: ProviderGrant) -> int:
    """One provider script verb. It prints the resources, asks `Proceed? [y/N]`, and acts."""
    from pdt.deploy import DECLINED, confirm, show_plan
    values = grant_values()
    if command == "grant-env":
        try:
            found = provider.env(place, values)
        except Mismatch as e:
            found = {REFUSED_KEY: str(e)}
        console.data(json.dumps(found))
        return 0
    lines = (provider.plan if command == "grant" else provider.remove_plan)(place, values)
    if plan_only:
        return show_plan(lines)
    if not confirm(lines, assume_yes):
        return DECLINED
    if command == "grant":
        console.data(json.dumps({"role": provider.create(place, values, assume_yes)}))
    else:
        provider.remove(place, values, assume_yes)
    return 0


def no_grant(command: str, place: str) -> int:
    """The verb of a provider with nothing to grant."""
    if command == "grant-env":
        console.error(f"The windows provider has no grant, so {console.value(place)} has no "
                      "credentials.")
        return 1
    console.note("the windows provider acts on this computer, so it has no grant.")
    return 0


def provider_run(command: str, place: str, assume_yes: bool, values: dict,
                 stdout: int | None = None, plan_only: bool = False) -> subprocess.CompletedProcess:
    from pdt import __version__, deploy
    provider = split_place(place)[0]
    if provider not in deploy.PROVIDERS:
        raise config.ConfigError(f"{place} is not a place. A place has the form {PLACE_HELP}.")
    args = deploy.provider_command(provider, command, place, assume_yes,
                                   ["--plan"] if plan_only else [])
    env = dict(os.environ, PDT_CLI_VERSION=__version__, PDT_JSON_OUTPUT="1")
    env[GRANT_ENV] = json.dumps(values)
    return deploy.run_provider(args, env=env, stdout=stdout, text=True)


def script_json(args: list[str], values: dict, what: str) -> dict:
    """The JSON a provider script prints. `values` travel in the environment of that one process,
    and the answer travels through a pipe."""
    env = dict(os.environ, PDT_JSON_OUTPUT="1", UV_NO_CONFIG="1")
    env[GRANT_ENV] = json.dumps(values)
    proc = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pdt could not {what}: {proc.stderr.strip()[-500:]}")
    try:
        found = json.loads(proc.stdout)
    except ValueError:
        found = None
    if not (isinstance(found, dict) and all(isinstance(value, str) for value in found.values())):
        raise RuntimeError(f"pdt could not {what}: the provider script printed no values")
    return found


def credentials(grant: dict, registration: object) -> dict[str, str]:
    """The env values of a token's `grant` claim, for a cloud worker. /v1/grants/values names the
    role, the external ID, AutoIDM's principal, and the Parameter Store name of AutoIDM's own
    Azure or Google Cloud credential. A place whose identity does not carry the external ID
    raises Refused, and the child does not start."""
    from pdt import deploy
    from pdt.service_commands import Refused
    from pdt.utils import autoidm
    place = grant["place"]
    answer = autoidm.post(registration, "/v1/grants/values", {"version": 1, "place": place})
    if answer.get("role") != grant["role"]:
        raise Refused(f"The command names the role {grant['role']} in {place}, and AutoIDM's "
                      f"grant names {answer.get('role')}.")
    values = {"role": answer["role"], "external_id": answer.get("external_id"),
              "principal": answer.get("principal")}
    parameter = answer.get("credential_parameter")
    if parameter:
        values["credential"] = script_json(
            deploy.provider_command("aws", WORKER_PARAMETER, parameter, False), {},
            f"read the AutoIDM credential {parameter}")["value"]
    found = script_json(deploy.provider_command(split_place(place)[0], "grant-env", place, False),
                        values, f"act as {grant['role']} in {place}")
    if REFUSED_KEY in found:
        raise Refused(found[REFUSED_KEY])
    return found


def records_path() -> Path:
    from pdt.utils import autoidm
    return autoidm.folder() / GRANTS_FILE


def read_records() -> dict:
    try:
        found = json.loads(records_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def write_records(records: dict) -> None:
    path = records_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config.write_text_atomically(path, json.dumps(records, indent=2) + "\n")


def registration_for_place() -> object | None:
    """The registration a grant uses: the customer of this project, else the only one here."""
    from pdt import managed_services
    from pdt.utils import autoidm
    try:
        found = managed_services.settings()
    except config.ConfigError:
        found = None
    if found is not None:
        return autoidm.machine_registration(found["customer"])
    customers = sorted(path.stem for path in autoidm.folder().glob("cus_*.json"))
    return autoidm.machine_registration(customers[0]) if len(customers) == 1 else None


def add(place: str, assume_yes: bool, plan_only: bool, registration: object | None = None) -> int:
    """Make the identity in `place`, and tell AutoIDM its name."""
    from pdt.service_commands import PLACE
    from pdt.utils import autoidm
    if not PLACE.fullmatch(place):
        console.error(f"{console.value(place)} is not a place. A place has the form "
                      f"{console.value(PLACE_HELP)}, such as "
                      f"{console.value('aws:123456789012:us-east-2')}.")
        return 1
    if plan_only:
        return provider_run("grant", place, assume_yes, {}, plan_only=True).returncode
    found = registration or registration_for_place()
    if found is None:
        console.error(f"This computer has no AutoIDM registration to grant from. Run "
                      f"{console.value('pdt register')} first.")
        return 1
    answer = autoidm.post(found, "/v1/grants/start", {"version": 1, "place": place})
    values = {"external_id": answer.get("external_id"), "principal": answer.get("principal")}
    proc = provider_run("grant", place, assume_yes, values, subprocess.PIPE)
    if proc.returncode != 0:
        return proc.returncode
    role = json.loads(proc.stdout)["role"]
    autoidm.post(found, "/v1/grants/finish", {"version": 1, "place": place, "role": role})
    records = read_records()
    records[place] = {"role": role, "installation_id": found.installation_id}
    try:
        write_records(records)
    except OSError as e:
        console.warn(f"{console.value('pdt grant list')} will not show this place: "
                     f"{console.escape(str(e))}")
    console.done(f"AutoIDM can now ask to act in {console.value(place)} as {console.value(role)}. "
                 "The portal checks it before it uses it.")
    return 0


def remove(place: str, assume_yes: bool, plan_only: bool) -> int:
    records = read_records()
    values = {"role": (records.get(place) or {}).get("role")}
    code = provider_run("grant-remove", place, assume_yes, values, plan_only=plan_only).returncode
    if code != 0 or plan_only:
        return code
    if place in records:
        del records[place]
        try:
            write_records(records)
        except OSError as e:
            console.warn(f"{console.value('pdt grant list')} still shows this place: "
                         f"{console.escape(str(e))}")
    console.done(f"Removed the AutoIDM identity from {console.value(place)}.")
    return 0


def show(as_json: bool) -> int:
    records = read_records()
    if as_json:
        console.data(json.dumps([{"place": place, **record}
                                 for place, record in sorted(records.items())]))
        return 0
    if not records:
        console.say("This computer has made no grant.")
        console.next_steps([("pdt grant add PLACE", "let AutoIDM act in a place")])
        return 0
    console.table(["place", "identity"],
                  [[place, record.get("role", "-")] for place, record in sorted(records.items())],
                  ["bold"])
    return 0


def memory_folder(prefix: str) -> Path:
    """A new folder in memory for a cloud CLI's own login files. A worker with no such folder
    stops, because it never writes cloud credentials to disk."""
    import tempfile
    if not MEMORY_FOLDER.is_dir():
        raise RuntimeError(f"{MEMORY_FOLDER} does not exist, so a cloud worker cannot keep this "
                           "cloud's login in memory")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=MEMORY_FOLDER))


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
