"""Project snapshots: the file list, its ID, the zip, and the one safe way to unpack a zip.

In a Git clone, a snapshot holds only tracked files. Otherwise it leaves out each LEFT_OUT
pattern. In both cases it leaves out each link, each file larger than MAX_FILE_BYTES, and each
ALWAYS_OUT name. The `files` list is sorted by path, with `/` as the separator, and its canonical
JSON gives the ID, so the same files always give the same ID on every computer.

A hash follows one line-ending rule (`content_hash`), so a text file has one hash on Windows
and on Linux. `size` is the raw size.

A manifest and a zip come from the network. `check_manifest` refuses a path that could leave
the folder or name pdt's own state, and `unpack` writes only into a new, empty folder, only the
paths the manifest lists, after it checks each file's size and hash.

The uploader signs each publication with its installation key (`publication_bytes`), and the
computer that receives a snapshot checks the signature and the author (`author_problem`).
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import http.client
import json
import os
import re
import stat
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from pdt import config, managed_services
from pdt.deploy_common import BUILD_EXCLUDES
from pdt.utils import autoidm

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024
MAX_BODY_BYTES = 256 * 1024
MAX_NOTE_CHARACTERS = 256
SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_PATH_CHARACTERS = 1024
# pdt's own state, the lock files of config.locked, and a run's storage. Git may track none of
# these and still reach the snapshot, and no zip may write them on a computer.
ALWAYS_OUT = (".git", ".pdt", ".env", ".pdt-state", ".pdt-project-name", "*.yml.lock")
LEFT_OUT = (*BUILD_EXCLUDES, *ALWAYS_OUT, "*.env", "*.csv", "*.tsv", "*.xlsx", "*.ppk",
            "known_hosts", ".secrets*", "artifacts")
WINDOWS_DEVICES = {"con", "prn", "aux", "nul",
                   *(f"com{number}" for number in range(10)),
                   *(f"lpt{number}" for number in range(10))}
# A zip entry's date goes into its bytes, so one fixed date gives one zip for one list of files.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_TRIES = 5
READ_BYTES = 1024 * 1024
TEXT_CHECK_BYTES = 8192
GIT_TIMEOUT_SECONDS = 60


class SnapshotError(RuntimeError):
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


def content_hash(data: bytes) -> str:
    """The SHA-256 of a file. A file with no NUL byte in its first TEXT_CHECK_BYTES is text, and
    each CR LF counts as LF."""
    if b"\0" not in data[:TEXT_CHECK_BYTES]:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


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
        raise SnapshotError(f"{root} is a Git clone, and pdt could not run git: {e}") from None
    if proc.returncode != 0:
        raise SnapshotError(f"git could not list the files of {root}: "
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
    return {"path": relative, "sha256": content_hash(path.read_bytes()), "size": size}, ""


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
                left_out[relative] = "a name that a snapshot leaves out"
            elif is_link(Path(folder, name)):
                left_out[relative] = "a link"
            else:
                kept.append(name)
        dirs[:] = kept
        for name in names:
            relative = (relative_folder / name).as_posix()
            if matches(name, LEFT_OUT):
                left_out[relative] = "a name that a snapshot leaves out"
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


def snapshot_id(files: list[dict]) -> str:
    canonical = json.dumps(files, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def publication_bytes(customer: str, project: str, identity: str, purpose: str, ref: str | None,
                      label: str | None, parent: str | None) -> bytes:
    return json.dumps({"customer_id": customer, "label": label, "parent": parent,
                       "project": project, "purpose": purpose, "ref": ref,
                       "snapshot_id": identity}, separators=(",", ":"), sort_keys=True).encode()


def sign_publication(key: Ed25519PrivateKey, *fields) -> str:
    raw = key.sign(publication_bytes(*fields))
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def from_base64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def publication_valid(publication: object, customer: str, project: str, identity: str) -> bool:
    """Whether the signature of `publication` is valid for this snapshot of this project."""
    if not isinstance(publication, dict):
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(from_base64url(publication["public_key"]))
        key.verify(from_base64url(publication["signature"]),
                   publication_bytes(customer, project, identity, publication["purpose"],
                                     publication.get("ref"), publication.get("label"),
                                     publication.get("parent")))
    except (KeyError, TypeError, ValueError, binascii.Error, InvalidSignature):
        return False
    return True


def author_problem(publications: object, customer: str, project: str, identity: str,
                   allowed: set[str] | None) -> str:
    """Why this snapshot has no accepted author, or "". With `allowed` None, as on a cloud
    worker, one valid signature is enough."""
    found = publications if isinstance(publications, list) else []
    valid = [item for item in found if publication_valid(item, customer, project, identity)]
    if not valid:
        return f"Snapshot {identity} of {project} has no valid signature."
    if allowed is None or any(item["public_key"] in allowed for item in valid):
        return ""
    return f"Snapshot {identity} of {project} has no author that this computer accepts."


def build_zip(root: Path, files: list[dict], target: Path) -> None:
    """Zip `files` from `root`, and stop when a file changed after `collect` read it."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zipped:
        for entry in files:
            data = Path(long_path(root / entry["path"])).read_bytes()
            if content_hash(data) != entry["sha256"]:
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
        if matches(part, ALWAYS_OUT):
            return "names pdt's own state"
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
    if len(data) != entry["size"] or content_hash(data) != entry["sha256"]:
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


def download_part(url: str, target: Path, limit: int) -> None:
    """Add to `target` from a presigned GET, from the byte it holds. Raises OSError for a
    download that a new try can continue."""
    have = target.stat().st_size if target.exists() else 0
    request = urllib.request.Request(autoidm.check_url(url, "/v1/snapshots/get"),
                                     headers={"Range": f"bytes={have}-"} if have else {})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            # A server that ignores Range answers 200 with the whole file.
            mode = "ab" if response.status == 206 else "wb"
            total = have if mode == "ab" else 0
            wanted = total + int(response.headers.get("Content-Length") or 0)
            with target.open(mode) as out:
                while chunk := response.read(READ_BYTES):
                    total += len(chunk)
                    if total > limit:
                        raise SnapshotError(f"the snapshot zip is larger than {limit} bytes")
                    out.write(chunk)
            if total < wanted:
                raise OSError(f"the connection closed after {total} of {wanted} bytes")
    except urllib.error.HTTPError as e:
        e.close()
        # A range from the end of the file means that the first try read it all.
        if e.code != 416:
            raise OSError(f"HTTP {e.code}") from None


def get(found: autoidm.Registration, project: str, wanted: str) -> dict:
    return autoidm.post(found, "/v1/snapshots/get",
                        {"version": 1, "project": project, "snapshot_id": wanted})


def download(found: autoidm.Registration, project: str, wanted: str, url: str, target: Path,
             limit: int) -> None:
    """Save a snapshot's zip to `target`. A failed download asks AutoIDM for a new URL and goes
    on from the last byte, up to DOWNLOAD_TRIES times, because a URL expires."""
    for attempt in range(DOWNLOAD_TRIES):
        try:
            download_part(url, target, limit)
            return
        except (OSError, http.client.HTTPException) as e:
            problem = getattr(e, "reason", e)
        if attempt + 1 < DOWNLOAD_TRIES:
            url = autoidm.answered(get(found, project, wanted), "/v1/snapshots/get", "url")["url"]
    raise SnapshotError(f"pdt could not download the snapshot in {DOWNLOAD_TRIES} tries: {problem}")


def customer_problem(root: Path, customer: str) -> str:
    """Why the project files in `root` must not serve `customer`, or "". A pdt.yml that names no
    customer is fine: pdt adds the customer after it copies the files."""
    path = root / config.PROJECT_FILE
    if not path.is_file():
        return ""
    try:
        found = config.load_yaml(path).get(managed_services.KEY)
    except config.ConfigError as e:
        return str(e)
    named = found.get("customer") if isinstance(found, dict) else None
    if named is not None and named != customer:
        return (f"the {config.PROJECT_FILE} of this snapshot names the customer {named}, "
                f"and this computer acts for {customer}")
    return ""


def fetch(found: autoidm.Registration, project: str, wanted: str, target: Path,
          customer: str, allowed: set[str] | None = None, check_author: bool = False) -> tuple:
    """Ask AutoIDM for a snapshot, download its zip, and write its files into the new, empty
    folder `target`. It stops when the snapshot names another customer, and with `check_author`,
    when it has no accepted author. Returns its ID, its `files`, and its publications."""
    answer = autoidm.answered(get(found, project, wanted), "/v1/snapshots/get", "url")
    identity, files = check_manifest(answer.get("manifest"), wanted)
    publications = answer.get("publications") or []
    if check_author:
        problem = author_problem(publications, customer, project, identity, allowed)
        if problem:
            raise SnapshotError(problem)
    archive = target.with_name(target.name + ".zip")
    limit = sum(entry["size"] for entry in files) + 1024 * (len(files) + 1) * 2
    try:
        download(found, project, wanted, answer["url"], archive, limit)
        unpack(archive, files, target)
    except (zipfile.BadZipFile, zlib.error, EOFError):
        raise SnapshotError("the snapshot zip does not parse") from None
    finally:
        archive.unlink(missing_ok=True)
    problem = customer_problem(target, customer)
    if problem:
        raise SnapshotError(problem)
    return identity, files, publications


def read_manifest(found: autoidm.Registration, project: str, wanted: str) -> tuple:
    """The ID and `files` of a snapshot, with no download of its zip."""
    return check_manifest(get(found, project, wanted).get("manifest"), wanted)


def heads(found: autoidm.Registration, project: str) -> list[dict]:
    """The newest snapshot of each computer that publishes this project, newest first."""
    found_heads = autoidm.post(found, "/v1/snapshots/heads",
                               {"version": 1, "project": project}).get("heads")
    return [head for head in found_heads or [] if isinstance(head, dict)
            and isinstance(head.get("snapshot_id"), str)
            and SNAPSHOT_ID.fullmatch(head["snapshot_id"])]


CHECK_SECONDS = 3
CHECK_LIMIT_SECONDS = 300


def finish(found: autoidm.Registration, project: str, identity: str) -> None:
    """Wait for AutoIDM to check the zip. A rejected snapshot is an AutoidmError."""
    deadline = time.monotonic() + CHECK_LIMIT_SECONDS
    body = {"version": 1, "project": project, "snapshot_id": identity}
    while autoidm.post(found, "/v1/snapshots/finish", body).get("state") == "checking":
        if time.monotonic() > deadline:
            raise SnapshotError(f"AutoIDM did not finish its check of snapshot {identity} in "
                                f"{CHECK_LIMIT_SECONDS} seconds.")
        time.sleep(CHECK_SECONDS)


def publish(found: autoidm.Registration, customer: str, project: str, root: Path,
            files: list[dict], purpose: str, ref: str | None, label: str | None,
            parent: str | None, work: Path) -> str:
    """Send a snapshot through `/v1/snapshots/start`, the presigned POST, and
    `/v1/snapshots/finish`. `work` is a folder for the zip. Returns the snapshot ID."""
    identity = snapshot_id(files)
    archive = work / f"{identity}.zip"
    build_zip(root, files, archive)
    size = archive.stat().st_size
    if size > MAX_SNAPSHOT_BYTES:
        raise SnapshotError(f"The zip of {project} is {size} bytes, over the limit of "
                            f"{MAX_SNAPSHOT_BYTES} bytes.")
    answer = autoidm.post(found, "/v1/snapshots/start", {
        "version": 1, "project": project, "snapshot_id": identity, "size": size,
        "purpose": purpose, "manifest": {"files": files},
        "publication": {"ref": ref, "label": label, "parent": parent,
                        "signature": sign_publication(found.key, customer, project, identity,
                                                      purpose, ref, label, parent)}})
    place = answer.get("upload")
    if place:
        autoidm.post_file(place, archive.name, archive, "/v1/snapshots/start")
    finish(found, project, identity)
    return identity
