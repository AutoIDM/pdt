"""Project copies: the current zip of each unit of a project at AutoIDM.

A unit is `root` (the project files outside the app folders), `app=<name>` (one app folder, with
paths relative to it), or `mr=<iid>` (a whole project from a merge request). A copy has no
history and no per-file hash: `sha256` is the SHA-256 of the zip bytes.

In a Git clone, a copy holds only tracked files. Otherwise it leaves out each LEFT_OUT pattern.
In both cases it leaves out each link, each file larger than MAX_FILE_BYTES, and each ALWAYS_OUT
name. The zip has one fixed date, so the same files give the same zip.

A manifest and a zip come from the network. `unpack` checks the zip's SHA-256 and size, each path
(`path_problem`), each entry type, and each file size before it writes a file, and it writes only
into a new, empty folder.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import http.client
import io
import os
import re
import stat
import subprocess
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from pdt import config
from pdt.deploy_common import BUILD_EXCLUDES
from pdt.utils import autoidm

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_COPY_BYTES = 1024 * 1024 * 1024
MAX_PATH_CHARACTERS = 1024
ROOT = "root"
UNIT = re.compile(r"root|app=(?!\.{1,2}$)(?!-)[A-Za-z0-9._-]{1,128}|mr=[0-9]{1,9}")
SHA256 = re.compile(r"[0-9a-f]{64}")
# pdt's own state, the lock files of config.locked, and a run's storage. Git may track none of
# these and still reach the copy, and no zip may write them on a computer.
ALWAYS_OUT = (".git", ".pdt", ".env", ".pdt-state", ".pdt-project-name", "*.yml.lock")
LEFT_OUT = (*BUILD_EXCLUDES, *ALWAYS_OUT, "*.env", "*.csv", "*.tsv", "*.xlsx", "*.ppk",
            "known_hosts", ".secrets*", "artifacts")
WINDOWS_DEVICES = {"con", "prn", "aux", "nul",
                   *(f"com{number}" for number in range(10)),
                   *(f"lpt{number}" for number in range(10))}
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_TRIES = 3
READ_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 60


class CopyError(RuntimeError):
    pass


def long_path(path: Path) -> str:
    """`path` in the form Windows opens at any length, because a project's files can pass the
    260 character limit under the service's folders."""
    if os.name != "nt":
        return str(path)
    full = os.path.abspath(path)
    return full if full.startswith("\\\\") else "\\\\?\\" + full


def matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), pattern.lower()) for pattern in patterns)


def in_clone(root: Path) -> bool:
    return any((folder / ".git").exists() for folder in (root, *root.parents))


def git_paths(root: Path, *args: str) -> list[str]:
    """The paths `git ls-files` names, relative to `root`. A service account does not own a
    customer's clone, and Git refuses such a clone unless it is named as safe."""
    try:
        proc = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), "ls-files", "-z",
                               *args], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as e:
        raise CopyError(f"{root} is a Git clone, and pdt could not run git: {e}") from None
    if proc.returncode != 0:
        raise CopyError(f"git could not list the files of {root}: "
                        f"{proc.stderr.decode(errors='replace').strip()}")
    return [path.rstrip("/") for path in proc.stdout.decode().split("\0") if path]


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def entry_of(root: Path, relative: str) -> tuple[dict | None, str]:
    """One entry of a `files` list, or None and why it stays out."""
    path = Path(long_path(root / relative))
    if is_link(path):
        return None, "a link"
    if not path.is_file():
        return None, "not a file"
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        return None, "larger than the limit of a file"
    return {"path": relative, "size": size}, ""


def collect(root: Path) -> tuple[list[dict], dict[str, str]]:
    """The sorted `files` list of the folder `root`, and each path it left out, with the reason.
    A folder left out counts as one path."""
    if in_clone(root):
        return collect_tracked(root)
    files, left_out = [], {}
    start = long_path(root)
    for folder, dirs, names in os.walk(start, followlinks=False):
        relative_folder = Path(os.path.relpath(folder, start))
        kept = []
        for name in dirs:
            relative = (relative_folder / name).as_posix()
            if matches(name, LEFT_OUT):
                left_out[relative] = "a name that a copy leaves out"
            elif is_link(Path(folder, name)):
                left_out[relative] = "a link"
            else:
                kept.append(name)
        dirs[:] = kept
        for name in names:
            relative = (relative_folder / name).as_posix()
            if matches(name, LEFT_OUT):
                left_out[relative] = "a name that a copy leaves out"
                continue
            entry, why = entry_of(root, relative)
            if entry is None:
                left_out[relative] = why
            else:
                files.append(entry)
    files.sort(key=lambda entry: entry["path"])
    return files, dict(sorted(left_out.items()))


def collect_tracked(root: Path) -> tuple[list[dict], dict[str, str]]:
    files, left_out = [], {}
    for relative in git_paths(root):
        if any(matches(part, ALWAYS_OUT) for part in relative.split("/")):
            left_out[relative] = "pdt's own state"
            continue
        entry, why = entry_of(root, relative)
        if entry is None:
            left_out[relative] = why
        else:
            files.append(entry)
    for relative in git_paths(root, "--others", "--directory"):
        left_out[relative] = "not tracked by Git"
    files.sort(key=lambda entry: entry["path"])
    return files, dict(sorted(left_out.items()))


def app_names(root: Path) -> list[str]:
    try:
        folders = sorted(root.iterdir())
    except OSError:
        return []
    return [folder.name for folder in folders if folder.is_dir() and config.is_app(folder)]


def split_units(files: list[dict], apps: list[str]) -> dict[str, list[dict]]:
    """The files of each unit: each app folder is `app=<name>`, with paths relative to it, and
    the rest is `root`."""
    units: dict[str, list[dict]] = {ROOT: []}
    for app in apps:
        units[f"app={app}"] = []
    for entry in files:
        first, _, rest = entry["path"].partition("/")
        if rest and first in apps:
            units[f"app={first}"].append({**entry, "path": rest})
        else:
            units[ROOT].append(entry)
    return units


def unit_folder(root: Path, unit: str) -> Path:
    """The folder whose files a unit holds."""
    return root / unit.removeprefix("app=") if unit.startswith("app=") else root


def build_zip(root: Path, files: list[dict]) -> bytes:
    """The zip of `files` from `root`. It stops when a file changed after `collect` read it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for entry in files:
            data = Path(long_path(root / entry["path"])).read_bytes()
            if len(data) != entry["size"]:
                raise CopyError(f"{entry['path']} changed while pdt read the project. "
                                "Run the command again.")
            info = zipfile.ZipInfo(entry["path"], ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zipped.writestr(info, data)
    return buffer.getvalue()


def path_problem(path: object) -> str:
    """Why a copy may not hold `path`, or ""."""
    if not (isinstance(path, str) and 0 < len(path) <= MAX_PATH_CHARACTERS):
        return "is not a path of at most 1024 characters"
    if "\\" in path or ":" in path or any(ord(character) < 32 for character in path):
        return "holds a backslash, a colon, or a control character"
    parts = path.split("/")
    if PurePosixPath(path).is_absolute() or any(part in ("", ".", "..") for part in parts):
        return "leaves the folder"
    for part in parts:
        if part.endswith((".", " ")) or part.split(".")[0].lower() in WINDOWS_DEVICES:
            return "has a name Windows changes or keeps for a device"
        if matches(part, ALWAYS_OUT):
            return "names pdt's own state"
    return ""


def checked_files(entry: dict) -> list[dict]:
    """The `files` of a unit from the network, after each path and size passes."""
    files = entry.get("files") if isinstance(entry, dict) else None
    if not isinstance(files, list):
        raise CopyError("the copy has no list of files")
    seen = set()
    for item in files:
        if not (isinstance(item, dict) and set(item) >= {"path", "size"}):
            raise CopyError("a file of the copy has no path and size")
        problem = path_problem(item["path"])
        if problem:
            raise CopyError(f"the copy path {item['path']!r} {problem}")
        if not (type(item["size"]) is int and 0 <= item["size"] <= MAX_FILE_BYTES):
            raise CopyError(f"the copy file {item['path']!r} has no valid size")
        if item["path"].casefold() in seen:
            raise CopyError(f"the copy lists {item['path']!r} twice")
        seen.add(item["path"].casefold())
    return files


def unpack(data: bytes, entry: dict, target: Path) -> None:
    """Write the files of the zip `data` of one unit into the new, empty folder `target`. It
    writes nothing unless the zip has the unit's SHA-256 and holds exactly its files."""
    if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
        raise CopyError(f"the zip of {entry.get('unit')} does not have the SHA-256 of its copy")
    files = checked_files(entry)
    wanted = {item["path"]: item for item in files}
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise CopyError(f"{target} is not empty")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zipped:
            infos = zipped.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != set(wanted):
                raise CopyError("the copy zip does not hold exactly the files of its copy")
            for info in infos:
                kind = stat.S_IFMT(info.external_attr >> 16)
                if info.is_dir() or kind not in (0, stat.S_IFREG):
                    raise CopyError(f"the copy zip entry {info.filename!r} is not a file")
            root = target.resolve()
            for info in infos:
                with zipped.open(info) as handle:
                    content = handle.read(wanted[info.filename]["size"] + 1)
                if len(content) != wanted[info.filename]["size"]:
                    raise CopyError(f"the copy file {info.filename!r} does not have its size")
                path = root / info.filename
                if not path.resolve().is_relative_to(root):
                    raise CopyError(f"the copy zip entry {info.filename!r} leaves the folder")
                Path(long_path(path.parent)).mkdir(parents=True, exist_ok=True)
                Path(long_path(path)).write_bytes(content)
    except (zipfile.BadZipFile, zlib.error, EOFError):
        raise CopyError("the copy zip does not parse") from None


def inside(root: Path, relative: str) -> Path:
    """`root / relative`, when no link in an existing folder or file leads out of `root`."""
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise CopyError(f"{relative} leads out of {root}")
    return path


def download(entry: dict) -> bytes:
    """The zip of one unit, from the presigned GET of the answer."""
    limit = entry.get("size")
    if not (type(limit) is int and 0 <= limit <= MAX_COPY_BYTES):
        raise CopyError(f"the copy of {entry.get('unit')} has no valid size")
    url = autoidm.check_url(entry.get("url"), "/v1/copies/get")
    problem = None
    for _ in range(DOWNLOAD_TRIES):
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                data = response.read(limit + 1)
            if len(data) != limit:
                raise CopyError(f"the zip of {entry.get('unit')} is not {limit} bytes")
            return data
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            problem = getattr(e, "reason", e)
    raise CopyError(f"pdt could not download the copy in {DOWNLOAD_TRIES} tries: {problem}")


def fetch(entry: dict, target: Path) -> list[dict]:
    """Download one unit of an answer and write its files into the new folder `target`."""
    unpack(download(entry), entry, target)
    return entry["files"]


def get(found: autoidm.Registration, project: str, unit: str | None = None) -> list[dict]:
    """The current units of a project at AutoIDM: one unit, or `root` and each app."""
    body = {"version": 1, "project": project, **({"unit": unit} if unit else {})}
    units = autoidm.post(found, "/v1/copies/get", body).get("units")
    return [entry for entry in units or [] if isinstance(entry, dict)
            and isinstance(entry.get("unit"), str) and UNIT.fullmatch(entry["unit"])]


def put(upload: dict, data: bytes, digest: str) -> None:
    """Send the zip to the presigned PUT of /v1/copies/start."""
    headers = upload.get("headers") if isinstance(upload.get("headers"), dict) else {}
    request = urllib.request.Request(
        autoidm.check_url(upload.get("url"), "/v1/copies/start"), data=data, method="PUT",
        headers={"x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(digest)).decode(),
                 **headers})
    try:
        with urllib.request.urlopen(request, timeout=autoidm.UPLOAD_TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError as e:
        e.close()
        raise CopyError(f"The upload of the copy got HTTP {e.code}.") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise CopyError(f"The upload of the copy failed: {getattr(e, 'reason', e)}") from None


def send(found: autoidm.Registration, project: str, unit: str, folder: Path,
         files: list[dict]) -> str:
    """Send one unit through /v1/copies/start, the presigned PUT, and /v1/copies/finish.
    Returns the SHA-256 of the zip."""
    data = build_zip(folder, files)
    if len(data) > MAX_COPY_BYTES:
        raise CopyError(f"The zip of {unit} of {project} is {len(data)} bytes, over the limit of "
                        f"{MAX_COPY_BYTES} bytes.")
    digest = hashlib.sha256(data).hexdigest()
    answer = autoidm.post(found, "/v1/copies/start", {
        "version": 1, "project": project, "unit": unit, "sha256": digest, "size": len(data),
        "files": files})
    if answer.get("upload"):
        put(answer["upload"], data, digest)
    autoidm.post(found, "/v1/copies/finish",
                 {"version": 1, "project": project, "unit": unit, "sha256": digest})
    return digest
