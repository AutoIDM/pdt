"""Compare the tools pdt downloads at a pinned version with their newest release.

The scheduled ``security-scans`` workflow runs this. For each entry in TOOLS
it reads the version constant from the source file without importing it, and
reads the newest release from the tool's own source. A newer release prints a
``::warning::`` annotation. A known vulnerability of the pinned version in the
OSV database prints an ``::error::`` annotation and makes the exit status 1.

OSV has no package for the Google Cloud CLI, so that tool gets only the
release check. PowerShell is looked up as the NuGet package
Microsoft.PowerShell.SDK, which has the same version numbers as pwsh.

Stdlib only, so CI runs it with no extra install.
"""

from __future__ import annotations

import ast
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TIMEOUT = 60
OSV_QUERY = "https://api.osv.dev/v1/query"


def read_url(url: str, data: dict | None = None) -> urllib.request.addinfourl:
    body = None if data is None else json.dumps(data).encode()
    return urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=TIMEOUT)


def read_json(url: str, data: dict | None = None) -> dict:
    with read_url(url, data) as response:
        return json.load(response)


def gcloud_latest() -> str:
    return read_json("https://dl.google.com/dl/cloudsdk/channels/rapid/components-2.json")["version"]


def pwsh_latest() -> str:
    # github.com answers with a redirect to the tag, so this needs no API token.
    with read_url("https://github.com/PowerShell/PowerShell/releases/latest") as response:
        return response.geturl().rsplit("/", 1)[-1].removeprefix("v")


def npm_latest(package: str):
    return lambda: read_json(f"https://registry.npmjs.org/{package}")["dist-tags"]["latest"]


# (name, source file, constant, newest release, OSV ecosystem and package or None)
TOOLS = [
    ("Google Cloud CLI", "src/pdt/gcloud_sdk.py", "VERSION", gcloud_latest, None),
    ("PowerShell", "src/pdt/pwsh.py", "VERSION", pwsh_latest,
     ("NuGet", "Microsoft.PowerShell.SDK")),
    ("DuckDB-WASM", "src/pdt/duckdb_wasm.py", "VERSION", npm_latest("@duckdb/duckdb-wasm"),
     ("npm", "@duckdb/duckdb-wasm")),
    ("Apache Arrow", "src/pdt/duckdb_wasm.py", "ARROW_VERSION", npm_latest("apache-arrow"),
     ("npm", "apache-arrow")),
]


def pinned_version(path: Path, name: str) -> str:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise LookupError(f"{path} has no {name} = ...")


def vulnerabilities(ecosystem: str, package: str, version: str) -> list[tuple[str, list[str]]]:
    """Each OSV advisory for this version, with the versions that fix it."""
    found = read_json(OSV_QUERY, {"package": {"ecosystem": ecosystem, "name": package},
                                  "version": version})
    result = []
    for vuln in found.get("vulns", []):
        fixed = [event["fixed"]
                 for affected in vuln.get("affected", [])
                 if affected.get("package", {}).get("name") == package
                 for r in affected.get("ranges", [])
                 for event in r.get("events", []) if "fixed" in event]
        result.append((vuln["id"], fixed))
    return result


def check(name: str, source: str, constant: str, latest, osv) -> bool:
    """Print the annotations for one tool. Return False on a known vulnerability."""
    pinned = pinned_version(REPO / source, constant)
    newest = latest()
    if newest == pinned:
        print(f"{name} {pinned}: newest release")
    else:
        print(f"::warning file={source}::{name} {newest} is the newest release; "
              f"{constant} pins {pinned}")
    if osv is None:
        return True
    found = vulnerabilities(*osv, pinned)
    for vuln_id, fixed in found:
        print(f"::error file={source}::{name} {pinned}: {vuln_id}, "
              f"fixed in {', '.join(fixed) or 'no release'}")
    return not found


def main() -> int:
    results = [check(*tool) for tool in TOOLS]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
