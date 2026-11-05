"""`pdt service`: run the portal's commands on this computer, with no open port.

The service serves one registration: `<data folder>/service/settings.json` names the customer. A
job runs as SYSTEM or root and has the identity of the service, so one computer serves one
customer. `run` is the only loop, and `python -m pdt.service_main` starts it. A WebSocket
connection on its own thread brings a notice (pdt.service_delivery), and the loop reads a
presigned doorbell URL and checks in on the timers of `Schedule`. A failure of one pass is
logged, and the next pass runs. For each command in a check-in, the service verifies the token
against the pinned keys, writes the journal, claims it, and starts it (pdt.service_runner).

The service reads the settings at each pass. A change starts a check-in at once. When `pdt service
remove` deletes the settings, the service stops.

The service serves each project the settings name, and each folder directly in the projects folder
that holds pdt.yml. The projects folder has a default, `<data folder>/service/projects`, and a new
project from `sync_down` goes there. A command for a project of another customer is refused.

`run` needs only a registration, so the loop also runs in a terminal before an install. With no
administrator rights it runs as that person, in that person's data folder.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pdt import (__version__, config, console, managed_services, register_cli, service_access,
                 service_backends, service_commands, service_delivery, service_install,
                 service_runner)
from pdt.service_commands import Clock, Refused
from pdt.service_delivery import Connection, Schedule
from pdt.service_install import InstallError
from pdt.service_runner import Job, Pool
from pdt.utils import autoidm
from pdt.utils.email_auth import can_prompt

LOG_LIMIT_BYTES = 1024 * 1024
LOOP_SECONDS = 1.0
RECOVER_SECONDS = 30
CHECKIN_PAGE = 20
MAX_LISTED_IDS = 100
DOORBELL_TIMEOUT_SECONDS = 10
CLOCK_WARNING_SECONDS = 60
CLOCK_WARNING_EVERY_SECONDS = 3600
FIRST_CHECKIN_SECONDS = 60
KEY_SET_EVENT = 1003
ACCESS_STATEMENT = ("A portal user of your organization and AutoIDM staff can run any program on "
                    "this computer.")
_said_once: set[str] = set()


def settings_path() -> Path:
    return service_install.settings_path()


def state_path() -> Path:
    return service_runner.service_folder() / "state.json"


def pid_path() -> Path:
    return service_runner.service_folder() / "service.pid"


def log_path() -> Path:
    return service_runner.service_folder() / "service.log"


def service_account() -> str:
    return "SYSTEM" if os.name == "nt" else "root"


def running_pid() -> int | None:
    try:
        pid = json.loads(pid_path().read_text(encoding="utf-8"))["pid"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pid if type(pid) is int and service_runner.process_runs(pid) else None


read_settings = service_install.read_settings
write_settings = service_install.write_settings


def read_state() -> dict:
    try:
        found = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def write_state(state: dict) -> None:
    config.write_text_atomically(state_path(), json.dumps(state, indent=2) + "\n")


def say(message: str) -> None:
    console.say(console.escape(message))
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_LIMIT_BYTES:
            os.replace(path, path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as log:
            log.write(f"{service_runner.now_text()} {message}\n")
    except OSError:
        pass


def say_once(message: str) -> None:
    if message not in _said_once:
        _said_once.add(message)
        say(message)


@contextmanager
def one_service():
    folder = service_runner.service_folder()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "service.lock", "w") as handle:
        fd = handle.fileno()
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def project_customer(root: Path) -> str | None:
    try:
        found = config.load_yaml(root / config.PROJECT_FILE).get(managed_services.KEY)
    except config.ConfigError:
        return None
    return found.get("customer") if isinstance(found, dict) else None


def projects_folder(settings: dict) -> Path:
    return Path(settings.get("projects") or service_runner.service_folder() / "projects")


def project_folders(settings: dict) -> list[Path]:
    try:
        folders = sorted(projects_folder(settings).iterdir())
    except OSError:
        folders = []
    folders += [Path(named) for named in settings.get("named_projects") or []]
    return [folder for folder in folders if (folder / config.PROJECT_FILE).is_file()]


def projects_of(settings: dict) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for folder in project_folders(settings):
        name = managed_services.backend_name(folder.name)
        if name == service_commands.RESERVED_PROJECT:
            say_once(f"The portal does not show the project folder {folder}: the name "
                     f"{service_commands.RESERVED_PROJECT} is reserved. Rename the folder to use "
                     "it from the portal.")
        elif name in found:
            say_once(f"Two projects have the name {name}: {found[name]} and {folder}. The service "
                     f"serves {found[name]}.")
        else:
            found[name] = folder
    return found


def choose_projects(wanted: str | None, current: dict) -> tuple[str, list[str]]:
    folder = current.get("projects") or str(service_runner.service_folder() / "projects")
    named = list(current.get("named_projects") or [])
    if wanted is None:
        try:
            wanted = str(config.find_project())
        except config.ConfigError:
            pass
    for path in (folder, wanted):
        if path is None:
            continue
        resolved = Path(path).expanduser().resolve()
        if (resolved / config.PROJECT_FILE).is_file():
            named.append(str(resolved))
            if path == folder:
                folder = str(service_runner.service_folder() / "projects")
        else:
            folder = str(resolved)
    return folder, list(dict.fromkeys(named))


def project_record(name: str, root: Path) -> dict:
    try:
        folders = sorted(root.iterdir())
    except OSError:
        folders = []
    apps = []
    for folder in folders:
        if not (folder.is_dir() and config.is_app(folder)):
            continue
        if service_commands.NAME.fullmatch(folder.name):
            apps.append(folder.name)
        else:
            say_once(f"The portal does not show the app folder {folder}: a name holds only "
                     "letters, digits, dots, dashes, and underscores, and does not start with "
                     "a dash. Rename the folder to use it from the portal.")
    return {"name": name, "apps": apps}


def checkin_projects(settings: dict, customer: str) -> list[dict]:
    records = []
    for name, root in projects_of(settings).items():
        if project_customer(root) != customer:
            continue
        if service_commands.NAME.fullmatch(name):
            records.append(project_record(name, root))
        else:
            say_once(f"The portal does not show the project folder {root}: its name starts with "
                     "a dash. Rename the folder to use it from the portal.")
    return records


def boot_time() -> str | None:
    try:
        if os.name == "nt":
            import ctypes
            ticks = ctypes.windll.kernel32.GetTickCount64
            ticks.restype = ctypes.c_uint64
            booted = time.time() - ticks() / 1000
        else:
            booted = next(float(line.split()[1]) for line in
                          Path("/proc/stat").read_text(encoding="utf-8").splitlines()
                          if line.startswith("btime "))
    except (OSError, StopIteration, ValueError, AttributeError):
        return None
    return when(booted)


def os_name() -> str:
    import platform
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
                product = str(winreg.QueryValueEx(key, "ProductName")[0])
        except OSError:
            product = None
        if product is not None:
            version = platform.version()
            # Windows 11 kept the ProductName of Windows 10; build 22000 is the first Windows 11.
            if product.startswith("Windows 10 ") and int(version.split(".")[-1]) >= 22000:
                product = "Windows 11 " + product.removeprefix("Windows 10 ")
            return f"{product} {version}"[:200]
    return platform.platform()[:200]


def machine_id() -> str | None:
    """The ID that stays the same for this computer when pdt registers it again."""
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                found = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        elif sys.platform == "darwin":
            text = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                  capture_output=True, text=True, timeout=10).stdout
            found = next(line.split('"')[-2] for line in text.splitlines()
                         if '"IOPlatformUUID"' in line)
        else:
            found = Path("/etc/machine-id").read_text(encoding="utf-8")
    except (OSError, StopIteration, IndexError, subprocess.SubprocessError):
        return None
    return found.strip()[:200] or None


def computer_facts() -> dict[str, str | None]:
    return {"boot_time": boot_time(), "service_started": when(time.time()), "os": os_name(),
            "machine_id": machine_id()}


@dataclass
class Served:
    customer: str
    registration: autoidm.Registration
    keys: list[dict]
    keys_version: int = 1
    clock: Clock = field(default_factory=Clock)
    schedule: Schedule = field(default_factory=Schedule)
    connection: Connection | None = None
    keys_file: Path | None = None
    clock_warned_at: float = 0.0
    checked_projects: set[Path] = field(default_factory=set)
    facts: dict[str, str | None] = field(default_factory=computer_facts)


def read_saved(customer: str) -> dict:
    info, _key = autoidm.files(customer)
    found = json.loads(info.read_text(encoding="utf-8"))
    return found if isinstance(found, dict) else {}


def machine_served(settings: dict) -> Served | None:
    customer = settings.get("customer")
    try:
        found = autoidm.machine_registration(customer) if customer else None
        saved = read_saved(customer) if found else {}
    except (autoidm.AutoidmError, OSError, ValueError) as e:
        console.warn_once(f"pdt cannot serve the registration of {console.value(customer)}: "
                          f"{console.escape(str(e))}")
        return None
    if found is None:
        console.warn_once(f"The service settings name {console.value(customer)}, which has "
                          "no registration on this computer.")
        return None
    keys = saved.get("command_keys")
    keys = keys if isinstance(keys, list) else []
    if not keys:
        console.warn_once(f"The registration of {console.value(customer)} has no command "
                          "keys, so the service refuses each command for it.")
    version = saved.get("command_keys_version")
    return Served(customer, found, keys, version if type(version) is int else 1,
                  connection=Connection(found, say), keys_file=autoidm.files(customer)[0])


def refuse(served: Served, command_id: str, message: str, keep_journal: bool = False) -> None:
    result = service_runner.result_of("refused", message=message)
    if not keep_journal:
        service_runner.journal_write({
            "schema": service_runner.JOURNAL_SCHEMA, "command_id": command_id, "state": "finished",
            "finished_at": service_runner.now_text(), "result": result, "reported": False})
    service_runner.send_result(served.registration, command_id, result)
    say(f"Refused {command_id}: {message}")


def locate(claims: dict, served: Served, settings: dict) -> Path | None:
    project = claims["args"]["project"]
    root = projects_of(settings).get(project)
    if root is None:
        if claims["operation"] == "sync_down":
            return None
        raise Refused(f"This computer does not serve a project named {project}.")
    owner = project_customer(root)
    if owner is None and claims["operation"] != "sync_down":
        raise Refused(f"The project {project} has no {managed_services.KEY} in "
                      f"{config.PROJECT_FILE}. To add it, run pdt register in {root}.")
    if owner not in (None, served.customer):
        raise Refused(f"The project {project} names the customer {owner} in "
                      f"{config.PROJECT_FILE}, not {served.customer}.")
    if root not in served.checked_projects and service_access.is_admin():
        try:
            found = service_access.writable_by_others(root)
        except service_access.AccessError as e:
            raise Refused(str(e)) from None
        if found:
            account, path = found
            raise Refused(f"The account {account} can change {path}, so the pdt service does "
                          f"not run commands for {root} as {service_account()}. An "
                          "administrator must remove that right.")
        served.checked_projects.add(root)
    return root


def accept(item: object, served: Served, settings: dict, pool: Pool) -> bool:
    command_id = item.get("command_id") if isinstance(item, dict) else None
    token = item.get("token") if isinstance(item, dict) else None
    if not (isinstance(command_id, str) and service_commands.COMMAND_ID.fullmatch(command_id)):
        say(f"AutoIDM sent a command with the ID {command_id!r}, which pdt ignores.")
        return True
    try:
        entry = service_runner.journal_read(command_id)
    except (OSError, ValueError) as e:
        refuse(served, command_id, f"The journal file of this command does not parse: {e}.", True)
        return True
    if entry is not None and entry.get("state") != "claiming":
        say(f"Refused a repeat of {command_id}: this computer already holds that command ID.")
        return True
    try:
        claims = service_commands.verify(token, command_id, served.registration.installation_id,
                                         served.customer, served.keys, served.clock.now())
        root = locate(claims, served, settings)
    except Refused as e:
        refuse(served, command_id, str(e))
        return True
    if entry is None:
        entry = {"schema": service_runner.JOURNAL_SCHEMA, "command_id": command_id, "token": token,
                 "requested_by": claims.get("requested_by"), "operation": claims["operation"],
                 "claim_id": secrets.token_hex(16), "state": "claiming", "pid": None,
                 "process_created": None, "output_seq": 0, "output_offset": 0, "result": None}
        service_runner.journal_write(entry)
    job = Job(command_id, claims, served.registration, root, served.clock,
              known_values=service_runner.known_values_of(root, claims["args"].get("app")))
    return claim(job, entry["claim_id"], pool)


def claim(job: Job, claim_id: str, pool: Pool) -> bool:
    command_id = job.command_id
    try:
        service_runner.report(job.registration, command_id, fact="claimed", claim_id=claim_id)
    except autoidm.AutoidmError as e:
        if e.code == "cancelled":
            service_runner.finish(job, service_runner.result_of(
                "cancelled", message="A person cancelled the command before it started."))
            return True
        if e.code in ("already_claimed", "unknown_command", "already_finished"):
            say(f"pdt did not run {command_id}: {e}")
            service_runner.journal_update(command_id, state="finished", reported=True,
                                          finished_at=service_runner.now_text(),
                                          result=service_runner.result_of("refused", message=str(e)))
            return True
        say(f"AutoIDM did not answer the claim of {command_id}: {e}")
        return False
    service_runner.journal_update(command_id, state="claimed")
    service_runner.write_event(command_id, "claimed", job.operation,
                               job.claims.get("requested_by"), None)
    say(f"Claimed {command_id}: {job.operation} on {job.args['project']}.")
    pool.add(job)
    return True


def doorbell(url: str, etag: str | None) -> tuple[int, str | None]:
    import urllib.error
    import urllib.request
    try:
        request = urllib.request.Request(autoidm.check_url(url, "/v1/service/checkin"),
                                         headers={"If-None-Match": etag} if etag else {})
        with urllib.request.urlopen(request, timeout=DOORBELL_TIMEOUT_SECONDS) as response:
            response.read()
            return response.status, response.headers.get("ETag")
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, None
    except (urllib.error.URLError, OSError, autoidm.AutoidmError) as e:
        console.warn_once(f"pdt could not read the AutoIDM doorbell: {console.escape(str(e))}")
        return 0, None


def set_clock(served: Served, server_time: object) -> None:
    try:
        moment = datetime.fromisoformat(str(server_time))
    except ValueError:
        return
    offset = moment.timestamp() - time.time()
    served.clock.offset = offset
    autoidm.clock_offset = offset
    now = time.time()
    if (abs(offset) > CLOCK_WARNING_SECONDS
            and now - served.clock_warned_at >= CLOCK_WARNING_EVERY_SECONDS):
        served.clock_warned_at = now
        say(f"The clock of this computer differs from AutoIDM's clock by {round(offset)} seconds. "
            "pdt corrects its own checks. Set the clock, because the jobs read it too.")


def apply_key_sets(served: Served, key_sets: object) -> None:
    for token in key_sets if isinstance(key_sets, list) else []:
        try:
            served.keys_version, served.keys = service_commands.next_key_set(
                token, served.keys_version, served.keys, served.customer)
        except Refused as e:
            message = f"pdt refused new command keys: {e}"
            say(message)
            service_runner.write_log_line(message, KEY_SET_EVENT)
            return
        if served.keys_file is not None:
            saved = json.loads(served.keys_file.read_text(encoding="utf-8"))
            config.write_text_atomically(served.keys_file, json.dumps(
                {**saved, "command_keys": served.keys,
                 "command_keys_version": served.keys_version}) + "\n")
        say(f"Now holds command keys of version {served.keys_version}.")


def checkin_answer(served: Served, settings: dict, running: list[str], claiming: list[str],
                   state: dict) -> dict:
    connected = served.connection.read()[0] if served.connection else False
    now = time.time()
    interval = served.schedule.checkin_interval(connected, now)
    answer = autoidm.post(served.registration, "/v1/service/checkin", {
        "version": 1, "pdt_version": __version__, "after": state.get("after"),
        "running": running[:MAX_LISTED_IDS], "claiming": claiming[:MAX_LISTED_IDS],
        "interval_seconds": max(60, min(86400, interval)),
        "clock_offset_seconds": round(served.clock.offset),
        "command_keys_version": served.keys_version,
        "projects": checkin_projects(settings, served.customer), **served.facts},
        retry=False)
    if answer.get("schema") not in (None, 1):
        raise autoidm.AutoidmError(f"AutoIDM answered /v1/service/checkin with the schema "
                                   f"{answer['schema']!r}, which this pdt does not read")
    set_clock(served, answer.get("server_time"))
    served.schedule.apply(answer)
    apply_key_sets(served, answer.get("key_sets"))
    return answer


def check_in(served: Served, settings: dict, pool: Pool, state: dict) -> bool:
    answer = checkin_answer(served, settings, pool.ids(), service_runner.claiming_ids(), state)
    state["last_checkin"] = time.time()
    state["status"] = answer.get("status")
    if answer.get("doorbell_url"):
        state["doorbell_url"] = answer["doorbell_url"]
    if answer.get("ws_url") and served.connection:
        served.connection.keepalive_seconds = served.schedule.ws_keepalive_seconds
        served.connection.start(answer["ws_url"])
    if answer.get("status") == "pending":
        say_once("A customer administrator must approve this installation. The service keeps "
                 "checking in.")
    commands = answer.get("commands") if isinstance(answer.get("commands"), list) else []
    settled = True
    for item in commands:
        if not accept(item, served, settings, pool):
            settled = False
            break
    if settled and answer.get("after"):
        state["after"] = answer["after"]
    for item in answer.get("cancels") or []:
        apply_cancel(item, served, pool)
    return len(commands) >= CHECKIN_PAGE or not settled


def apply_cancel(item: object, served: Served, pool: Pool) -> None:
    command_id = item.get("command_id") if isinstance(item, dict) else None
    token = item.get("token") if isinstance(item, dict) else None
    try:
        service_commands.verify_cancel(token, command_id, served.registration.installation_id,
                                       served.customer, served.keys, served.clock.now())
    except Refused as e:
        say(f"pdt ignored a cancel of {command_id}: {e}")
        return
    if pool.cancel(command_id):
        say(f"Cancelling {command_id}.")


def check(served: Served, settings: dict, pool: Pool, state: dict) -> None:
    schedule = served.schedule
    now = time.time()
    connected, frames, wanted_at = served.connection.read()
    schedule.frames_seen(frames)
    if wanted_at is not None and now >= wanted_at:
        schedule.wanted = True
        served.connection.clear_wanted()
    url = state.get("doorbell_url")
    if url and schedule.doorbell_due(connected, now):
        known = state.get("etag")
        status, etag = doorbell(url, known)
        schedule.doorbell_read(status, frames, connected, bool(known), now)
        if status:
            state["last_doorbell"] = now
        if etag:
            state["etag"] = etag
    if schedule.checkin_due(connected, now):
        try:
            more = check_in(served, settings, pool, state)
        except autoidm.AutoidmError as e:
            say(f"AutoIDM did not answer the check-in of {served.customer}: {e}")
            schedule.checkin_failed(now)
        else:
            schedule.checkin_done(time.time(), more)


def settings_mark(settings: dict) -> tuple:
    return (settings.get("customer"), settings.get("projects"),
            tuple(settings.get("named_projects") or []))


def current_settings(last: dict) -> dict | None:
    try:
        found = read_settings()
    except InstallError:
        return last
    if found is not None:
        return found
    return last if settings_path().exists() else None


def loop(served: Served, settings: dict, stop: threading.Event) -> int:
    def project_of(name: str) -> Path | None:
        return projects_of(settings).get(name)

    pool = Pool()
    _said_once.clear()
    state = read_state()
    last_recover = 0.0
    seen = settings_mark(settings)
    try:
        while not stop.is_set():
            try:
                settings = current_settings(settings)
                if settings is None:
                    say("The service settings are gone, so the service stops.")
                    break
                if settings_mark(settings) != seen:
                    seen = settings_mark(settings)
                    os.environ[service_runner.PROJECTS_HOME_ENV] = str(projects_folder(settings))
                    served.schedule.wanted = True
                check(served, settings, pool, state)
                write_state(state)
                pool.start_ready()
                if time.monotonic() - last_recover >= RECOVER_SECONDS:
                    service_runner.recover(pool, served.registration, served.clock, project_of)
                    last_recover = time.monotonic()
            except Exception as e:
                say(f"One pass of the service failed: {type(e).__name__}: {e}")
            console.drain()
            stop.wait(LOOP_SECONDS)
    finally:
        served.connection.stop()
        pool.stop_all()
        write_state(state)
        console.drain()
    return 0


def say_no_registration() -> None:
    console.error(f"This computer has no AutoIDM registration in "
                  f"{console.value(autoidm.folder())}. Run {console.value('pdt register')} "
                  "first.")


def registrations_here() -> list[str]:
    return sorted(path.stem for path in autoidm.folder().glob("cus_*.json"))


def one_customer_message(customer: str) -> str:
    return (f"This computer runs jobs as {service_account()} for {console.value(customer)}. One "
            "computer serves one customer, because a job can read each file on this computer.")


def access_problem(settings: dict) -> bool:
    if not service_access.is_admin():
        return False
    folders = []
    python = settings.get("python")
    if python:
        folders.append(service_install.version_folder(settings.get("pdt_version") or __version__))
    home = projects_folder(settings)
    folders.append(home)
    folders.extend(root for root in projects_of(settings).values() if root.parent != home)
    for folder in folders:
        if not folder.exists():
            continue
        try:
            found = service_access.writable_by_others(folder)
        except service_access.AccessError as e:
            console.error(console.escape(str(e)))
            say(f"{e} The service stopped.")
            return True
        if found:
            account, path = found
            console.error(f"The account {console.value(account)} can change "
                          f"{console.value(path)}, so the pdt service does not run from "
                          f"{console.value(folder)} as {console.value(service_account())}.")
            console.next_steps(service_access.repair_commands(folder))
            say(f"The account {account} can change {path}, so the service stopped.")
            return True
    return False


def settings_to_serve() -> dict | None:
    try:
        settings = read_settings()
    except InstallError as e:
        console.error(console.escape(str(e)))
        return None
    if settings is not None:
        return settings
    found = registrations_here()
    if not found:
        say_no_registration()
        return None
    if len(found) > 1:
        console.error(f"This computer holds registrations of {console.value(len(found))} "
                      "customers, and the service serves one. Name it with "
                      f"{console.value('pdt service install')}.")
        return None
    if os.name == "nt" and not require_admin("install"):
        return None
    folder, named = choose_projects(None, {})
    settings = {"version": service_install.SETTINGS_VERSION, "customer": found[0],
                "projects": folder, "named_projects": named, "backend": None,
                "pdt_version": __version__, "python": None, "proxy": service_install.find_proxy(None),
}
    write_data_folders()
    write_settings(settings)
    return settings


def write_data_folders() -> None:
    data = autoidm.data_folder()
    service_access.limit(data, private=False)
    service_access.limit(service_runner.service_folder())


def run(stop: threading.Event) -> int:
    settings = settings_to_serve()
    if settings is None:
        return 1
    return serve(settings, stop)


def serve(settings: dict, stop: threading.Event) -> int:
    with one_service() as first:
        if not first:
            console.error("A pdt service already runs on this computer. A computer has one "
                          "service.")
            return 1
        if access_problem(settings):
            return 1
        os.environ.update(service_install.service_env(settings))
        os.environ[service_runner.PROJECTS_HOME_ENV] = str(projects_folder(settings))
        served = machine_served(settings)
        if served is None:
            console.error("The service has no registration to serve. Run "
                          f"{console.value('pdt register')} first.")
            return 1
        config.write_text_atomically(pid_path(), json.dumps({"pid": os.getpid()}) + "\n")
        try:
            return loop(served, settings, stop)
        finally:
            pid_path().unlink(missing_ok=True)


def install_next_step() -> str:
    return "pdt service install" if os.name == "nt" else "sudo pdt service install"


def require_admin(action: str) -> bool:
    if service_access.is_admin():
        return True
    if os.name == "nt":
        console.error(f"pdt service {console.value(action)} needs an administrator. Open a "
                      "terminal with Run as administrator.")
        console.next_steps([(f"pdt service {action}", "run it in that terminal")])
    else:
        console.error(f"pdt service {console.value(action)} needs root.")
        console.next_steps([(f"sudo pdt service {action}", "run it as root")])
    return False


def ask_access(assume_yes: bool) -> bool:
    console.say(console.escape(ACCESS_STATEMENT))
    if assume_yes:
        return True
    if not can_prompt(None):
        console.warn("There is no one to answer. Run this in a terminal, or add --yes.")
        return False
    try:
        return console.confirm("Install the pdt service?")
    except EOFError:
        return False


def last_log_error() -> str:
    try:
        lines = log_path().read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return "The service wrote no log."
    return lines[-1] if lines else "The service wrote no log."


def wait_first_checkin(started: float) -> bool:
    deadline = time.monotonic() + FIRST_CHECKIN_SECONDS
    with console.waiting(f"waiting: first check-in by {service_account()}"):
        while time.monotonic() < deadline:
            if (read_state().get("last_checkin") or 0) >= started:
                return True
            time.sleep(1)
    return False


def install_for(customer: str, projects: str | None, backend: str | None,
                proxy: str | None) -> int:
    try:
        current = read_settings() or {}
    except InstallError:
        current = {}
    if current.get("customer") not in (None, customer):
        console.error(console.escape(one_customer_message(current["customer"])))
        return 1
    try:
        name = service_backends.choose(backend)
        write_data_folders()
        service_access.limit(service_runner.service_folder() / "projects")
        folder, named = choose_projects(projects, current)
        settings = {"version": service_install.SETTINGS_VERSION, "customer": customer,
                    "projects": folder, "named_projects": named, "backend": name,
                    "pdt_version": __version__, "python": None,
                    "proxy": service_install.find_proxy(proxy) or current.get("proxy") or {}}
        with console.waiting(f"installing: pdt {__version__} under "
                             f"{service_install.versions_folder()}"):
            settings["python"] = str(service_install.install_running(settings))
        if access_problem(settings):
            return 1
        previous = current.get("backend")
        if previous in service_backends.BACKENDS and previous != name:
            service_backends.BACKENDS[previous].remove()
        write_settings(settings)
        started = time.time()
        chosen = service_backends.BACKENDS[name]
        chosen.remove()
        chosen.define(settings["python"], service_runner.service_folder())
        chosen.start(settings["python"])
    except (InstallError, service_backends.BackendError, OSError, RuntimeError) as e:
        console.error(console.escape(str(e)))
        return 1
    if not wait_first_checkin(started):
        console.error(f"The pdt service did not check in within "
                      f"{console.value(FIRST_CHECKIN_SECONDS)} seconds. The last line of "
                      f"{console.value(log_path())} is: {console.escape(last_log_error())}")
        return 1
    console.done(f"The pdt service runs through {console.value(name)} and checked in as "
                 f"{console.value(service_account())}.")
    show_settings(settings)
    return 0


def install(projects: str | None, backend: str | None, proxy: str | None,
            assume_yes: bool) -> int:
    if not require_admin("install"):
        return 1
    if not registrations_here():
        code = register_cli.register(None, None, assume_yes, install_service=False)
        if code != 0:
            return code
    found = registrations_here()
    if not found:
        say_no_registration()
        return 1
    if len(found) > 1:
        console.error(f"{console.value(autoidm.folder())} holds registrations of "
                      f"{console.value(', '.join(found))}. One computer serves one customer, "
                      "because a job can read each file on this computer. Delete the files of "
                      "the other customers, then run this command again.")
        return 1
    if not ask_access(assume_yes):
        console.next_steps([(install_next_step(), "install it later")])
        return 1
    return install_for(found[0], projects, backend, proxy)


def show_settings(settings: dict) -> None:
    console.field("Customer", settings.get("customer") or "")
    console.field("Projects folder", settings.get("projects", ""))
    for named in settings.get("named_projects") or []:
        console.field("Project", named)


def start() -> int:
    if not require_admin("start"):
        return 1
    try:
        settings = read_settings() or {}
    except InstallError as e:
        console.error(console.escape(str(e)))
        return 1
    name = settings.get("backend")
    if name not in service_backends.BACKENDS:
        console.error("This computer has no installed pdt service.")
        console.next_steps([(install_next_step(), "install it")])
        return 1
    backend = service_backends.BACKENDS[name]
    try:
        if backend.running():
            console.done(f"The pdt service already runs through {console.value(name)}.")
            return 0
        backend.start(settings["python"])
    except service_backends.BackendError as e:
        console.error(console.escape(str(e)))
        return 1
    console.done(f"Started the pdt service through {console.value(name)}.")
    return 0


def stop_children(registration: autoidm.Registration | None) -> None:
    for entry in service_runner.journal_entries():
        if entry.get("result") or entry.get("state") != "started":
            continue
        command_id, pid = entry["command_id"], entry.get("pid")
        if not service_runner.is_same_process(pid, entry.get("process_created")):
            continue
        created = entry.get("process_created")
        service_runner.stop_tree(pid, lambda: not service_runner.is_same_process(pid, created))
        result = service_runner.result_of("cancelled", message="pdt service remove stopped it.")
        service_runner.journal_update(command_id, state="finished", result=result, reported=False,
                                      finished_at=service_runner.now_text())
        sent = registration is not None and service_runner.send_result(registration, command_id,
                                                                       result)
        console.say(f"Stopped {console.value(command_id)}. AutoIDM "
                    f"{'has' if sent else 'does not have'} the result.")


def remove(assume_yes: bool) -> int:
    if not require_admin("remove"):
        return 1
    try:
        settings = read_settings()
    except InstallError as e:
        console.error(console.escape(str(e)))
        return 1
    if settings is None:
        console.say("This computer has no pdt service.")
        return 0
    customer = settings.get("customer")
    if not assume_yes:
        console.say(f"pdt will stop the service, revoke this installation at AutoIDM, and delete "
                    f"the key of {console.value(customer)}.")
        if not can_prompt(None):
            console.warn("There is no one to answer. Run this in a terminal, or add --yes.")
            return 1
        try:
            if not console.confirm("Remove the pdt service?"):
                console.say("Cancelled. Nothing changed.")
                return 0
        except EOFError:
            return 0
    try:
        registration = autoidm.machine_registration(customer) if customer else None
    except (autoidm.AutoidmError, ValueError) as e:
        console.warn(console.escape(str(e)))
        registration = None
    stop_children(registration)
    name = settings.get("backend")
    if name in service_backends.BACKENDS:
        try:
            service_backends.BACKENDS[name].remove()
        except service_backends.BackendError as e:
            console.error(console.escape(str(e)))
            return 1
    if registration is not None:
        try:
            answer = autoidm.post(registration, "/v1/service/remove", {"version": 1})
        except autoidm.AutoidmError as e:
            console.error(f"AutoIDM did not revoke this installation, so the key stays: "
                          f"{console.escape(str(e))}. Run the command again.")
            return 1
        console.say(f"AutoIDM revoked this installation and "
                    f"{console.value(answer.get('children', 0))} child installations.")
        for path in autoidm.files(customer):
            path.unlink(missing_ok=True)
    settings_path().unlink(missing_ok=True)
    console.done(f"Removed the pdt service. Backend: {console.value(name or 'none')}.")
    console.heading("These stay on this computer:")
    console.bullet(f"data folder {console.value(autoidm.data_folder())}")
    console.bullet(f"journal {console.value(service_runner.service_folder() / 'journal')}")
    console.bullet(f"version folders {console.value(service_install.versions_folder())}")
    for name, folder in projects_of(settings).items():
        console.bullet(f"project {console.value(name)} in {console.value(folder)}")
    return 0


def status_record() -> dict:
    settings = read_settings()
    if settings is None:
        return {"installed": False}
    states = read_state()
    name = settings.get("backend")
    running = False
    if name in service_backends.BACKENDS:
        try:
            running = service_backends.BACKENDS[name].running()
        except service_backends.BackendError as e:
            console.warn(console.escape(str(e)))
    pid = running_pid()
    commands = [{"command_id": entry["command_id"], "operation": entry.get("operation"),
                 "state": entry.get("state"), "started_at": entry.get("started_at")}
                for entry in service_runner.journal_entries()
                if not entry.get("result") and entry.get("state") in ("claimed", "started")]
    return {"installed": name in service_backends.BACKENDS, "backend": name or "none",
            "running": running or pid is not None, "backend_running": running, "pid": pid,
            "customer": settings.get("customer"), "status": states.get("status"),
            "pdt_version": settings.get("pdt_version"),
            "projects_folder": str(projects_folder(settings)),
            "projects": sorted(projects_of(settings)),
            "last_doorbell": states.get("last_doorbell"),
            "last_checkin": states.get("last_checkin"), "commands": commands}


def status(as_json: bool = False) -> int:
    found = status_record()
    if as_json:
        console.data(json.dumps(found))
        return 0
    if not found["installed"] and "customer" not in found:
        console.say(f"This computer has no pdt service. To add one, run "
                    f"{console.value('pdt service install')}.")
        return 0
    if found["installed"]:
        words = f"{found['backend']} ({'running' if found['backend_running'] else 'not running'})"
        if found["pid"] and not found["backend_running"]:
            words += f", and a service runs in the foreground, PID {found['pid']}"
    elif found["pid"]:
        words = f"none (running in the foreground, PID {found['pid']})"
    else:
        words = "none (not running)"
    console.field("Backend", words)
    console.field("Customer", found["customer"] or "")
    console.field("Version", found["pdt_version"] or "")
    console.field("Projects folder", found["projects_folder"])
    console.field("Projects", ", ".join(found["projects"]) or "none")
    console.field("Last doorbell check", when(found["last_doorbell"]))
    console.field("Last check-in", when(found["last_checkin"]))
    if found["commands"]:
        console.table(["command", "operation", "state"],
                      [[item["command_id"], item["operation"] or "-", item["state"] or "-"]
                       for item in found["commands"]], ["bold"])
    else:
        console.field("Running commands", "none")
    return 0


def when(stamp: float | None) -> str:
    if not stamp:
        return "never"
    return time.strftime(service_runner.TIME_FORMAT, time.gmtime(stamp))


__all__ = ["run", "install", "start", "status", "remove", "sys", "service_delivery"]
