"""`pdt register`: authenticate this computer for AutoIDM managed services.

With no `--code`, a person approves this computer in a browser: pdt prints the URL and a short
code, opens the browser, and waits. `pdt register --code CODE` uses a one-time code instead.
Both follow the rules below. The browser flow's pending key has one fixed name, because its
code comes from the server after the key exists, so a retry also sends the same public key.
The registration file also holds `command_keys`, the keys that `pdt service` pins to check
what the portal signs. After approval, pdt makes each grant the person chose in the browser. A
person at a terminal who chose none gets one question for each place of the project.

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

import ctypes
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

from pdt import __version__, config, console, grants, managed_services
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
PLATFORM_NAMES = {"win32": "windows", "darwin": "macos"}
# .NET calls only, not Get-Acl, Set-Acl, or New-Object: Windows PowerShell started under
# PowerShell 7 inherits its PSModulePath and cannot load the modules that hold those commands.
# It prints the access list as .NET holds it before it saves it, then as it reads it back, so
# both pass through the same .NET form (two-letter SIDs such as LA, merged entries).
SET_FOLDER_SDDL = ("$ErrorActionPreference = 'Stop'; "
                   f"$folder = [IO.DirectoryInfo]$env:{FOLDER_ENV}; "
                   f"$sddl = '{FOLDER_SDDL}'.Replace('{{user}}', "
                   "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value)"
                   f".Replace('{SYSTEM_READ}', ''); "
                   "$acl = $folder.GetAccessControl(); "
                   "$acl.SetSecurityDescriptorSddlForm($sddl); "
                   "$acl.GetSecurityDescriptorSddlForm('Owner, Access'); "
                   "$folder.SetAccessControl($acl); "
                   "$folder.GetAccessControl().GetSecurityDescriptorSddlForm('Owner, Access')")


def restrict_folder(folder: Path) -> None:
    """On Windows, replace the owner and the whole access list of `folder` with FOLDER_SDDL,
    so from the moment each file made in it exists, SYSTEM and Administrators can open it and
    the account that runs this can read it. It reads the folder back and raises RuntimeError
    when it does not match."""
    if os.name != "nt":
        return
    shown = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            SET_FOLDER_SDDL], env={**os.environ, FOLDER_ENV: str(folder)},
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           check=True).stdout.split()
    if len(shown) != 2:
        raise RuntimeError(f"PowerShell printed {' '.join(shown) or 'nothing'}, not the access "
                           "list pdt set and the one it read back")
    if shown[0] != shown[1]:
        raise RuntimeError(f"its owner and access list are {shown[1]}, not {shown[0]}")


def write_key(path: Path, key: Ed25519PrivateKey) -> None:
    """Write the key through a temporary file in the same folder, so `path` holds a whole key
    or nothing. The temporary file is readable by its owner alone on POSIX, and takes the
    access list of the folder (`restrict_folder`) on Windows."""
    config.write_text_atomically(path, autoidm.private_pem(key).decode(), newline="\n")


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


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
    if os.name == "nt" and not is_admin():
        console.error(f"pdt register writes the key of this computer in {console.value(folder)}, "
                      "which only an administrator can do. Run it in a terminal opened with "
                      "Run as administrator.")
        return 1
    folder.mkdir(parents=True, exist_ok=True)
    return limit_folder(folder, "wrote no key")


def default_api_url() -> str:
    return os.environ.get(autoidm.URL_ENV, "").strip() or config.AUTOIDM_API_URL


def register(code: str | None, api_url: str | None) -> int:
    api_url = (api_url or default_api_url()).rstrip("/")
    code_prepared = prepare_folder()
    if code_prepared != 0:
        return code_prepared
    if code is None:
        return register_in_browser(api_url)
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
            return 1
        return save_registration(answer, key, api_url, pending)


def save_registration(answer: dict, key: Ed25519PrivateKey, api_url: str, pending: Path) -> int:
    customer = answer["customer_id"]
    info, key_path = autoidm.files(customer)
    saved = {"installation_id": answer["installation_id"], "customer_id": customer,
             "api_url": api_url, "command_keys": answer.get("command_keys") or []}
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
    try:
        projects = [managed_services.backend_name(config.project_name())]
    except config.ConfigError:
        projects = []
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
                if e.status and e.status < 500:
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


def register_in_browser(api_url: str) -> int:
    folder = autoidm.folder()
    pending = folder / DEVICE_KEY
    with config.locked(folder):
        try:
            key = autoidm.load_key(pending.read_bytes())
        except (OSError, ValueError, autoidm.AutoidmError):
            key = Ed25519PrivateKey.generate()
            write_key(pending, key)
    body = json.dumps(device_start_body(key)).encode()
    try:
        codes = autoidm.answered(autoidm.send(f"{api_url}/v1/device/start", body, {}),
                                 "/v1/device/start", "user_code", "device_code", "url")
        autoidm.check_url(codes["url"], "/v1/device/start")
    except autoidm.AutoidmError as e:
        console.error(console.escape(str(e)))
        return 1
    console.say("To connect this computer to AutoIDM, open this page and sign in:")
    console.command(codes["url"])
    console.field("Check that the page shows this code", codes["user_code"])
    webbrowser.open(codes["url"])
    answer = wait_for_decision(api_url, codes)
    if answer is None:
        return 1
    try:
        autoidm.answered(answer, "/v1/device/poll", "installation_id", "customer_id")
    except autoidm.AutoidmError as e:
        console.error(console.escape(str(e)))
        return 1
    if not managed_services.CUSTOMER.fullmatch(answer["customer_id"]):
        console.error(f"AutoIDM answered with the customer ID {console.value(answer['customer_id'])}, "
                      "which is not one.")
        return 1
    with config.locked(folder):
        code = save_registration(answer, key, api_url, pending)
    if code != 0:
        return code
    found = autoidm.machine_registration(answer["customer_id"])
    try:
        autoidm.post(found, "/v1/device/confirm", {"version": 1, "user_code": codes["user_code"]})
    except autoidm.AutoidmError as e:
        console.warn(f"The portal page may not show success: {console.escape(str(e))}")
    if answer.get("email"):
        console.field("Signed in as", str(answer["email"]))
    return make_grants(answer.get("grants") or [], found)


def make_grants(chosen: list[str], found: autoidm.Registration) -> int:
    """Make each grant the person chose in the browser. With none chosen, ask a person at a
    terminal about each place of this project."""
    places = list(chosen)
    if not places and can_prompt(None):
        for place in grants.places_of_project():
            try:
                if console.confirm(f"Let AutoIDM deploy and run pdt jobs in {place}?"):
                    places.append(place)
            except EOFError:
                break
    code = 0
    for place in places:
        code = grants.add(place, False, False, found) or code
    return code


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
