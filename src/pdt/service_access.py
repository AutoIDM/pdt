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
# LOCAL SERVICE (LS), which runs a merge request run, can read the program, the same as Users.
READABLE_SDDL = ("O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
                 "(A;OICI;0x1200a9;;;LS)")
# The same with no read for Users: settings, the journal, keys.
PRIVATE_SDDL = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
# The work folder of a merge request run: LOCAL SERVICE can change it (FILE_GENERIC_READ, WRITE,
# EXECUTE, and DELETE), and no other account but SYSTEM and Administrators can open it.
WORK_SDDL = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1301bf;;;LS)"
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
LOGON32_LOGON_SERVICE = 5
LOGON32_PROVIDER_DEFAULT = 0
DISABLE_MAX_PRIVILEGE = 0x1
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
STARTF_USESTDHANDLES = 0x100
INFINITE = 0xFFFFFFFF
# CREATE_SUSPENDED, CREATE_NEW_PROCESS_GROUP, CREATE_UNICODE_ENVIRONMENT,
# EXTENDED_STARTUPINFO_PRESENT, and CREATE_NO_WINDOW.
LOCAL_SERVICE_FLAGS = 0x4 | 0x200 | 0x400 | 0x80000 | 0x08000000


class AccessError(RuntimeError):
    pass


def is_admin() -> bool:
    if os.name == "nt":
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


def is_system() -> bool:
    """Whether this process runs as SYSTEM, the one account that can log on as LOCAL SERVICE."""
    if os.name != "nt":
        return False
    found = subprocess.run(["whoami", "/user"], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, check=False).stdout
    return "S-1-5-18" in found.split()


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
                 "*S-1-5-32-544:(OI)(CI)F",
                 "let only SYSTEM and Administrators change it"),
                (f'icacls "{folder}\\*" /reset /T',
                 "give each file and folder in it the same access")]
    return [(f'sudo chown -R root "{folder}"', "make root the owner"),
            (f'sudo chmod -R go-w "{folder}"', "let only root change it")]


class LocalServiceChild:
    """A child that runs as LOCAL SERVICE, started suspended. It has the methods of a Popen that
    the service uses, and `_handle`, the process handle, as a Popen has on Windows."""

    def __init__(self, process: int, thread: int, pid: int) -> None:
        self._handle = process
        self.thread = thread
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None and windows_kernel().WaitForSingleObject(self._handle, 0) == 0:
            self.finish()
        return self.returncode

    def wait(self) -> int:
        if self.returncode is None:
            windows_kernel().WaitForSingleObject(self._handle, INFINITE)
            self.finish()
        return self.returncode

    def kill(self) -> None:
        windows_kernel().TerminateProcess(self._handle, 1)

    def finish(self) -> None:
        import ctypes
        from ctypes import wintypes
        kernel = windows_kernel()
        code = wintypes.DWORD()
        kernel.GetExitCodeProcess(self._handle, ctypes.byref(code))
        self.returncode = code.value
        kernel.CloseHandle(self.thread)
        kernel.CloseHandle(self._handle)


def windows_kernel():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.InitializeProcThreadAttributeList.argtypes = (
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t))
    kernel.UpdateProcThreadAttribute.argtypes = (
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p)
    kernel.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
    return kernel


def start_as_local_service(argv: list[str], cwd: Path, env: dict[str, str],
                           output: int) -> LocalServiceChild:
    """Start `argv` suspended as LOCAL SERVICE, with no privilege but the bypass of traverse
    checks (DISABLE_MAX_PRIVILEGE), its output and errors in the open file `output`, and no
    other handle of the service. The caller puts it in a job object and resumes it."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                    ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                    *((name, wintypes.DWORD) for name in
                      ("x", "y", "x_size", "y_size", "x_chars", "y_chars", "fill", "flags")),
                    ("show_window", wintypes.WORD), ("reserved2_size", wintypes.WORD),
                    ("reserved2", ctypes.c_void_p), ("stdin", wintypes.HANDLE),
                    ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE),
                    ("attributes", ctypes.c_void_p)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                    ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.LogonUserW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.CreateRestrictedToken.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE))
    advapi.CreateProcessAsUserW.argtypes = (
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(ProcessInformation))
    kernel = windows_kernel()

    def check(ok: int) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    logon, token = wintypes.HANDLE(), wintypes.HANDLE()
    check(advapi.LogonUserW("LocalService", "NT AUTHORITY", None, LOGON32_LOGON_SERVICE,
                            LOGON32_PROVIDER_DEFAULT, ctypes.byref(logon)))
    try:
        check(advapi.CreateRestrictedToken(logon, DISABLE_MAX_PRIVILEGE, 0, None, 0, None, 0,
                                           None, ctypes.byref(token)))
    finally:
        kernel.CloseHandle(logon)
    with open(os.devnull, "rb") as nothing:
        handles = (wintypes.HANDLE * 2)(msvcrt.get_osfhandle(nothing.fileno()),
                                        msvcrt.get_osfhandle(output))
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        size = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        check(kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)))
        try:
            check(kernel.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                                   handles, ctypes.sizeof(handles), None, None))
            startup = StartupInfo(cb=ctypes.sizeof(StartupInfo), flags=STARTF_USESTDHANDLES,
                                  desktop="", stdin=handles[0], stdout=handles[1],
                                  stderr=handles[1], attributes=ctypes.addressof(attributes))
            block = "".join(f"{name}={value}\0" for name, value in sorted(env.items())) + "\0"
            environment = ctypes.create_string_buffer(block.encode("utf-16-le"))
            info = ProcessInformation()
            check(advapi.CreateProcessAsUserW(
                token, argv[0], ctypes.create_unicode_buffer(subprocess.list2cmdline(argv)),
                None, None, True, LOCAL_SERVICE_FLAGS, environment, str(cwd),
                ctypes.byref(startup), ctypes.byref(info)))
        finally:
            kernel.DeleteProcThreadAttributeList(attributes)
            kernel.CloseHandle(token)
            os.set_handle_inheritable(handles[1], False)
    return LocalServiceChild(info.process, info.thread, info.pid)
