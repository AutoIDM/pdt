"""`pdt service run --worker --command ID`: run one command in one Fargate task.

The task's environment names only the Parameter Store name of the worker's registration,
PDT_WORKER_PARAMETER. The worker reads it, removes the name from its environment, and keeps the key
in memory. It checks in for that one command, verifies and claims it, and runs it from its
snapshot with the grant's credentials (pdt.service_runner). It checks in each CHECKIN_SECONDS for
a cancel, and a hard timer ends the process at the run limit plus GRACE_SECONDS. Exit code 0 means
that AutoIDM has the result, or that no command was there to run.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time

from pdt import console, deploy, grants, service, service_commands, service_runner
from pdt.service_commands import Refused
from pdt.service_runner import Job, Pool
from pdt.utils import autoidm

PARAMETER_ENV = "PDT_WORKER_PARAMETER"
CHECKIN_SECONDS = 60
GRACE_SECONDS = 300
TIMEOUT_EXIT = 124


def registration() -> service.Served:
    """The worker's registration, from the parameter that the task's environment names."""
    name = os.environ.pop(PARAMETER_ENV, "").strip()
    if not name:
        raise autoidm.AutoidmError(f"{PARAMETER_ENV} is not set, so this task has no worker "
                                   "registration")
    found = json.loads(grants.script_json(
        deploy.provider_command("aws", grants.WORKER_PARAMETER, name, False), {},
        f"read the worker registration {name}")["value"])
    keys = found.get("command_keys") if isinstance(found.get("command_keys"), list) else []
    version = found.get("command_keys_version")
    return service.Served(
        found["customer_id"],
        autoidm.Registration(found["installation_id"], found["api_url"].rstrip("/"),
                             autoidm.load_key(found["private_key_pem"].encode()), name),
        keys, version if type(version) is int else 1, worker=True)


def stop_at(seconds: float) -> None:
    """End the process when the run limit passes, whatever the child does."""
    def end() -> None:
        console.error(f"The worker passed its time limit of {console.value(round(seconds))} "
                      "seconds.")
        console.drain()
        os._exit(TIMEOUT_EXIT)
    timer = threading.Timer(seconds, end)
    timer.daemon = True
    timer.start()


def take(served: service.Served, command_id: str, pool: Pool) -> Job | None:
    """Check in for the one command, verify it, and claim it. None when there is nothing to run."""
    answer = service.checkin_answer(served, {}, [], [], {}, only=command_id)
    item = next((item for item in answer.get("commands") or []
                 if isinstance(item, dict) and item.get("command_id") == command_id), None)
    if item is None:
        console.say(f"AutoIDM has no command {console.value(command_id)} for this worker to run.")
        return None
    try:
        claims = service_commands.verify(item.get("token"), command_id,
                                         served.registration.installation_id, served.customer,
                                         served.keys, served.clock.now(), worker=True)
    except Refused as e:
        service.refuse(served, command_id, str(e))
        return None
    stop_at(claims["limit_seconds"] + GRACE_SECONDS)
    entry = {"schema": service_runner.JOURNAL_SCHEMA, "command_id": command_id,
             "token": item["token"], "requested_by": claims.get("requested_by"),
             "operation": claims["operation"], "claim_id": secrets.token_hex(16),
             "state": "claiming", "pid": None, "process_created": None, "output_seq": 0,
             "output_offset": 0, "result": None}
    service_runner.journal_write(entry)
    job = Job(command_id, claims, served.registration, None, served.clock, worker=True)
    if not service.claim(job, entry["claim_id"], pool):
        raise autoidm.AutoidmError(f"AutoIDM did not answer the claim of {command_id}")
    return job if pool.ids() else None


def follow(served: service.Served, job: Job, pool: Pool) -> None:
    """Wait for the job, and check in each CHECKIN_SECONDS for a cancel."""
    pool.start_ready()
    last = time.monotonic()
    while job.thread is None or job.thread.is_alive():
        if time.monotonic() - last >= CHECKIN_SECONDS:
            last = time.monotonic()
            try:
                answer = service.checkin_answer(served, {}, [job.command_id], [], {},
                                                only=job.command_id)
            except autoidm.AutoidmError as e:
                console.warn(f"AutoIDM did not answer the check-in: {console.escape(str(e))}")
            else:
                for item in answer.get("cancels") or []:
                    service.apply_cancel(item, served, pool)
        time.sleep(1)


def run(command_id: str) -> int:
    if not service_commands.COMMAND_ID.fullmatch(command_id):
        console.error(f"{console.value(command_id)} is not a command ID.")
        return 2
    try:
        served = registration()
        pool = Pool()
        job = take(served, command_id, pool)
        if job is None:
            return 0
        follow(served, job, pool)
    except (autoidm.AutoidmError, RuntimeError, KeyError, TypeError, ValueError) as e:
        console.error(console.escape(str(e)))
        return 1
    entry = service_runner.journal_read(command_id) or {}
    if not entry.get("reported"):
        console.error(f"AutoIDM does not have the result of {console.value(command_id)}.")
        return 1
    return 0
