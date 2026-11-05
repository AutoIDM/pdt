"""Who can change what the pdt service runs.

The service runs as SYSTEM or root, so a non-administrator who can write its program or a project
folder could run code as SYSTEM or root. `writable_by_others` names such an account. The install
and each start of the service refuse such a folder, on each operating system, and the service
checks each project again before its first command after a start. A person who runs
`python -m pdt.service_main` in a terminal with no administrator rights runs code as that person,
so that start needs no check.

`limit` sets the access list of a folder under the data folder. On Windows, `%ProgramData%` lets
each user create files, and a new folder inherits that, so each folder of pdt inherits nothing.
"""

from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import sys
from pathlib import Path

PATH_ENV = "PDT_ACCESS_PATH"
SDDL_ENV = "PDT_FOLDER_SDDL"
# Owner Administrators, and an access list that inherits nothing. SYSTEM and Administrators have
# full control, and Users can read, which a `pdt run` from a terminal needs for pwsh and logs.
READABLE_SDDL = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
# The same with no read for Users: settings, the journal, keys.
PRIVATE_SDDL = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
# SYSTEM, Administrators, and the owner of the system files. CREATOR OWNER rules apply only to
# the owner of a new file.
ALLOWED_SIDS = ("S-1-5-18", "S-1-5-32-544", "S-1-3-0",
                "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
# FILE_WRITE_DATA, FILE_APPEND_DATA, FILE_DELETE_CHILD, DELETE, WRITE_DAC, WRITE_OWNER.
WRITE_RIGHTS = 0xD0046
INHERIT_ONLY = 2
WALK_LIMIT = 50000
FIND_WRITER = ("$ErrorActionPreference = 'Stop'; "
               f"$allowed = @({', '.join(repr(sid) for sid in ALLOWED_SIDS)}); "
               "$sid = [Security.Principal.SecurityIdentifier]; "
               "$items = [Collections.Generic.Queue[IO.FileSystemInfo]]::new(); "
               f"$items.Enqueue([IO.DirectoryInfo]$env:{PATH_ENV}); $checked = 0; "
               f"while ($items.Count -gt 0 -and $checked -lt {WALK_LIMIT}) {{ "
               "$item = $items.Dequeue(); $checked++; $path = $item.FullName; "
               "$acl = $item.GetAccessControl(); "
               "$owner = $acl.GetOwner($sid).Value; "
               "if ($allowed -notcontains $owner) { "
               "$acl.GetOwner([Security.Principal.NTAccount]).Value + \"`t\" + $path; exit 0 }; "
               "foreach ($rule in $acl.GetAccessRules($true, $true, $sid)) { "
               "if ($rule.AccessControlType -ne 'Allow') { continue }; "
               f"if (([int]$rule.PropagationFlags -band {INHERIT_ONLY}) -ne 0) {{ continue }}; "
               "if ($allowed -contains $rule.IdentityReference.Value) { continue }; "
               f"if (([int]$rule.FileSystemRights -band {WRITE_RIGHTS}) -ne 0) {{ "
               "$rule.IdentityReference.Translate([Security.Principal.NTAccount]).Value "
               "+ \"`t\" + $path; exit 0 } }; "
               "if ($item -is [IO.DirectoryInfo] -and ($checked -eq 1 -or "
               "-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))) { "
               "foreach ($child in $item.GetFileSystemInfos()) { $items.Enqueue($child) } } }; "
               "if ($items.Count -gt 0) { \"`t\" + $items.Peek().FullName }")
# .NET calls only, not Get-Acl, Set-Acl, or New-Object: Windows PowerShell started under
# PowerShell 7 inherits its PSModulePath and cannot load the modules that hold those commands.
# It prints the access list as .NET holds it before it saves it, then as it reads it back, so
# both pass through the same .NET form (two-letter SIDs such as LA, merged entries).
SET_FOLDER_SDDL = ("$ErrorActionPreference = 'Stop'; "
                   "$folder = [IO.DirectoryInfo]$env:PDT_REGISTRATIONS_FOLDER; "
                   f"$sddl = $env:{SDDL_ENV}.Replace('{{user}}', "
                   "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value)"
                   ".Replace('(A;OICI;FR;;;S-1-5-18)', ''); "
                   "$acl = $folder.GetAccessControl(); "
                   "$acl.SetSecurityDescriptorSddlForm($sddl); "
                   "$acl.GetSecurityDescriptorSddlForm('Owner, Access'); "
                   "$folder.SetAccessControl($acl); "
                   "$folder.GetAccessControl().GetSecurityDescriptorSddlForm('Owner, Access')")
MACOS_ADMIN_GROUP = 80


class AccessError(RuntimeError):
    pass


def is_admin() -> bool:
    if os.name == "nt":
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


def run_powershell(script: str, env: dict[str, str]) -> str:
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          env={**os.environ, **env}, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True)
    if proc.returncode != 0:
        raise AccessError(f"PowerShell could not read the access list: {proc.stderr.strip()}")
    return proc.stdout


def restrict_folder(folder: Path, sddl: str) -> None:
    if os.name != "nt":
        return
    shown = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            SET_FOLDER_SDDL],
                           env={**os.environ, "PDT_REGISTRATIONS_FOLDER": str(folder),
                                SDDL_ENV: sddl},
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           check=True).stdout.split()
    if len(shown) != 2:
        raise RuntimeError(f"PowerShell printed {' '.join(shown) or 'nothing'}, not the access "
                           "list pdt set and the one it read back")
    if shown[0] != shown[1]:
        raise RuntimeError(f"its owner and access list are {shown[1]}, not {shown[0]}")


def limit(folder: Path, private: bool = True) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            restrict_folder(folder, PRIVATE_SDDL if private else READABLE_SDDL)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"PowerShell could not limit {folder}: {e.stderr}") from None
        return
    os.chmod(folder, 0o700 if private else 0o755)


def account_name(uid: int) -> str:
    import pwd
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def group_name(gid: int) -> str:
    import grp
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def posix_writer(path: Path) -> str | None:
    found = path.lstat()
    if found.st_uid != 0:
        return account_name(found.st_uid)
    if stat.S_ISLNK(found.st_mode):
        return None
    admin_groups = {0, MACOS_ADMIN_GROUP} if sys.platform == "darwin" else {0}
    sticky = bool(found.st_mode & stat.S_ISVTX)
    if found.st_mode & stat.S_IWGRP and found.st_gid not in admin_groups and not sticky:
        return f"group {group_name(found.st_gid)}"
    if found.st_mode & stat.S_IWOTH and not sticky:
        return "each user"
    return None


def posix_writable(folder: Path) -> tuple[str, Path] | None:
    checked = 0
    for parent in (*reversed(folder.parents), folder):
        account = posix_writer(parent)
        if account:
            return account, parent
    for parent, dirs, names in os.walk(folder):
        for name in [*dirs, *names]:
            if checked >= WALK_LIMIT:
                raise AccessError(f"{folder} holds more than {WALK_LIMIT} files and folders, so "
                                  "pdt could not check who can change each one.")
            path = Path(parent, name)
            account = posix_writer(path)
            if account:
                return account, path
            checked += 1
    return None


def windows_writable(folder: Path) -> tuple[str, Path] | None:
    """The account and the path of the first thing that a non-administrator can change: `folder`
    or each thing below it, because an item can hold a right of its own that it did not inherit.
    A folder above does not count, because Windows gives a right on a folder only to that
    folder's own list."""
    shown = run_powershell(FIND_WRITER, {PATH_ENV: str(folder)}).rstrip()
    if not shown:
        return None
    account, _, path = shown.partition("\t")
    if not account:
        raise AccessError(f"{folder} holds more than {WALK_LIMIT} files and folders, so pdt "
                          "could not check who can change each one.")
    return account, Path(path)


def writable_by_others(folder: Path) -> tuple[str, Path] | None:
    return windows_writable(folder) if os.name == "nt" else posix_writable(folder)


def repair_commands(folder: Path) -> list[tuple[str, str]]:
    if os.name == "nt":
        return [(f'icacls "{folder}" /setowner *S-1-5-32-544 /T',
                 "make Administrators the owner"),
                (f'icacls "{folder}" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F '
                 "*S-1-5-32-544:(OI)(CI)F /T",
                 "let only SYSTEM and Administrators change it")]
    return [(f'sudo chown -R root "{folder}"', "make root the owner"),
            (f'sudo chmod -R go-w "{folder}"', "let only root change it")]
