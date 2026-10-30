"""`pdt register`: authenticate this computer for AutoIDM managed services with a one-time code.

The code names the customer, so pdt learns the customer only from the answer.
It makes the key and writes it as pending-<hash of the code>.key before it
sends the code, so a retry after a lost answer sends the same public key and
AutoIDM answers with the same installation. When the answer comes, the key
becomes <customer_id>.key next to <customer_id>.json. On Windows the folder is
%ProgramData%\\pdt\\registrations, and only SYSTEM (the scheduled task) and
Administrators can read a key, so the command needs an administrator terminal.
A computer that already holds a registration for the customer keeps it; the
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
from pdt.utils.env_secret import private_file

# The well-known SIDs of SYSTEM and of the Administrators group.
KEY_READERS = ("*S-1-5-18:F", "*S-1-5-32-544:F")


def write_key(path: Path, key: Ed25519PrivateKey) -> None:
    private_file(path)
    path.write_bytes(autoidm.private_pem(key))
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", *KEY_READERS],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)


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
    digest = hashlib.sha256(code.replace("-", "").encode()).hexdigest()
    pending = folder / f"pending-{digest[:16]}.key"
    if pending.is_file():
        key = autoidm.load_key(pending.read_bytes())
    else:
        key = Ed25519PrivateKey.generate()
        write_key(pending, key)
    try:
        answer = autoidm.register(api_url, {
            "code": code, "public_key": autoidm.public_key(key), "hostname": socket.gethostname(),
            "pdt_version": __version__})
    except autoidm.AutoidmError as e:
        if e.code in ("unknown_code", "code_used"):
            pending.unlink()
        if e.code == "unknown_code":
            console.error(f"AutoIDM does not know the code {console.value(code)}, or it expired. "
                          "Ask AutoIDM for a new code.")
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
        console.error(f"This computer is already registered to AutoIDM for "
                      f"{console.value(customer)} as {console.value(found.get('installation_id'))} "
                      f"in {console.value(info)}. To register it again, delete "
                      f"{console.value(info)} and {console.value(key_path)}, then run this "
                      "command again with the same code.")
        save_customer(customer)
        return 1
    pending.replace(key_path)
    info.write_text(json.dumps({"installation_id": answer["installation_id"],
                                "customer_id": customer, "api_url": api_url}) + "\n",
                    encoding="utf-8")
    console.done(f"Registered this computer to AutoIDM for {console.value(customer)} as "
                 f"{console.value(answer['installation_id'])}.")
    console.field("Saved in", str(info))
    save_customer(customer)
    return 0


def save_customer(customer: str) -> None:
    """Name the customer in the pdt.yml of the project this command runs in, so the project
    sends to AutoIDM with this registration."""
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
