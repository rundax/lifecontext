"""`lifecontext skills`: links into temporary agent homes, backups, drift and removal."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from local_paths import scratch_root

from lifecontext.skills import (
    SKILL_NAME,
    check,
    install,
    remove,
    render,
    select_agents,
    skill_source,
)

V1_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _home():
    with tempfile.TemporaryDirectory(prefix="lifecontext-skills-test.", dir=scratch_root()) as temporary:
        yield Path(temporary).resolve(strict=True)


class SkillsTests(unittest.TestCase):
    def test_skill_ships_in_the_package_and_the_repository_path_points_at_it(self) -> None:
        source = skill_source()
        self.assertTrue((source / "SKILL.md").is_file())
        self.assertTrue((source / "references" / "document-wiki-workflows.md").is_file())
        repository_path = V1_ROOT / "skills" / SKILL_NAME
        self.assertTrue(repository_path.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(repository_path)))
        self.assertEqual(repository_path.resolve(), source)

    def test_claude_plugin_marketplace_exposes_the_packaged_skill(self) -> None:
        import tomllib

        marketplace = json.loads((V1_ROOT / ".claude-plugin" / "marketplace.json").read_text())
        (entry,) = marketplace["plugins"]
        plugin_root = (V1_ROOT / entry["source"]).resolve()
        self.assertEqual(plugin_root, skill_source().parent)
        manifest = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text())
        project = tomllib.loads((V1_ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual((manifest["name"], entry["name"]), ("lifecontext", "lifecontext"))
        self.assertEqual(manifest["version"], project["version"])
        self.assertEqual(manifest["license"], project["license"])
        self.assertTrue((plugin_root / manifest["skills"] / SKILL_NAME / "SKILL.md").is_file())

    def test_install_backs_up_conflicts_relinks_stale_links_and_is_idempotent(self) -> None:
        with _home() as home:
            environ = {"XDG_DATA_HOME": str(home / "data")}
            copy = home / ".codex" / "skills" / SKILL_NAME
            copy.mkdir(parents=True)
            (copy / "SKILL.md").write_text("an old copy")
            foreign = home / ".agents" / "skills" / SKILL_NAME
            foreign.parent.mkdir(parents=True)
            foreign.symlink_to(home / "somewhere-else")
            stale = home / ".hermes" / "skills" / "productivity" / SKILL_NAME
            stale.parent.mkdir(parents=True)
            stale.symlink_to("/old/venv/lib/python3.12/site-packages/lifecontext/agent_skill/" + SKILL_NAME)

            planned = install(home, select_agents(home, "all"), dry_run=True, environ=environ)
            self.assertEqual([item["action"] for item in planned["agents"]],
                             ["link", "backup_and_link", "backup_and_link", "relink"])
            self.assertFalse((home / ".claude").exists())
            self.assertTrue(copy.is_dir() and not copy.is_symlink())

            report = install(home, select_agents(home, "all"), environ=environ)
            for item in report["agents"]:
                self.assertEqual(os.path.realpath(item["link"]), str(skill_source()))
            backups = sorted((home / "data" / "life-context" / "skill-backups").iterdir())
            self.assertEqual([path.name.split("-")[0] for path in backups], ["codex", "gemini"])
            self.assertTrue(any((path / "SKILL.md").read_text() == "an old copy" for path in backups if path.is_dir()))
            self.assertTrue(check(home, select_agents(home, "all"))["ok"])
            again = install(home, select_agents(home, "all"), environ=environ)
            self.assertEqual({item["action"] for item in again["agents"]}, {"none"})
            self.assertIn("ok", render(again))

    def test_check_reports_drift_and_remove_deletes_only_our_links(self) -> None:
        with _home() as home:
            install(home, ["claude", "codex"], environ={})
            foreign = home / ".agents" / "skills" / SKILL_NAME
            foreign.parent.mkdir(parents=True)
            foreign.symlink_to(home)
            own_copy = home / ".hermes" / "skills" / "productivity" / SKILL_NAME
            own_copy.mkdir(parents=True)
            report = check(home, select_agents(home, "all"))
            self.assertFalse(report["ok"])
            self.assertEqual([item["state"] for item in report["agents"]],
                             ["linked", "linked", "foreign_link", "conflict"])
            self.assertIn("DRIFT", render(report))

            dry = remove(home, select_agents(home, "all"), dry_run=True)
            self.assertTrue((home / ".claude" / "skills" / SKILL_NAME).is_symlink())
            self.assertEqual([item["action"] for item in dry["agents"]], ["remove", "remove", "none", "none"])
            remove(home, select_agents(home, "all"))
            self.assertFalse(os.path.lexists(home / ".claude" / "skills" / SKILL_NAME))
            self.assertFalse(os.path.lexists(home / ".codex" / "skills" / SKILL_NAME))
            self.assertTrue(foreign.is_symlink())
            self.assertTrue(own_copy.is_dir())

    def test_default_selection_is_the_installed_agents(self) -> None:
        with _home() as home:
            self.assertEqual(select_agents(home, None), [])
            self.assertIn("No agent found", render(install(home, [], environ={})))
            (home / ".claude").mkdir()
            (home / ".gemini").mkdir()
            self.assertEqual(select_agents(home, None), ["claude", "gemini"])
            self.assertEqual(select_agents(home, "codex"), ["codex"])

    def test_command_dry_run_prints_the_plan_and_changes_nothing(self) -> None:
        with _home() as home:
            env = {"HOME": str(home), "PATH": os.defpath, "PYTHONPATH": str(V1_ROOT / "src")}
            result = subprocess.run(
                [sys.executable, "-m", "lifecontext.cli", "skills", "install", "--agent", "all", "--dry-run", "--json"],
                env=env, cwd=home, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual([item["agent"] for item in report["agents"]], ["claude", "codex", "gemini", "hermes"])
            self.assertEqual(list(home.iterdir()), [])
            checked = subprocess.run(
                [sys.executable, "-m", "lifecontext.cli", "skills", "check", "--agent", "claude"],
                env=env, cwd=home, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(checked.returncode, 1)
            self.assertIn("DRIFT", checked.stdout)


if __name__ == "__main__":
    unittest.main()
