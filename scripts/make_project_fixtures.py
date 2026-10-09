"""Write the project that each pdt release makes, for the migration tests.

    make_project_fixtures.py [RELEASE ...]

Each release makes its own project through uvx: `pdt init --yes`, then
`pdt new` for each example it ships. The projects land in
tests/fixtures/projects/<release>/ and are committed, so the tests need no
network. PyPI has no 0.1.5. Commit each project's .env with `git add -f`,
because the project's own .gitignore names it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

RELEASES = ("0.1.0", "0.1.1", "0.1.2", "0.1.3", "0.1.4", "0.1.6")
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "projects"
LIST_EXAMPLES = ("import pathlib, pdt; print(*sorted(p.name for p in "
                 "(pathlib.Path(pdt.__file__).parent / 'examples').iterdir() if p.is_dir()))")


def uvx(release: str, *args: str, cwd: Path) -> str:
    env = {key: value for key, value in os.environ.items() if key != "PDT_PROJECT"}
    return subprocess.run(["uvx", "--from", f"pdt-cli=={release}", *args], cwd=cwd, env=env,
                          check=True, capture_output=True, text=True).stdout


def make(release: str) -> None:
    target = FIXTURES / release
    shutil.rmtree(target, ignore_errors=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    uvx(release, "pdt", "init", str(target), "--yes", cwd=FIXTURES)
    for example in uvx(release, "python", "-c", LIST_EXAMPLES, cwd=FIXTURES).split():
        if not (target / example).exists():
            uvx(release, "pdt", "new", example, "--from", example, cwd=target)


def main() -> None:
    for release in sys.argv[1:] or RELEASES:
        make(release)


if __name__ == "__main__":
    main()
