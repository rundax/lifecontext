"""Check the built sdist and wheel and print a Markdown summary.

Usage: python .github/scripts/check_dist.py DIST_DIR
"""

from __future__ import annotations

import email.parser
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
NAME, VERSION = PROJECT["name"], PROJECT["version"]
DIST_INFO = f"{NAME}-{VERSION}.dist-info/"
# Local state, plans and assistant files that must never ship.
PRIVATE_PARTS = {".dev", ".claude", ".planning", ".gsd", ".omc", "plan", "plans", ".env", ".venv"}
SKILL = "personal-journal-operations"


def main(dist: Path) -> int:
    wheels = sorted(dist.glob(f"{NAME}-{VERSION}-*.whl"))
    sdists = sorted(dist.glob(f"{NAME}-{VERSION}.tar.gz"))
    errors: list[str] = []
    if len(wheels) != 1 or len(sdists) != 1:
        print(f"expected one wheel and one sdist, found {wheels} and {sdists}")
        return 1

    with zipfile.ZipFile(wheels[0]) as wheel:
        members = wheel.namelist()
        metadata = email.parser.Parser().parsestr(wheel.read(DIST_INFO + "METADATA").decode())
        entry_points = wheel.read(DIST_INFO + "entry_points.txt").decode()
    outside = [m for m in members if not m.startswith((f"{NAME}/", DIST_INFO))]
    if outside:
        errors.append(f"wheel has files outside the package: {outside}")
    for required in ("LICENSE", "NOTICE"):
        if DIST_INFO + "licenses/" + required not in members:
            errors.append(f"wheel lacks {DIST_INFO}licenses/{required}")
    if metadata.get("License-Expression") != "BUSL-1.1":
        errors.append(f"License-Expression is {metadata.get('License-Expression')!r}")
    if sorted(metadata.get_all("License-File", [])) != ["LICENSE", "NOTICE"]:
        errors.append(f"License-File entries are {metadata.get_all('License-File')}")
    for command in ("lifecontext = lifecontext.cli:main", "life-context = lifecontext.cli:main"):
        if command not in entry_points:
            errors.append(f"missing console script: {command}")
    skill = ROOT / "src" / NAME / "agent_skill" / SKILL
    expected_skill = sorted(
        f"{NAME}/agent_skill/{SKILL}/{path.relative_to(skill).as_posix()}" for path in skill.rglob("*.md")
    )
    missing_skill = [member for member in expected_skill if member not in members]
    if not expected_skill or missing_skill:
        errors.append(f"wheel lacks agent skill files: {missing_skill or 'all'}")

    with tarfile.open(sdists[0]) as sdist:
        sdist_members = sdist.getnames()
    prefix = f"{NAME}-{VERSION}/"
    for required in ("LICENSE", "NOTICE", "pyproject.toml", f"src/{NAME}/agent_skill/{SKILL}/SKILL.md"):
        if prefix + required not in sdist_members:
            errors.append(f"sdist lacks {required}")
    private = [
        m for m in sdist_members
        if any(part in PRIVATE_PARTS for part in m.removeprefix(prefix).split("/"))
    ] + [m for m in members if any(part in PRIVATE_PARTS for part in m.split("/"))]
    if private:
        errors.append(f"distribution contains private paths: {private}")

    modules = sorted(m for m in members if m.startswith(f"{NAME}/") and m.endswith(".py"))
    print("## Distribution contents")
    print()
    print("| File | Size | Entries |")
    print("|---|---|---|")
    for path, count in ((wheels[0], len(members)), (sdists[0], len(sdist_members))):
        print(f"| `{path.name}` | {path.stat().st_size:,} bytes | {count} |")
    print()
    print(f"- Wheel Python modules: {len(modules)}")
    print(f"- Agent skill files: {len(expected_skill)}")
    print(f"- Metadata: `License-Expression: {metadata.get('License-Expression')}`, "
          f"`License-File: {', '.join(metadata.get_all('License-File', []))}`")
    print(f"- Requires-Python: `{metadata.get('Requires-Python')}`, "
          f"{len(metadata.get_all('Requires-Dist', []))} pinned dependencies")
    print()
    if errors:
        print("### Problems")
        for error in errors:
            print(f"- {error}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
