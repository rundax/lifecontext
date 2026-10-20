"""``lifecontext skills``: link the packaged agent skill into each agent's skills directory.

Every agent gets a symlink to the one copy installed with the package, so an
upgrade updates them all. A different entry at a link location is moved to a
backup first; ``remove`` deletes only links that point at a LifeContext skill.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SKILL_NAME = "personal-journal-operations"
# Agent: (skills directory under HOME, directories whose presence means it is installed).
AGENTS = {
    "claude": (".claude/skills", (".claude",)),
    "codex": (".codex/skills", (".codex",)),
    "gemini": (".agents/skills", (".gemini", ".agents")),
    "hermes": (".hermes/skills/productivity", (".hermes",)),
}


class SkillsError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def skill_source() -> Path:
    """The skill directory shipped inside the installed package."""
    return Path(__file__).resolve().parent / "agent_skill" / SKILL_NAME


def select_agents(home: Path, agent: str | None) -> list[str]:
    """``all``, one named agent, or (``None``) the agents installed in this HOME."""
    if agent == "all":
        return list(AGENTS)
    if agent is not None:
        return [agent]
    return [name for name, (_, markers) in AGENTS.items() if any((home / marker).is_dir() for marker in markers)]


def _state(link: Path, source: Path) -> str:
    if not os.path.lexists(link):
        return "missing"
    if not link.is_symlink():
        return "conflict"
    if os.path.realpath(link) == os.path.realpath(source):
        return "linked"
    # A link into another LifeContext installation (an older venv) is ours to replace.
    if Path(os.readlink(link)).parts[-2:] == ("agent_skill", SKILL_NAME):
        return "stale_link"
    return "foreign_link"


def _plan(home: Path, agents: Iterable[str], source: Path) -> list[dict[str, Any]]:
    return [{"agent": name, "link": str(home / AGENTS[name][0] / SKILL_NAME),
             "state": _state(home / AGENTS[name][0] / SKILL_NAME, source)} for name in agents]


def _backup_path(environ: Mapping[str, str], home: Path, agent: str) -> Path:
    data_home = Path(environ["XDG_DATA_HOME"]) if environ.get("XDG_DATA_HOME") else home / ".local" / "share"
    base = data_home / "life-context" / "skill-backups" / f"{agent}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    candidate, number = base, 1
    while os.path.lexists(candidate):
        number += 1
        candidate = base.with_name(f"{base.name}-{number}")
    return candidate


def install(home: Path, agents: list[str], *, dry_run: bool = False,
            environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    source = skill_source()
    if not (source / "SKILL.md").is_file():
        raise SkillsError("skill_missing")
    items = _plan(home, agents, source)
    for item in items:
        link = Path(item["link"])
        item["action"] = {"linked": "none", "missing": "link", "stale_link": "relink"}.get(item["state"], "backup_and_link")
        if item["action"] == "backup_and_link":
            item["backup"] = str(_backup_path(environ, home, item["agent"]))
        if dry_run or item["action"] == "none":
            continue
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            if item["action"] == "relink":
                link.unlink()
            elif item["action"] == "backup_and_link":
                backup = Path(item["backup"])
                backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.rename(link, backup)
            os.symlink(source, link)
        except OSError:
            raise SkillsError("link_failed") from None
    return {"command": "install", "source": str(source), "dry_run": dry_run, "agents": items}


def check(home: Path, agents: list[str]) -> dict[str, Any]:
    items = _plan(home, agents, skill_source())
    return {"command": "check", "source": str(skill_source()), "dry_run": False, "agents": items,
            "ok": all(item["state"] == "linked" for item in items)}


def remove(home: Path, agents: list[str], *, dry_run: bool = False) -> dict[str, Any]:
    items = _plan(home, agents, skill_source())
    for item in items:
        ours = item["state"] in {"linked", "stale_link"}
        item["action"] = "remove" if ours else "none"
        if ours and not dry_run:
            try:
                Path(item["link"]).unlink()
            except OSError:
                raise SkillsError("unlink_failed") from None
    return {"command": "remove", "source": str(skill_source()), "dry_run": dry_run, "agents": items}


def render(report: Mapping[str, Any]) -> str:
    if not report["agents"]:
        return ("No agent found in this HOME (~/.claude, ~/.codex, ~/.gemini or ~/.agents, ~/.hermes); "
                "name one with --agent claude|codex|gemini|hermes|all.")
    prefix = "would " if report["dry_run"] else ""
    lines = []
    for item in report["agents"]:
        action = item.get("action")
        if report["command"] == "check":
            word = "ok" if item["state"] == "linked" else f"DRIFT ({item['state']})"
        elif action == "none":
            word = "ok" if report["command"] == "install" else f"skip ({item['state']})"
        elif action == "backup_and_link":
            word = f"{prefix}move the existing entry to {item['backup']} and link"
        else:
            word = prefix + {"link": "link", "relink": "relink", "remove": "remove"}[action]
        lines.append(f"{item['agent']:<7} {word}: {item['link']}")
    lines.append(f"Skill: {report['source']}")
    return "\n".join(lines)
