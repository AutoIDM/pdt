"""`pdt register`: authenticate this computer for AutoIDM managed services with a one-time code.

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
and so each file made in it, to SYSTEM (the scheduled task) and
Administrators, so the command needs an administrator terminal.
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
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pdt import __version__, config, console, managed_services
from pdt.utils import autoidm

# Owner Administrators, and an access list that inherits nothing and gives full control of the
# folder and of each file and folder made in it to SYSTEM and Administrators alone.
FOLDER_SDDL = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
FOLDER_ENV = "PDT_REGISTRATIONS_FOLDER"
# .NET calls, not Get-Acl and Set-Acl: Windows PowerShell started under PowerShell 7 inherits its
# PSModulePath and cannot load the module that holds those two commands.
SET_FOLDER_SDDL = (f"$folder = [IO.DirectoryInfo]$env:{FOLDER_ENV}; "
                   "$acl = $folder.GetAccessControl(); "
                   f"$acl.SetSecurityDescriptorSddlForm('{FOLDER_SDDL}'); "
                   "$folder.SetAccessControl($acl); "
                   "$folder.GetAccessControl().GetSecurityDescriptorSddlForm('Owner, Access')")


def restrict_folder(folder: Path) -> None:
    """On Windows, replace the owner and the whole access list of `folder` with FOLDER_SDDL,
    so each file made in it can be opened only by SYSTEM and Administrators from the moment it
    exists. It reads the folder back and raises RuntimeError when it does not match."""
    if os.name != "nt":
        return
    found = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            SET_FOLDER_SDDL], env={**os.environ, FOLDER_ENV: str(folder)},
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           check=True).stdout.strip()
    if found != FOLDER_SDDL:
        raise RuntimeError(f"its owner and access list are {found or 'unknown'}, "
                           f"not {FOLDER_SDDL}")


def write_key(path: Path, key: Ed25519PrivateKey) -> None:
    """Write the key through a temporary file in the same folder, so `path` holds a whole key
    or nothing. The temporary file is readable by its owner alone on POSIX, and takes the
    access list of the folder (`restrict_folder`) on Windows."""
    config.write_text_atomically(path, autoidm.private_pem(key).decode(), newline="\n")


def register(code: str, api_url: str) -> int:
    folder = autoidm.folder()
    if os.name == "nt" and not ctypes.windll.shell32.IsUserAnAdmin():
        console.error(f"pdt register writes the key of this computer in {console.value(folder)}, "
                      "which only an administrator can do. Run it in a terminal opened with "
                      "Run as administrator.")
        return 1
    code = code.strip().upper()
    api_url = api_url.rstrip("/")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        restrict_folder(folder)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as e:
        console.error(f"pdt could not limit {console.value(folder)} to SYSTEM and "
                      f"Administrators, so it wrote no key: {console.escape(str(e))}")
        return 1
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
        customer = answer["customer_id"]
        info, key_path = autoidm.files(customer)
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
        else:
            write_key(key_path, key)
            config.write_text_atomically(info, json.dumps({
                "installation_id": answer["installation_id"], "customer_id": customer,
                "api_url": api_url}) + "\n")
        console.done(f"Registered this computer to AutoIDM for {console.value(customer)} as "
                     f"{console.value(answer['installation_id'])}.")
        console.field("Saved in", str(info))
        save_customer(customer)
        pending.unlink()
    return 0


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
