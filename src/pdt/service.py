"""`pdt service`: run the portal's commands on this computer, with no open port.

One service serves each registration that `<data folder>/service/settings.json` names. On
Windows that folder has the access list of the registrations folder, so only SYSTEM and
Administrators can change the maximum level. For each registration, the service reads a presigned
doorbell URL, checks in when the doorbell changed or `checkin_seconds` passed, verifies each
command against the pinned keys, claims it, and starts it (pdt.service_runner). The server sets
the two intervals. `pdt service tick` does this once and stays until no command runs, so cron
needs no running process, and a tick that finds a service running exits at once.

The service serves each project the settings name, and each folder directly in the projects
folder that holds pdt.yml. The projects folder has a default, `<data folder>/service/projects`,
and a new project from `sync_down` goes there. The service reads this list at each check-in, so
a new project needs no restart. A project serves the customer that its `managed_services` names,
and a command for any other customer is refused, so one computer can serve two customers.

`pdt service run --worker` is the same loop for a cloud worker. Its registration, and the keys
it pins in PDT_COMMAND_KEYS, come from its environment. It has no settings file.
"""

from __future__ import annotations

import json
import os
import shutil
import secrets
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pdt import (__version__, config, console, managed_services, register_cli, service_backends,
                 service_commands, service_runner)
from pdt.service_commands import Refused
from pdt.service_runner import Job, Pool
from pdt.utils import autoidm

SETTINGS_VERSION = 1
COMMAND_KEYS_ENV = "PDT_COMMAND_KEYS"
LOG_LIMIT_BYTES = 1024 * 1024
DEFAULT_POLL_SECONDS = 30
DEFAULT_CHECKIN_SECONDS = 900
# Below this, a poll or a wake of the loop would cost more than it brings.
MINIMUM_POLL_SECONDS = 5
LOOP_SECONDS = 1.0
# The most commands /v1/service/checkin answers with: a full answer means there can be more.
CHECKIN_PAGE = 20
MAX_RUNNING_IDS = 100
DOORBELL_TIMEOUT_SECONDS = 10
_said_once: set[str] = set()


def settings_path() -> Path:
    return service_runner.service_folder() / "settings.json"


def state_path() -> Path:
    return service_runner.service_folder() / "state.json"


def read_settings() -> dict | None:
    try:
        found = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def write_settings(settings: dict) -> None:
    config.write_text_atomically(settings_path(), json.dumps(settings, indent=2) + "\n")


def read_state() -> dict:
    try:
        found = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def write_state(state: dict) -> None:
    config.write_text_atomically(state_path(), json.dumps(state, indent=2) + "\n")


def say(message: str) -> None:
    """A line for a person watching, and for the log file a service with no terminal leaves."""
    console.say(console.escape(message))
    path = service_runner.service_folder() / "service.log"
    try:
        if path.is_file() and path.stat().st_size > LOG_LIMIT_BYTES:
            os.replace(path, path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as log:
            log.write(f"{service_runner.now_text()} {message}\n")
    except OSError:
        pass


@contextmanager
def one_service():
    """Yield True while this process holds the lock of the machine's one service, else False."""
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


def projects_of(settings: dict) -> dict[str, Path]:
    """Each project the settings serve, by the name the portal uses."""
    try:
        folders = sorted(projects_folder(settings).iterdir())
    except OSError:
        folders = []
    folders += [Path(named) for named in settings.get("named_projects") or []]
    return {managed_services.backend_name(folder.name): folder for folder in folders
            if (folder / config.PROJECT_FILE).is_file()}


def choose_projects(wanted: str | None, current: dict) -> tuple[str, list[str]]:
    """The projects folder and the named projects after an install. A folder that holds pdt.yml
    is one named project, and any other folder is the projects folder. With no choice, an
    install in a project names that project."""
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


def say_once(message: str) -> None:
    """`say`, one time for each start of the service."""
    if message not in _said_once:
        _said_once.add(message)
        say(message)


def project_record(name: str, root: Path) -> dict:
    """One project of a check-in. AutoIDM refuses a whole check-in that holds one bad value, so
    a name or a head that fails its rule stays out, and delivery goes on."""
    head = None
    try:
        record = json.loads((root / config.STATE_DIR / "sync.json").read_text(encoding="utf-8"))
        head = record.get("snapshot_id") if isinstance(record, dict) else None
    except (OSError, ValueError):
        pass
    if not (isinstance(head, str) and service_commands.SNAPSHOT.fullmatch(head)):
        head = None
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
    return {"name": name, "head": head, "apps": apps}


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


@dataclass
class Served:
    customer: str
    registration: autoidm.Registration
    keys: list[dict]
    next_poll: float = 0.0
    checked: bool = False


def read_keys(customer: str) -> list[dict]:
    info, _key = autoidm.files(customer)
    saved = json.loads(info.read_text(encoding="utf-8"))
    keys = saved.get("command_keys") if isinstance(saved, dict) else None
    return keys if isinstance(keys, list) else []


def machine_served(settings: dict) -> dict[str, Served]:
    served = {}
    for customer in settings.get("registrations") or []:
        try:
            found = autoidm.machine_registration(customer)
            keys = read_keys(customer) if found else []
        except (autoidm.AutoidmError, OSError, ValueError) as e:
            console.warn_once(f"pdt cannot serve the registration of {console.value(customer)}: "
                              f"{console.escape(str(e))}")
            continue
        if found is None:
            console.warn_once(f"The service settings name {console.value(customer)}, which has "
                              "no registration on this computer.")
            continue
        if not keys:
            console.warn_once(f"The registration of {console.value(customer)} has no command "
                              "keys, so the service refuses each command for it.")
        served[customer] = Served(customer, found, keys)
    return served


def worker_served() -> dict[str, Served]:
    found = autoidm.environment_values()
    if not found:
        raise config.ConfigError(f"pdt service run --worker needs {', '.join(autoidm.SECRET_NAMES)} "
                                 "in the environment")
    try:
        keys = json.loads(os.environ.get(COMMAND_KEYS_ENV) or "[]")
    except ValueError:
        keys = []
    if not (isinstance(keys, list) and keys):
        raise config.ConfigError(f"pdt service run --worker needs {COMMAND_KEYS_ENV}: the JSON "
                                 "list of command keys")
    customer = found[autoidm.CUSTOMER_ENV]
    registration = autoidm.Registration(found[autoidm.ID_ENV], found[autoidm.URL_ENV].rstrip("/"),
                                        autoidm.load_key(found[autoidm.KEY_ENV].encode()),
                                        autoidm.ID_ENV)
    return {customer: Served(customer, registration, keys)}


def refuse(served: Served, command_id: str, message: str) -> None:
    """Record a refusal, and report it. A refused result needs no earlier claim."""
    result = {"outcome": "refused", "exit_code": None, "message": message, "run_id": None,
              "artifact_key": None}
    service_runner.journal_begin({
        "command_id": command_id, "customer_id": served.customer, "phase": "finished",
        "finished_at": service_runner.now_text(), "result": result, "reported": False})
    service_runner.send_result(served.registration, command_id, result)
    say(f"Refused {command_id}: {message}")


def locate(claims: dict, served: Served, settings: dict, worker: bool) -> Path | None:
    """The project folder of a command on this computer. A worker's comes from a snapshot, and
    sync_down and run_snapshot can act on a project that is not here yet. Raises Refused."""
    if worker:
        return None
    project = claims["args"]["project"]
    root = projects_of(settings).get(project)
    if root is not None:
        owner = project_customer(root)
        if owner is None and claims["operation"] == "sync_down":
            return root
        if owner is None:
            raise Refused(f"The project {project} has no {managed_services.KEY} in "
                          f"{config.PROJECT_FILE}. To add it, run pdt register in {root}.")
        if owner != served.customer:
            raise Refused(f"The project {project} names the customer {owner} in "
                          f"{config.PROJECT_FILE}, not {served.customer}.")
        return root
    if claims["operation"] in ("sync_down", "run_snapshot"):
        return None
    raise Refused(f"This computer does not serve a project named {project}.")


def accept(item: object, served: Served, settings: dict, worker: bool, pool: Pool) -> bool:
    """Verify, claim, and queue one command of a check-in. False when the claim got no answer,
    so the cursor must not pass this command."""
    command_id = item.get("command_id") if isinstance(item, dict) else None
    token = item.get("token") if isinstance(item, dict) else None
    if not (isinstance(command_id, str)
            and service_commands.COMMAND_ID.fullmatch(command_id)):
        say(f"AutoIDM sent a command with the ID {command_id!r}, which pdt ignores.")
        return True
    entry = service_runner.journal_read(command_id)
    if entry is not None and entry.get("phase") != "claiming":
        say(f"Refused {command_id}: this computer already holds that command ID.")
        return True
    maximum = settings.get("level", service_commands.DEFAULT_LEVEL)
    try:
        claims = service_commands.verify(token, command_id, served.registration.installation_id,
                                         served.customer, served.keys, maximum, worker)
        root = locate(claims, served, settings, worker)
        preset = None
        if claims["args"].get("preset"):
            if root is None:
                raise Refused(f"This computer has no project {claims['args']['project']} "
                              f"to read the preset {claims['args']['preset']} from.")
            preset = service_commands.load_preset(root, claims["args"]["preset"])
    except Refused as e:
        refuse(served, command_id, str(e))
        return True
    if entry is None:
        entry = {"command_id": command_id, "customer_id": served.customer, "phase": "claiming",
                 "operation": claims["operation"], "args": claims["args"],
                 "claim_id": secrets.token_hex(16)}
        service_runner.journal_begin(entry)
    try:
        service_runner.report(served.registration, command_id, fact="claimed",
                              claim_id=entry["claim_id"])
    except autoidm.AutoidmError as e:
        if e.code != "already_claimed":
            say(f"AutoIDM did not answer the claim of {command_id}: {e}")
            return False
        service_runner.journal_update(command_id, phase="finished", reported=True,
                                      result={"outcome": "not_claimed", "message": str(e)})
        return True
    service_runner.journal_update(command_id, phase="claimed",
                                  claimed_at=service_runner.now_text())
    say(f"Claimed {command_id}: {claims['operation']} on {claims['args']['project']}.")
    pool.add(Job(command_id, claims, served.registration, root, worker, preset))
    return True


def doorbell(url: str, etag: str | None) -> tuple[int, str | None]:
    """GET the doorbell. Returns the status, and the new ETag on a 200. Status 0 means no answer."""
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


def check_in(served: Served, settings: dict, worker: bool, pool: Pool, state: dict) -> None:
    projects = checkin_projects(settings, served.customer)
    answer = autoidm.post(served.registration, "/v1/service/checkin", {
        "version": 1, "pdt_version": __version__,
        "maximum_level": settings.get("level", service_commands.DEFAULT_LEVEL),
        "after": state.get("after"), "running": pool.ids()[:MAX_RUNNING_IDS],
        "projects": projects})
    state["last_checkin"] = time.time()
    for name in ("doorbell_url", "poll_seconds", "checkin_seconds"):
        if answer.get(name):
            state[name] = answer[name]
    commands = answer.get("commands") if isinstance(answer.get("commands"), list) else []
    for item in commands:
        if not accept(item, served, settings, worker, pool):
            break
        if isinstance(item, dict) and isinstance(item.get("command_id"), str):
            state["after"] = item["command_id"]
    state["more"] = len(commands) >= CHECKIN_PAGE
    for command_id in answer.get("cancels") or []:
        if isinstance(command_id, str):
            pool.cancel(command_id)
            say(f"Cancelling {command_id}.")


def check(served: Served, settings: dict, worker: bool, pool: Pool, states: dict) -> None:
    """Steps 1 to 3 of the contract's delivery for one registration."""
    state = states.setdefault(served.customer, {})
    now = time.time()
    url = state.get("doorbell_url")
    due = (not url or state.get("more")
           or now - state.get("last_checkin", 0) >= state.get("checkin_seconds",
                                                              DEFAULT_CHECKIN_SECONDS))
    new_etag = None
    if url:
        status, new_etag = doorbell(url, state.get("etag"))
        if status:
            state["last_doorbell"] = now
        due = due or status in (200, 403)
    if due:
        try:
            check_in(served, settings, worker, pool, state)
        except autoidm.AutoidmError as e:
            say(f"AutoIDM did not answer the check-in of {served.customer}: {e}")
        else:
            state["etag"] = new_etag or state.get("etag")
    served.checked = not state.get("more")
    poll = state.get("poll_seconds", DEFAULT_POLL_SECONDS)
    served.next_poll = time.monotonic() + (0 if state.get("more")
                                           else max(MINIMUM_POLL_SECONDS, poll))


def loop(served: dict[str, Served], settings_reader, worker: bool, once: bool) -> int:
    pool = Pool()
    stopping: list[int] = []

    def stop(signum: int, _frame: object) -> None:
        stopping.append(signum)

    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    _said_once.clear()
    states = read_state()
    registrations = {name: item.registration for name, item in served.items()}
    service_runner.recover(pool, registrations)
    last_recover = time.monotonic()
    try:
        while not stopping:
            settings = settings_reader()
            changed = False
            for item in served.values():
                if time.monotonic() >= item.next_poll:
                    check(item, settings, worker, pool, states)
                    changed = True
            if changed:
                write_state(states)
            pool.start_ready()
            if time.monotonic() - last_recover >= DEFAULT_POLL_SECONDS:
                service_runner.recover(pool, registrations)
                last_recover = time.monotonic()
            console.drain()
            if once and all(item.checked for item in served.values()) and not pool.ids():
                break
            time.sleep(LOOP_SECONDS)
    finally:
        pool.stop_all()
        write_state(states)
        console.drain()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def run(worker: bool = False) -> int:
    """`pdt service run`: the foreground loop."""
    if worker:
        return serve({"level": "manage", "projects": ""}, worker=True, once=False)
    settings = read_settings()
    if settings is None:
        console.error(f"This computer has no pdt service. Run {console.value('pdt service install')} "
                      "first.")
        return 1
    return serve(settings, worker=False, once=False)


def tick() -> int:
    """`pdt service tick`: one pass, and then wait for the commands it started."""
    settings = read_settings()
    if settings is None:
        console.error(f"This computer has no pdt service. Run {console.value('pdt service install')} "
                      "first.")
        return 1
    return serve(settings, worker=False, once=True)


def serve(settings: dict, worker: bool, once: bool) -> int:
    with one_service() as first:
        if not first:
            if once:
                console.status("A pdt service already runs on this computer.")
                return 0
            console.error("A pdt service already runs on this computer. A computer has one "
                          "service.")
            return 1
        uv = settings.get("uv")
        if uv:
            os.environ["PATH"] = str(Path(uv).parent) + os.pathsep + os.environ.get("PATH", "")
        os.environ[service_runner.PROJECTS_HOME_ENV] = str(projects_folder(settings))
        served = worker_served() if worker else machine_served(settings)
        if not served:
            console.error("The service has no registration to serve. Run "
                          f"{console.value('pdt register')} first.")
            return 1
        reader = (lambda: settings) if worker else (lambda: read_settings() or settings)
        return loop(served, reader, worker, once)


def registrations_here() -> list[str]:
    return sorted(path.stem for path in autoidm.folder().glob("cus_*.json"))


def needs_admin(action: str) -> bool:
    if os.name == "nt" and not register_cli.is_admin():
        console.error(f"pdt service {console.value(action)} changes what the service may do here, "
                      "which only an administrator can do. Run it in a terminal opened with "
                      "Run as administrator.")
        return True
    return False


def prepare_folder() -> int:
    folder = service_runner.service_folder()
    folder.mkdir(parents=True, exist_ok=True)
    return register_cli.limit_folder(folder, "wrote no service settings")


def install(level: str | None, projects: str | None, backend: str | None) -> int:
    if needs_admin("install"):
        return 1
    found = registrations_here()
    if not found:
        console.error(f"This computer has no AutoIDM registration in "
                      f"{console.value(autoidm.folder())}. Run {console.value('pdt register')} "
                      "first.")
        return 1
    code = prepare_folder()
    if code != 0:
        return code
    try:
        name = service_backends.choose(backend)
    except service_backends.BackendError as e:
        console.error(console.escape(str(e)))
        return 1
    current = read_settings() or {}
    folder, named = choose_projects(projects, current)
    settings = {"version": SETTINGS_VERSION,
                "level": level or current.get("level", service_commands.DEFAULT_LEVEL),
                "projects": folder, "named_projects": named, "registrations": found,
                "backend": name, "uv": shutil.which("uv"), "installed_by": __version__}
    if settings["uv"] is None:
        console.warn(f"pdt did not find {console.value('uv')} on the PATH, so an app run by the "
                     "service needs it on the service account's PATH.")
    previous = current.get("backend")
    try:
        if previous in service_backends.BACKENDS and previous != name:
            service_backends.BACKENDS[previous].remove()
        write_settings(settings)
        service_backends.BACKENDS[name].install(service_runner.service_folder())
    except service_backends.BackendError as e:
        console.error(console.escape(str(e)))
        return 1
    console.done(f"The pdt service is installed. It runs through {console.value(name)}.")
    show_settings(settings)
    for customer in found:
        if not read_keys(customer):
            console.warn(f"The registration of {console.value(customer)} has no command keys. "
                         f"Delete {console.value(autoidm.files(customer)[0])} and "
                         f"{console.value(autoidm.files(customer)[1])}, then run "
                         f"{console.value('pdt register')}.")
    return 0


def show_settings(settings: dict) -> None:
    console.field("Maximum level", settings.get("level", ""))
    console.field("Projects folder", settings.get("projects", ""))
    for named in settings.get("named_projects") or []:
        console.field("Project", named)
    for customer in settings.get("registrations") or []:
        console.field("Registration", customer)


def allow(level: str) -> int:
    if needs_admin("allow"):
        return 1
    settings = read_settings()
    if settings is None:
        console.error(f"This computer has no pdt service. Run {console.value('pdt service install')} "
                      "first.")
        return 1
    settings["level"] = level
    write_settings(settings)
    console.done(f"The service now allows at most the level {console.value(level)}.")
    return 0


def remove() -> int:
    if needs_admin("remove"):
        return 1
    settings = read_settings() or {}
    name = settings.get("backend")
    if name not in service_backends.BACKENDS:
        console.say("This computer has no pdt service.")
        return 0
    try:
        service_backends.BACKENDS[name].remove()
    except service_backends.BackendError as e:
        console.error(console.escape(str(e)))
        return 1
    settings_path().unlink(missing_ok=True)
    console.done(f"Removed the pdt service from {console.value(name)}. The registrations stay.")
    return 0


def status(as_json: bool = False) -> int:
    settings = read_settings()
    if settings is None:
        if as_json:
            console.data(json.dumps({"installed": False}))
            return 0
        console.say(f"This computer has no pdt service. To add one, run "
                    f"{console.value('pdt service install')}.")
        return 0
    states = read_state()
    name = settings.get("backend")
    running = False
    if name in service_backends.BACKENDS:
        try:
            running = service_backends.BACKENDS[name].running()
        except service_backends.BackendError as e:
            console.warn(console.escape(str(e)))
    commands = [{"command_id": entry["command_id"], "operation": entry.get("operation"),
                 "project": (entry.get("args") or {}).get("project"),
                 "app": (entry.get("args") or {}).get("app"), "phase": entry.get("phase"),
                 "started_at": entry.get("started_at")}
                for entry in service_runner.journal_entries()
                if not entry.get("result") and entry.get("phase") in ("claimed", "running")]
    registrations = [{"customer_id": customer,
                      "last_doorbell": (states.get(customer) or {}).get("last_doorbell"),
                      "last_checkin": (states.get(customer) or {}).get("last_checkin")}
                     for customer in settings.get("registrations") or []]
    projects = sorted(projects_of(settings))
    if as_json:
        console.data(json.dumps({"installed": True, "backend": name, "running": running,
                                 "level": settings.get("level"),
                                 "projects_folder": str(projects_folder(settings)),
                                 "projects": projects, "registrations": registrations,
                                 "commands": commands}))
        return 0
    console.field("Backend", f"{name} ({'running' if running else 'not running'})")
    console.field("Maximum level", settings.get("level", ""))
    console.field("Projects folder", str(projects_folder(settings)))
    console.field("Projects", ", ".join(projects) or "none")
    for item in registrations:
        console.field("Registration", item["customer_id"])
        console.detail(f"last doorbell check: {console.value(when(item['last_doorbell']))}", 2)
        console.detail(f"last check-in: {console.value(when(item['last_checkin']))}", 2)
    if commands:
        console.table(["command", "operation", "project", "app", "phase"],
                      [[item["command_id"], item["operation"] or "-", item["project"] or "-",
                        item["app"] or "-", item["phase"] or "-"] for item in commands], ["bold"])
    else:
        console.field("Running commands", "none")
    return 0


def when(stamp: float | None) -> str:
    if not stamp:
        return "never"
    return time.strftime(service_runner.TIME_FORMAT, time.gmtime(stamp))
