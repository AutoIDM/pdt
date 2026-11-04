"""`pdt register`: connect this computer to AutoIDM, and install what it needs.

With no `--code`, a person approves this computer in a browser: pdt prints the URL and a short
code, opens the browser, and waits. `pdt register --code CODE` uses a one-time code instead.
Both follow the rules below. The browser flow's pending key has one fixed name, because its
code comes from the server after the key exists, so a retry also sends the same public key.
The registration file also holds `command_keys`, the keys that `pdt service` pins to check
what the portal signs.

After the registration, pdt installs the service when this computer runs the project's jobs (the
project has the provider `windows` or no provider, or no project exists) and the process has
administrator rights and finds a backend. It states the access first, and `--yes` accepts it. With
no administrator rights it prints the next command. A computer that only deploys to a cloud gets
no service. Then, for each place of the project and each place the person chose in the browser,
it opens the place's Connect page and waits until AutoIDM finds the grant active. `--yes` prints
the pages and does not wait. One computer with the service serves one customer, so a
registration of another customer revokes the new installation and deletes the new key.

The code names the customer, so pdt learns the customer only from the answer.
It makes the key and writes it as pending-<hash of the code>.key before it
sends the code, so a retry after a lost answer sends the same public key and
AutoIDM answers with the same installation. When the answer comes, it writes
<customer_id>.key, then <customer_id>.json, then the project's pdt.yml, and only
then deletes the pending key, so a retry after a crash at any point sends the same
key, and a retry that finds that key and installation already saved finishes pdt.yml.
Each key file is written whole or not at all, and a pending key pdt cannot read counts as none. A lock on the
folder keeps two registers on one computer apart. On Windows the folder is
%ProgramData%\\pdt\\registrations, and before it writes a key, pdt limits the folder,
and so each file made in it, to SYSTEM (the scheduled task), Administrators, and
read access for the account that runs the command, so that account's terminals
without Run as administrator can read the key. The command needs an administrator
terminal. Each run replaces the access list, so only the last account to run it reads.
A computer that already holds another registration for the customer keeps it; the
command stops and names the file. Either way, in a project whose pdt.yml has no
`managed_services`, it adds `managed_services:` with the customer.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pdt import __version__, config, console, grants, managed_services, service_access
from pdt.utils import autoidm
from pdt.utils.email_auth import can_prompt

# Owner Administrators, and an access list that inherits nothing and gives the folder and each
# file and folder made in it full control to SYSTEM and Administrators, and read to {user}, the
# SID of the account that runs pdt register. SYSTEM (S-1-5-18) already has full control, so it
# gets no read entry.
FOLDER_SDDL = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FR;;;{user})"
SYSTEM_READ = "(A;OICI;FR;;;S-1-5-18)"
FOLDER_ENV = "PDT_REGISTRATIONS_FOLDER"
DEVICE_KEY = "pending-device.key"
DEFAULT_POLL_SECONDS = 3
GRANT_POLL_SECONDS = 5
GRANT_WAIT_SECONDS = 15 * 60
PLATFORM_NAMES = {"win32": "windows", "darwin": "macos"}


def restrict_folder(folder: Path) -> None:
    """`service_access.restrict_folder` with FOLDER_SDDL, so from the moment each file made in it
    exists, SYSTEM and Administrators can open it and the account that runs this can read it."""
    service_access.restrict_folder(folder, FOLDER_SDDL)


def write_key(path: Path, key: Ed25519PrivateKey) -> None:
    """Write the key through a temporary file in the same folder, so `path` holds a whole key
    or nothing. The temporary file is readable by its owner alone on POSIX, and takes the
    access list of the folder (`restrict_folder`) on Windows."""
    config.write_text_atomically(path, autoidm.private_pem(key).decode(), newline="\n")


def limit_folder(folder: Path, what: str) -> int:
    """`restrict_folder`, with one error line that says what pdt did not do."""
    try:
        restrict_folder(folder)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as e:
        console.error(f"pdt could not limit {console.value(folder)} to SYSTEM, "
                      f"Administrators, and your account, so it {console.escape(what)}: "
                      f"{console.escape(str(e))}")
        return 1
    return 0


def prepare_folder() -> int:
    folder = autoidm.folder()
    if os.name == "nt" and not service_access.is_admin():
        console.error(f"pdt register writes the key of this computer in {console.value(folder)}, "
                      "which only an administrator can do. Run it in a terminal opened with "
                      "Run as administrator.")
        return 1
    folder.mkdir(parents=True, exist_ok=True)
    return limit_folder(folder, "wrote no key")


def default_api_url() -> str:
    return os.environ.get(autoidm.URL_ENV, "").strip() or config.AUTOIDM_API_URL


def register(code: str | None, api_url: str | None, assume_yes: bool = False,
             install_service: bool = True) -> int:
    api_url = (api_url or default_api_url()).rstrip("/")
    prepared = prepare_folder()
    if prepared != 0:
        return prepared
    if code is None:
        answer, key, pending, user_code = register_in_browser(api_url)
    else:
        answer, key, pending, user_code = register_with_code(code, api_url)
    if answer is None:
        return 1
    customer = answer["customer_id"]
    other = other_customer(customer)
    if other is not None:
        return refuse_second_customer(answer, key, api_url, pending, other)
    with config.locked(autoidm.folder()):
        saved = save_registration(answer, key, api_url, pending)
    if saved != 0:
        return saved
    found = autoidm.machine_registration(customer)
    if user_code is not None:
        try:
            autoidm.post(found, "/v1/device/confirm", {"version": 1, "user_code": user_code})
        except autoidm.AutoidmError as e:
            console.warn(f"The portal page may not show success: {console.escape(str(e))}")
    if answer.get("email"):
        console.field("Signed in as", str(answer["email"]))
    result = install_here(customer, assume_yes) if install_service else 0
    pending_status = answer.get("status") == "pending"
    if pending_status:
        console.say(f"A customer administrator must approve the installation "
                    f"{console.value(answer['installation_id'])}. The service keeps checking in.")
    return connect_places(answer.get("grants") or [], found, assume_yes, pending_status) or result


def register_with_code(code: str, api_url: str) -> tuple:
    """The answer of /v1/register, the key, the pending key file, and no user code. None as the
    answer after one error line."""
    folder = autoidm.folder()
    code = code.strip().upper()
    digest = hashlib.sha256(code.replace("-", "").encode()).hexdigest()
    pending = folder / f"pending-{digest[:16]}.key"
    with config.locked(folder):
        try:
            key = autoidm.load_key(pending.read_bytes())
        except (OSError, ValueError, autoidm.AutoidmError):
            key = Ed25519PrivateKey.generate()
            write_key(pending, key)
        try:
            answer = autoidm.register(api_url, {
                "code": code, "public_key": autoidm.public_key(key),
                "hostname": socket.gethostname(), "pdt_version": __version__})
        except autoidm.AutoidmError as e:
            if e.code in ("unknown_code", "code_used"):
                pending.unlink()
            if e.code == "unknown_code":
                console.error(f"AutoIDM does not know the code {console.value(code)}, or it "
                              "expired. Ask AutoIDM for a new code.")
            elif e.code == "code_used":
                console.error(f"Another computer already used the code {console.value(code)}. "
                              "Ask AutoIDM for a new code.")
            else:
                console.error(console.escape(str(e)))
            return None, None, None, None
    return answer, key, pending, None


def save_registration(answer: dict, key: Ed25519PrivateKey, api_url: str, pending: Path) -> int:
    customer = answer["customer_id"]
    info, key_path = autoidm.files(customer)
    saved = {"installation_id": answer["installation_id"], "customer_id": customer,
             "api_url": api_url, "command_keys": answer.get("command_keys") or [],
             "command_keys_version": answer.get("command_keys_version") or 1}
    if info.is_file():
        found = json.loads(info.read_text(encoding="utf-8"))
        try:
            kept = autoidm.public_key(autoidm.load_key(key_path.read_bytes()))
        except (OSError, ValueError, autoidm.AutoidmError):
            kept = None
        if not (kept == autoidm.public_key(key)
                and found.get("installation_id") == answer["installation_id"]):
            console.error(f"This computer is already registered to AutoIDM for "
                          f"{console.value(customer)} as "
                          f"{console.value(found.get('installation_id'))} in "
                          f"{console.value(info)}. To register it again, delete "
                          f"{console.value(info)} and {console.value(key_path)}, then run "
                          "this command again with the same code.")
            save_customer(customer)
            return 1
        if found != saved:
            config.write_text_atomically(info, json.dumps(saved) + "\n")
    else:
        write_key(key_path, key)
        config.write_text_atomically(info, json.dumps(saved) + "\n")
    console.done(f"Registered this computer to AutoIDM for {console.value(customer)} as "
                 f"{console.value(answer['installation_id'])}.")
    console.field("Saved in", str(info))
    save_customer(customer)
    pending.unlink()
    return 0


def device_start_body(key: Ed25519PrivateKey) -> dict:
    projects = []
    try:
        projects = [managed_services.backend_name(config.project_name())]
    except config.ConfigError:
        pass
    return {"public_key": autoidm.public_key(key), "hostname": socket.gethostname(),
            "pdt_version": __version__,
            "platform": PLATFORM_NAMES.get(sys.platform, sys.platform),
            "places": grants.places_of_project(), "projects": projects}


def wait_for_decision(api_url: str, codes: dict) -> dict | None:
    """The approved answer of /v1/device/poll, or None after one error line. A poll that gets no
    answer tries again until the code expires."""
    interval = max(1, int(codes.get("interval") or DEFAULT_POLL_SECONDS))
    deadline = time.monotonic() + float(codes.get("expires_in") or 900)
    body = json.dumps({"user_code": codes["user_code"], "device_code": codes["device_code"]}).encode()
    with console.waiting("waiting: approval in the browser"):
        while time.monotonic() < deadline:
            try:
                answer = autoidm.send(f"{api_url}/v1/device/poll", body, {})
            except autoidm.AutoidmError as e:
                if e.code == "unknown_code":
                    break
                if e.status and e.status < 500 and e.status != 429:
                    console.error(console.escape(str(e)))
                    return None
                answer = {"state": "waiting"}
            if answer.get("state") == "approved":
                return answer
            if answer.get("state") == "denied":
                console.error("The request was denied in the browser. pdt registered nothing.")
                return None
            time.sleep(interval)
    console.error(f"The code {console.value(codes['user_code'])} expired. Run "
                  f"{console.value('pdt register')} again for a new code.")
    return None


def approve_in_browser(api_url: str, body: dict) -> tuple[dict | None, str | None]:
    """The device flow: the codes, the browser, and the wait. Returns the approved answer and the
    user code, or None after one error line."""
    data = json.dumps(body).encode()
    try:
        codes = autoidm.answered(
            autoidm.retried(lambda: autoidm.send(f"{api_url}/v1/device/start", data, {})),
            "/v1/device/start", "user_code", "device_code", "url")
        autoidm.check_url(codes["url"], "/v1/device/start")
    except autoidm.AutoidmError as e:
        console.error(console.escape(str(e)))
        return None, None
    console.say("To connect to AutoIDM, open this page and sign in:")
    console.command(codes["url"])
    console.field("Check that the page shows this code", codes["user_code"])
    webbrowser.open(codes["url"])
    answer = wait_for_decision(api_url, codes)
    if answer is None:
        return None, None
    try:
        autoidm.answered(answer, "/v1/device/poll", "installation_id", "customer_id")
    except autoidm.AutoidmError as e:
        console.error(console.escape(str(e)))
        return None, None
    if not managed_services.CUSTOMER.fullmatch(answer["customer_id"]):
        console.error(f"AutoIDM answered with the customer ID {console.value(answer['customer_id'])}, "
                      "which is not one.")
        return None, None
    return answer, codes["user_code"]


def register_in_browser(api_url: str) -> tuple:
    folder = autoidm.folder()
    pending = folder / DEVICE_KEY
    with config.locked(folder):
        try:
            key = autoidm.load_key(pending.read_bytes())
        except (OSError, ValueError, autoidm.AutoidmError):
            key = Ed25519PrivateKey.generate()
            write_key(pending, key)
    answer, user_code = approve_in_browser(api_url, device_start_body(key))
    return answer, key, pending, user_code


def other_customer(customer: str) -> str | None:
    """The customer that stops a registration of `customer`: the one the service settings name,
    or, when `pdt register` would install the service, another registration of this computer."""
    from pdt import service, service_install
    try:
        settings = service_install.read_settings()
    except service_install.InstallError:
        settings = None
    if settings and settings.get("customer") not in (None, customer):
        return settings["customer"]
    if service_wanted() and service_access.is_admin():
        others = [name for name in service.registrations_here() if name != customer]
        if others:
            return others[0]
    return None


def refuse_second_customer(answer: dict, key: Ed25519PrivateKey, api_url: str, pending: Path,
                           other: str) -> int:
    """One computer with the service serves one customer. AutoIDM revokes the new installation,
    and pdt deletes the new key."""
    from pdt import service
    new = autoidm.Registration(answer["installation_id"], api_url, key, "new")
    try:
        autoidm.post(new, "/v1/service/remove", {"version": 1})
    except autoidm.AutoidmError as e:
        console.warn(f"AutoIDM did not revoke the new installation "
                     f"{console.value(answer['installation_id'])}: {console.escape(str(e))}")
    pending.unlink(missing_ok=True)
    console.error(console.escape(service.one_customer_message(other)))
    return 1


def service_wanted() -> bool:
    """Whether this computer runs the project's jobs: the project has the provider `windows` or no
    provider, or no project exists. A computer that only deploys to a cloud gets no service."""
    try:
        found = config.load_yaml(config.find_project() / config.PROJECT_FILE).get("platform")
    except config.ConfigError:
        return True
    provider = found.get("provider") if isinstance(found, dict) else None
    return provider in (None, "windows")


def install_here(customer: str, assume_yes: bool) -> int:
    """The automatic install of `pdt register`. A person says yes once, and signs in no more."""
    from pdt import service, service_backends
    if not service_wanted():
        return 0
    next_command = [(service.install_next_step(),
                     "run commands from the AutoIDM portal on this computer")]
    if not service_access.is_admin():
        console.next_steps(next_command)
        return 0
    try:
        service_backends.choose(None)
    except service_backends.BackendError as e:
        console.warn(f"pdt did not install the service: {console.escape(str(e))}")
        return 0
    if not service.ask_access(assume_yes):
        console.next_steps(next_command)
        return 0
    return service.install_for(customer, None, None, None)


def grant_status(found: autoidm.Registration, place: str) -> dict:
    return autoidm.post(found, "/v1/grants/status", {"version": 1, "place": place})


def connect_places(chosen: list[str], found: autoidm.Registration, assume_yes: bool,
                   pending: bool) -> int:
    """Open the Connect page of each place that has no active grant, and wait for each. `--yes`,
    a `pending` installation, and a run with no one to watch print the pages and do not wait."""
    places = sorted({*chosen, *grants.places_of_project()})
    waiting: dict[str, str] = {}
    for place in places:
        try:
            status = grant_status(found, place)
        except autoidm.AutoidmError as e:
            console.warn(f"AutoIDM did not answer about {console.value(place)}: "
                         f"{console.escape(str(e))}")
            continue
        if status.get("status") == "active":
            console.done(f"AutoIDM can deploy and run jobs in {console.value(place)}.")
        elif status.get("connect_url"):
            waiting[place] = status["connect_url"]
    if not waiting:
        return 0
    console.heading("To let AutoIDM deploy and run jobs in a place, open its Connect page:")
    for place, url in waiting.items():
        console.field(place, url)
    if assume_yes or pending or not can_prompt(None):
        return 0
    for url in waiting.values():
        webbrowser.open(url)
    deadline = time.monotonic() + GRANT_WAIT_SECONDS
    with console.waiting("waiting: the Connect pages"):
        while waiting and time.monotonic() < deadline:
            time.sleep(GRANT_POLL_SECONDS)
            for place in list(waiting):
                try:
                    status = grant_status(found, place).get("status")
                except autoidm.AutoidmError:
                    continue
                if status == "active":
                    del waiting[place]
                    console.done(f"Granted: AutoIDM can deploy and run jobs in "
                                 f"{console.value(place)}.")
    for place in waiting:
        console.warn(f"Not granted: AutoIDM found no active grant for {console.value(place)} "
                     f"in {console.value(GRANT_WAIT_SECONDS // 60)} minutes.")
    return 1 if waiting else 0


def save_customer(customer: str) -> None:
    try:
        path = config.find_project() / config.PROJECT_FILE
    except config.ConfigError:
        console.next_steps([("pdt.yml", f"add {console.value('managed_services:')} with "
                                        f"{console.value('customer: ' + customer)} to send the "
                                        "run history of a project to AutoIDM")])
        return
    found = config.load_yaml(path).get(managed_services.KEY)
    if found is None:
        config.save_managed_customer(customer)
        console.done(f"Saved {console.value('managed_services:')} with "
                     f"{console.value('customer: ' + customer)} to {console.value(path)}.")
    elif not isinstance(found, dict) or found.get("customer") != customer:
        console.warn(f"{console.value(path)} names another customer in "
                     f"{console.value('managed_services:')}, so this project does not send to "
                     f"AutoIDM for {console.value(customer)}.")
