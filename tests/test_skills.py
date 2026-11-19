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
    INSTRUCTIONS,
    SKILL_NAME,
    check,
    install,
    instructions_block,
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


class InstructionsTests(unittest.TestCase):
    """`--instructions`: the marked block in CLAUDE.md and AGENTS.md, in a temporary HOME."""

    def setUp(self) -> None:
        manager = _home()
        self.home = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.environ = {"XDG_DATA_HOME": str(self.home / "data")}
        self.claude = self.home / ".claude" / "CLAUDE.md"
        self.codex = self.home / ".codex" / "AGENTS.md"

    def put(self, path: Path, text: str | bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())

    def run_install(self, agents=("claude", "codex"), **kwargs):
        return install(self.home, list(agents), environ=self.environ, instructions=True, **kwargs)

    def actions(self, report) -> dict[str, str]:
        return {item["agent"]: item["action"] for item in report["instructions"]}

    def test_the_block_names_the_commands_the_skill_documents_and_carries_its_hash(self) -> None:
        import hashlib
        begin, *content, end = instructions_block().split("\n")
        self.assertEqual(begin, f"<!-- lifecontext:instructions:begin sha256="
                                f"{hashlib.sha256(chr(10).join(content).encode()).hexdigest()} -->")
        self.assertEqual(end, "<!-- lifecontext:instructions:end -->")
        self.assertEqual("\n".join(content), INSTRUCTIONS)
        self.assertLessEqual(len(content), 5)
        recipes = (skill_source() / "SKILL.md").read_text()
        for command in ("lifecontext wiki list", "lifecontext wiki show", "lifecontext recall --query"):
            self.assertIn(command.replace("lifecontext ", '"$CLI" '), recipes)
            self.assertIn(command, INSTRUCTIONS)

    def test_no_block_without_the_flag(self) -> None:
        report = install(self.home, ["claude", "codex"], environ=self.environ)
        self.assertNotIn("instructions", report)
        self.assertFalse(self.claude.exists() or self.codex.exists())
        self.assertNotIn("instructions", render(report))

    def test_writes_into_a_fresh_file_then_appends_to_an_existing_one_and_is_idempotent(self) -> None:
        report = self.run_install(agents=("claude", "codex", "gemini"))
        self.assertEqual(self.actions(report), {"claude": "write", "codex": "write", "gemini": "none"})
        self.assertEqual(self.claude.read_text(), instructions_block() + "\n")
        self.assertEqual(self.claude.stat().st_mode & 0o777, 0o600)
        self.assertIn("gemini  instructions none (this agent has no instruction file", render(report))
        self.assertEqual(self.actions(self.run_install()), {"claude": "none", "codex": "none"})
        self.assertEqual(self.claude.read_text(), instructions_block() + "\n")

        self.claude.unlink()
        self.put(self.claude, "# My rules\nBe brief.\n")
        self.claude.chmod(0o640)
        self.run_install(agents=("claude",))
        self.assertEqual(self.claude.read_text(), "# My rules\nBe brief.\n\n" + instructions_block() + "\n")
        self.assertEqual(self.claude.stat().st_mode & 0o777, 0o640)

    def test_an_older_unedited_block_is_replaced_in_place(self) -> None:
        import hashlib
        older = "Old text."
        block = (f"<!-- lifecontext:instructions:begin sha256={hashlib.sha256(older.encode()).hexdigest()} -->\n"
                 f"{older}\n<!-- lifecontext:instructions:end -->")
        self.put(self.claude, f"before\n\n{block}\nafter\n")
        planned = self.run_install(agents=("claude",), dry_run=True)
        self.assertEqual(self.actions(planned), {"claude": "update"})
        self.assertIn("would update the block in", render(planned))
        self.assertIn("Old text.", self.claude.read_text())
        self.run_install(agents=("claude",))
        self.assertEqual(self.claude.read_text(), f"before\n\n{instructions_block()}\nafter\n")

    def test_an_edited_block_is_left_alone_on_install_and_remove(self) -> None:
        self.run_install(agents=("claude",))
        edited = self.claude.read_text().replace("Cite the title", "Cite nothing")
        self.put(self.claude, edited)
        for report in (self.run_install(agents=("claude",)), remove(self.home, ["claude"], instructions=True,
                                                                     environ=self.environ)):
            self.assertEqual(self.actions(report), {"claude": "edited"})
            self.assertIn("edited by hand, left alone", render(report))
            self.assertEqual(self.claude.read_text(), edited)
        self.assertEqual(check(self.home, ["claude"], instructions=True, environ=self.environ)
                         ["instructions"][0]["state"], "edited")
        # A begin marker without its end marker is not ours to rewrite either.
        self.put(self.claude, "x\n" + instructions_block().rsplit("\n", 1)[0] + "\n")
        self.assertEqual(self.actions(self.run_install(agents=("claude",))), {"claude": "edited"})

    def test_remove_restores_the_file_byte_for_byte(self) -> None:
        for original in ("", "# Rules\n", "# Rules\n\nmore\n", "no trailing newline", "crlf\r\nline\r\n"):
            with self.subTest(original=original):
                self.put(self.claude, original)
                self.run_install(agents=("claude",))
                dry = remove(self.home, ["claude"], dry_run=True, instructions=True, environ=self.environ)
                self.assertEqual(self.actions(dry), {"claude": "remove"})
                self.assertIn(instructions_block(), self.claude.read_text())
                report = remove(self.home, ["claude"], instructions=True, environ=self.environ)
                self.assertEqual(self.actions(report), {"claude": "remove"})
                self.assertEqual(self.claude.read_bytes(), original.encode())
                self.assertEqual(self.actions(remove(self.home, ["claude"], instructions=True,
                                                     environ=self.environ)), {"claude": "none"})

    def test_dry_run_changes_nothing_and_check_reports_the_state(self) -> None:
        self.put(self.claude, "keep\n")
        before = self.claude.read_bytes()
        report = self.run_install(dry_run=True)
        self.assertEqual(self.actions(report), {"claude": "write", "codex": "write"})
        self.assertIn("would add the block to", render(report))
        self.assertEqual(self.claude.read_bytes(), before)
        self.assertFalse(self.codex.parent.exists())
        states = lambda: [item["state"] for item in check(self.home, ["claude", "codex"], instructions=True,
                                                          environ=self.environ)["instructions"]]
        self.assertEqual(states(), ["absent", "absent"])
        self.run_install(agents=("claude",))
        self.assertEqual(states(), ["current", "absent"])
        self.assertIn("block present", render(check(self.home, ["claude"], instructions=True, environ=self.environ)))

    def test_a_missing_directory_is_not_created_for_the_note(self) -> None:
        report = install(self.home, ["codex"], dry_run=True, environ={"CODEX_HOME": str(self.home / "elsewhere")},
                         instructions=True)
        self.assertEqual(report["instructions"][0]["state"], "no_directory")
        install(self.home, ["codex"], environ={"CODEX_HOME": str(self.home / "elsewhere")}, instructions=True)
        self.assertFalse((self.home / "elsewhere").exists())

    def test_codex_honours_codex_home(self) -> None:
        elsewhere = self.home / "codex-config"
        elsewhere.mkdir()
        report = install(self.home, ["codex"], environ={**self.environ, "CODEX_HOME": str(elsewhere)},
                         instructions=True)
        self.assertEqual(report["instructions"][0]["file"], str(elsewhere / "AGENTS.md"))
        self.assertEqual((elsewhere / "AGENTS.md").read_text(), instructions_block() + "\n")
        self.assertFalse(self.codex.exists())

    def test_a_symlinked_file_is_edited_through_its_target_only_when_that_is_ours_and_regular(self) -> None:
        target = self.home / "dotfiles" / "CLAUDE.md"
        self.put(target, "mine\n")
        self.claude.parent.mkdir()
        self.claude.symlink_to(target)
        self.run_install(agents=("claude",))
        self.assertTrue(self.claude.is_symlink())
        self.assertEqual(target.read_text(), "mine\n\n" + instructions_block() + "\n")
        self.assertEqual(os.listdir(target.parent), ["CLAUDE.md"])

        self.claude.unlink()
        self.claude.symlink_to(self.home / "missing")
        report = self.run_install(agents=("claude",))
        self.assertEqual(report["instructions"][0]["reason"], "dangling_symlink")
        self.assertFalse(report["ok"])
        self.claude.unlink()
        self.claude.symlink_to(self.home / "dotfiles")
        self.assertEqual(self.run_install(agents=("claude",))["instructions"][0]["reason"], "not_a_regular_file")
        self.assertIn("refused (not_a_regular_file)", render(self.run_install(agents=("claude",))))

    def test_a_file_of_another_user_is_refused(self) -> None:
        from unittest import mock
        self.put(self.claude, "x\n")
        with mock.patch("lifecontext.skills.os.getuid", return_value=os.getuid() + 1):
            report = self.run_install(agents=("claude",))
        self.assertEqual(report["instructions"][0]["reason"], "owned_by_another_user")
        self.assertEqual(self.claude.read_text(), "x\n")

    def test_non_utf8_and_oversize_files_are_refused_untouched(self) -> None:
        for content, reason in ((b"caf\xe9\n", "not_utf8"), (b"a" * (1024 * 1024 + 1), "too_large")):
            with self.subTest(reason=reason):
                self.put(self.claude, content)
                for report in (self.run_install(agents=("claude",)),
                               remove(self.home, ["claude"], instructions=True, environ=self.environ)):
                    self.assertEqual(report["instructions"][0]["reason"], reason)
                    self.assertFalse(report["ok"])
                self.assertEqual(self.claude.read_bytes(), content)

    def test_command_line_adds_and_removes_the_block(self) -> None:
        env = {"HOME": str(self.home), "PATH": os.defpath, "PYTHONPATH": str(V1_ROOT / "src")}
        (self.home / ".claude").mkdir()

        def run(*args):
            return subprocess.run([sys.executable, "-m", "lifecontext.cli", "skills", *args, "--agent", "claude"],
                                  env=env, cwd=self.home, capture_output=True, text=True, timeout=60, check=False)
        result = run("install", "--instructions", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["instructions"][0]["action"], "write")
        self.assertEqual(self.claude.read_text(), instructions_block() + "\n")
        self.assertEqual(run("check", "--instructions").returncode, 0)
        self.assertEqual(run("remove", "--instructions").returncode, 0)
        self.assertEqual(self.claude.read_text(), "")


if __name__ == "__main__":
    unittest.main()
