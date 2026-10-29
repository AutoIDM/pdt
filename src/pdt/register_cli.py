"""`pdt register`: connect this computer to AutoIDM with a one-time enrollment code.

It makes the installation key and writes installation.key before it sends the
code, so a retry after a lost answer sends the same public key and AutoIDM
answers with the same installation. On Windows the folder is %ProgramData%\\pdt,
and only SYSTEM (the scheduled task) and Administrators can read the key, so the
command needs an administrator terminal. A computer that already holds
installation.json is registered; the command stops and changes nothing.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
from base64 import urlsafe_b64encode
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat)

from pdt import __version__, console
from pdt.utils import autoidm
from pdt.utils.env_secret import private_file

# The well-known SIDs of SYSTEM and of the Administrators group.
KEY_READERS = ("*S-1-5-18:F", "*S-1-5-32-544:F")


def write_key(path: Path, key: Ed25519PrivateKey) -> None:
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    private_file(path)
    path.write_bytes(pem)
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", *KEY_READERS],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)


def register(code: str, api_url: str) -> int:
    folder = autoidm.folder()
    info, key_path = folder / autoidm.INFO_FILE, folder / autoidm.KEY_FILE
    if info.is_file():
        found = json.loads(info.read_text(encoding="utf-8"))
        console.error(f"This computer is already registered to AutoIDM as "
                      f"{console.value(found.get('installation_id'))} in "
                      f"{console.value(info)}. To register it again, delete "
                      f"{console.value(folder)} first.")
        return 1
    if os.name == "nt" and not ctypes.windll.shell32.IsUserAnAdmin():
        console.error(f"pdt register writes the key of this computer in {console.value(folder)}, "
                      "which only an administrator can do. Run it in a terminal opened with "
                      "Run as administrator.")
        return 1
    folder.mkdir(parents=True, exist_ok=True)
    if key_path.is_file():
        key = autoidm.load_key(key_path.read_bytes())
    else:
        key = Ed25519PrivateKey.generate()
        write_key(key_path, key)
    public_key = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    code = code.strip().upper()
    api_url = api_url.rstrip("/")
    try:
        answer = autoidm.register(api_url, {
            "code": code, "public_key": urlsafe_b64encode(public_key).decode().rstrip("="),
            "hostname": socket.gethostname(), "pdt_version": __version__})
    except autoidm.AutoidmError as e:
        if e.code == "unknown_code":
            console.error(f"AutoIDM does not know the code {console.value(code)}, or it expired. "
                          "Ask AutoIDM for a new code.")
        elif e.code == "code_used":
            console.error(f"Another computer already used the code {console.value(code)}. "
                          "Ask AutoIDM for a new code.")
        else:
            console.error(console.escape(str(e)))
        return 1
    info.write_text(json.dumps({"installation_id": answer["installation_id"], "api_url": api_url})
                    + "\n", encoding="utf-8")
    console.done(f"Registered this computer to AutoIDM as "
                 f"{console.value(answer['installation_id'])}.")
    console.field("Saved in", str(folder))
    console.next_steps([("pdt.yml", f"add {console.value('- type: autoidm')} with "
                                    f"{console.value('events: [all]')} under "
                                    f"{console.value('alerts:')} to send the events of a project "
                                    "to AutoIDM")])
    return 0
