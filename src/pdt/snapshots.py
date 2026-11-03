"""Project snapshots: the file list, its ID, the zip, and the one safe way to unpack a zip.

A snapshot leaves out each deploy_common.BUILD_EXCLUDES pattern, each link, each file larger
than MAX_FILE_BYTES, and each folder that holds a run's output or storage. The `files` list is
sorted by path, with `/` as the separator, and its canonical JSON gives the ID, so the same
files always give the same ID on every computer.

A manifest and a zip come from the network. `check_manifest` refuses a path that could leave
the folder or name a file a snapshot never holds, and `unpack` writes only into a new, empty
folder, only the paths the manifest lists, after it checks each file's size and hash.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from pdt.deploy_common import BUILD_EXCLUDES
from pdt.utils import autoidm

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024
MAX_BODY_BYTES = 256 * 1024
MAX_NOTE_CHARACTERS = 256
SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_PATH_CHARACTERS = 1024
# The lock files of config.locked, and the state folder a run's storage uses.
SNAPSHOT_EXCLUDES = (*BUILD_EXCLUDES, "*.yml.lock", ".pdt-state", ".pdt-project-name")
WINDOWS_DEVICES = {"con", "prn", "aux", "nul",
                   *(f"com{number}" for number in range(10)),
                   *(f"lpt{number}" for number in range(10))}
# A zip entry's date goes into its bytes, so one fixed date gives one zip for one list of files.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
DOWNLOAD_TIMEOUT_SECONDS = 60
READ_BYTES = 1024 * 1024


class SnapshotError(RuntimeError):
    pass


def long_path(path: Path) -> str:
    """`path` in the form Windows opens at any length, because a project's files can pass the
    260 character limit under the service's folders."""
    if os.name != "nt":
        return str(path)
    full = os.path.abspath(path)
    return full if full.startswith("\\\\") else "\\\\?\\" + full


def excluded(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), pattern.lower()) for pattern in SNAPSHOT_EXCLUDES)


def collect(root: Path) -> tuple[list[dict], list[str]]:
    """The sorted `files` list of the folder `root`, and each file it left out for its size."""
    files, large = [], []
    start = long_path(root)
    for folder, dirs, names in os.walk(start, followlinks=False):
        dirs[:] = [name for name in dirs if not excluded(name)
                   and not (Path(folder, name).is_symlink() or Path(folder, name).is_junction())]
        for name in names:
            path = Path(folder, name)
            if excluded(name) or path.is_symlink() or not path.is_file():
                continue
            relative = Path(os.path.relpath(path, start)).as_posix()
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                large.append(relative)
                continue
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            files.append({"path": relative, "sha256": digest, "size": size})
    files.sort(key=lambda entry: entry["path"])
    return files, sorted(large)


def snapshot_id(files: list[dict]) -> str:
    canonical = json.dumps(files, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_zip(root: Path, files: list[dict], target: Path) -> None:
    """Zip `files` from `root`, and stop when a file changed after `collect` read it."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zipped:
        for entry in files:
            data = Path(long_path(root / entry["path"])).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise SnapshotError(f"{entry['path']} changed while pdt read the project. "
                                    "Run the command again.")
            info = zipfile.ZipInfo(entry["path"], ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zipped.writestr(info, data)


def path_problem(path: object) -> str:
    """Why a manifest may not hold `path`, or ""."""
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
        if excluded(part):
            return "names a file that a snapshot never holds"
    return ""


def check_manifest(manifest: object, wanted: str | None) -> tuple[str, list[dict]]:
    """The snapshot ID and the `files` of a manifest from the network. It is the ID that
    `wanted` names, when one is named."""
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        raise SnapshotError("the snapshot manifest has no list of files")
    seen = set()
    for entry in files:
        if not (isinstance(entry, dict) and set(entry) == {"path", "sha256", "size"}):
            raise SnapshotError("a manifest entry does not hold path, sha256, and size")
        problem = path_problem(entry["path"])
        if problem:
            raise SnapshotError(f"the snapshot manifest path {entry['path']!r} {problem}")
        if not (isinstance(entry["sha256"], str) and SHA256.fullmatch(entry["sha256"])):
            raise SnapshotError(f"the manifest entry {entry['path']!r} has no SHA-256")
        size = entry["size"]
        if not (type(size) is int and 0 <= size <= MAX_FILE_BYTES):
            raise SnapshotError(f"the manifest entry {entry['path']!r} has no valid size")
        if entry["path"].casefold() in seen:
            raise SnapshotError(f"the snapshot manifest lists {entry['path']!r} twice")
        seen.add(entry["path"].casefold())
    found = snapshot_id(files)
    if wanted is not None and found != wanted:
        raise SnapshotError(f"the snapshot manifest has the ID {found}, not {wanted}")
    return found, files


def read_member(zipped: zipfile.ZipFile, info: zipfile.ZipInfo, entry: dict) -> bytes:
    """One zip member, after it passes the manifest's size and hash. It reads at most one byte
    past the size, whatever the zip's header says."""
    with zipped.open(info) as handle:
        data = handle.read(entry["size"] + 1)
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise SnapshotError(f"the snapshot file {entry['path']!r} does not match its manifest")
    return data


def unpack(archive: Path, files: list[dict], target: Path) -> None:
    """Write the files of `archive` into the new, empty folder `target`. It writes nothing
    unless the zip holds exactly the manifest's paths, as plain files."""
    if any(target.iterdir()):
        raise SnapshotError(f"{target} is not empty")
    wanted = {entry["path"]: entry for entry in files}
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or set(names) != set(wanted):
            raise SnapshotError("the snapshot zip does not hold exactly the manifest's files")
        for info in infos:
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or kind not in (0, stat.S_IFREG):
                raise SnapshotError(f"the snapshot zip entry {info.filename!r} is not a file")
        root = target.resolve()
        for info in infos:
            data = read_member(zipped, info, wanted[info.filename])
            path = root / info.filename
            if not path.resolve().is_relative_to(root):
                raise SnapshotError(f"the snapshot zip entry {info.filename!r} leaves the folder")
            Path(long_path(path.parent)).mkdir(parents=True, exist_ok=True)
            Path(long_path(path)).write_bytes(data)


def inside(root: Path, relative: str) -> Path:
    """`root / relative`, when no link in an existing folder or file leads out of `root`."""
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise SnapshotError(f"{relative} leads out of {root}")
    return path


def download(url: str, target: Path, limit: int) -> None:
    """Save a presigned GET to `target`, and stop past `limit` bytes."""
    request = urllib.request.Request(autoidm.check_url(url, "/v1/snapshots/get"))
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, \
                target.open("wb") as out:
            while chunk := response.read(READ_BYTES):
                total += len(chunk)
                if total > limit:
                    raise SnapshotError(f"the snapshot zip is larger than {limit} bytes")
                out.write(chunk)
    except (urllib.error.URLError, OSError) as e:
        raise SnapshotError(f"pdt could not download the snapshot: "
                            f"{getattr(e, 'reason', e)}") from None


def fetch(found: autoidm.Registration, project: str, wanted: str | None, target: Path) -> tuple:
    """Ask AutoIDM for a snapshot (the newest head when `wanted` is None), download its zip, and
    write its files into the new, empty folder `target`. Returns its ID and its `files`."""
    answer = autoidm.answered(
        autoidm.post(found, "/v1/snapshots/get",
                     {"version": 1, "project": project, "snapshot_id": wanted}),
        "/v1/snapshots/get", "url")
    identity, files = check_manifest(answer.get("manifest"), wanted)
    archive = target.with_name(target.name + ".zip")
    limit = sum(entry["size"] for entry in files) + 1024 * (len(files) + 1) * 2
    try:
        download(answer["url"], archive, limit)
        unpack(archive, files, target)
    except (zipfile.BadZipFile, zlib.error, EOFError):
        raise SnapshotError("the snapshot zip does not parse") from None
    finally:
        archive.unlink(missing_ok=True)
    return identity, files


def read_manifest(found: autoidm.Registration, project: str, wanted: str | None) -> tuple:
    """The ID and `files` of a snapshot, with no download of its zip."""
    answer = autoidm.post(found, "/v1/snapshots/get",
                          {"version": 1, "project": project, "snapshot_id": wanted})
    return check_manifest(answer.get("manifest"), wanted)
