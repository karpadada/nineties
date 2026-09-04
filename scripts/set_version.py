from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (
    ROOT / ".codex-plugin" / "plugin.json",
    ROOT / ".claude-plugin" / "plugin.json",
)
FORMULA = ROOT / "Formula" / "nineties.rb"
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set the shared app, Homebrew, Codex, and Claude version."
    )
    parser.add_argument("version")
    arguments = parser.parse_args()
    if not SEMVER.fullmatch(arguments.version):
        parser.error("version must be semantic versioning, for example 0.2.0")

    for path in MANIFESTS:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = arguments.version
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    project_path = ROOT / "pyproject.toml"
    project = project_path.read_text(encoding="utf-8")
    project, count = re.subn(
        r'(?m)^(version = ")[^"]+("\s*)$',
        rf"\g<1>{arguments.version}\g<2>",
        project,
        count=1,
    )
    if count != 1:
        raise SystemExit("could not find the project version")
    project_path.write_text(project, encoding="utf-8")

    lock_path = ROOT / "uv.lock"
    lock = lock_path.read_text(encoding="utf-8")
    lock, count = re.subn(
        r'(?m)(^\[\[package\]\]\nname = "nineties-music-browser"\nversion = ")[^"]+',
        rf"\g<1>{arguments.version}",
        lock,
        count=1,
    )
    if count != 1:
        raise SystemExit("could not find the project version in uv.lock")
    lock_path.write_text(lock, encoding="utf-8")

    formula = FORMULA.read_text(encoding="utf-8")
    formula, count = re.subn(
        r'(?m)^(  url "https://github\.com/karpadada/nineties\.git", tag: "v)[^"]+("\s*)$',
        rf"\g<1>{arguments.version}\g<2>",
        formula,
        count=1,
    )
    if count != 1:
        raise SystemExit("could not find the release tag in the Homebrew formula")
    FORMULA.write_text(formula, encoding="utf-8")


if __name__ == "__main__":
    main()
