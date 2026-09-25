"""Fill the winget/ manifest templates for one release.

    render_winget_manifests.py --version 0.1.3 --sha256 <zip hash> --out DIR
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "winget"


def render(version: str, sha256: str, out: Path, date: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    values = {"{version}": version, "{sha256}": sha256.upper(), "{date}": date}
    written = []
    for template in sorted(TEMPLATES.glob("*.yaml")):
        text = template.read_text()
        for placeholder, value in values.items():
            text = text.replace(placeholder, value)
        target = out / template.name
        target.write_text(text)
        written.append(target)
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    args = parser.parse_args()
    render(args.version, args.sha256, args.out, args.date)


if __name__ == "__main__":
    main()
