"""Every command and flag the agent skill names exists in the real CLI parser."""

from __future__ import annotations

import argparse
import re
import shlex
import unittest
from pathlib import Path

from lifecontext import cli
from lifecontext.skills import skill_source

_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_SPAN = re.compile(r"`([^`]+)`")
_PROGRAM = {"lifecontext", "$CLI"}
_SEPARATORS = {"|", "||", "&", "&&", ";"}
_PLACEHOLDER = re.compile(r"[A-Z][A-Z0-9_]*|.*<.*>.*")


def _skill_files() -> list[Path]:
    source = skill_source()
    return [source / "SKILL.md", *sorted((source / "references").glob("*.md"))]


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars="|;&")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return line.split()


def _commands(markdown: str, top_level: set[str]) -> list[tuple[str, list[str]]]:
    """Each command the Markdown names, as (its text, the words after the program name).

    Code blocks count only lines that call ``lifecontext`` or ``"$CLI"``. Code spans
    also count a bare command (``wiki show PAGE_ID``) or a bare ``--flag``."""
    lines = [(line, False) for block in _FENCE.findall(markdown)
             for line in block.replace("\\\n", " ").splitlines()]
    lines += [(" ".join(span.split()), True) for span in _SPAN.findall(_FENCE.sub("", markdown))]
    found = []
    for line, bare in lines:
        tokens = _tokens(line)
        starts = [index for index, token in enumerate(tokens) if token in _PROGRAM]
        if starts:
            words = tokens[starts[0] + 1:]
        elif bare and tokens and (tokens[0] in top_level or tokens[0].startswith("--")):
            words = tokens
        else:
            continue
        for index, word in enumerate(words):
            if word in _SEPARATORS:
                words = words[:index]
                break
        if words:
            found.append((line, words))
    return found


def _subcommands(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    return next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)


def _all_flags(parser: argparse.ArgumentParser) -> set[str]:
    flags = set(parser._option_string_actions)
    subcommands = _subcommands(parser)
    for child in (subcommands.choices.values() if subcommands else ()):
        flags |= _all_flags(child)
    return flags


def _problems(parser: argparse.ArgumentParser, words: list[str]) -> list[str]:
    """What in one command the parser does not have: a subcommand, a flag or a choice."""
    if words[0].startswith("--"):
        flag = words[0].split("=", 1)[0]
        return [] if flag in _all_flags(parser) else [f"no lifecontext command has {flag}"]
    problems: list[str] = []
    path = ["lifecontext"]
    position = 0
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word.startswith("-"):
            flag = word.split("=", 1)[0]
            action = parser._option_string_actions.get(flag)
            takes_value = action is None or action.nargs != 0
            value = word.split("=", 1)[1] if "=" in word else None
            if takes_value and value is None and index < len(words) and not words[index].startswith("-"):
                value = words[index]
                index += 1
            if action is None:
                problems.append(f"{' '.join(path)} has no {flag}")
            elif action.choices and value is not None and not _PLACEHOLDER.fullmatch(value) \
                    and value not in action.choices:
                problems.append(f"{' '.join(path)} {flag} has no choice {value}")
            continue
        subcommands = _subcommands(parser)
        if subcommands is not None:
            name = word.split(".", 1)[0]  # `documents show.extraction_warnings` names a response field
            if name in subcommands.choices:
                parser = subcommands.choices[name]
                path.append(name)
                position = 0
                continue
            if not _PLACEHOLDER.fullmatch(word):
                problems.append(f"{' '.join(path)} has no command {word}")
            return problems
        positionals = [action for action in parser._actions if not action.option_strings]
        if position >= len(positionals):
            problems.append(f"{' '.join(path)} takes no argument {word}")
        elif positionals[position].choices and not _PLACEHOLDER.fullmatch(word) \
                and word not in positionals[position].choices:
            problems.append(f"{' '.join(path)} has no {positionals[position].dest} {word}")
        position += 1
    return problems


def _skill_problems() -> dict[tuple[str, str], list[str]]:
    parser = cli._build_parser()
    top_level = set(_subcommands(parser).choices)
    problems = {}
    for path in _skill_files():
        for line, words in _commands(path.read_text(encoding="utf-8"), top_level):
            found = _problems(parser, words)
            if found:
                problems[(path.name, line)] = found
    return problems


class SkillCommandTests(unittest.TestCase):
    def test_every_command_and_flag_the_skill_names_exists_in_the_parser(self) -> None:
        problems = _skill_problems()
        report = "\n".join(f"{name}: `{line}`: {'; '.join(found)}" for (name, line), found in sorted(problems.items()))
        self.assertEqual(problems, {}, "commands or flags the parser does not have:\n" + report)

    def test_the_recipes_are_read_and_checked(self) -> None:
        parser = cli._build_parser()
        text = (skill_source() / "SKILL.md").read_text(encoding="utf-8")
        commands = [words for _, words in _commands(text, set(_subcommands(parser).choices))]
        for expected in (["wiki", "list", "--json"], ["wiki", "show", "source_<SOURCE_ID>", "--json"],
                         ["documents", "import", "/path/to/lease.pdf", "--idempotency-key", "UNIQUE_KEY", "--json"],
                         ["retain", "SOURCE_ID", "1", "--idempotency-key", "UNIQUE_KEY", "--json"]):
            self.assertIn(expected, commands)
        self.assertIn(["recall", "--query", "What does my lease say about the deposit?", "--json"], commands)

    def test_a_made_up_command_flag_or_choice_is_reported(self) -> None:
        parser = cli._build_parser()
        top_level = set(_subcommands(parser).choices)
        markdown = "\n".join([
            "```bash",
            '"$CLI" wiki search --json',
            '"$CLI" recall --query "x" --page 3 --json',
            '"$CLI" wiki lint --fail-on fatal --json',
            "```",
            "Run `curate fix SOURCE_ID`, `wiki show PAGE_ID EXTRA`, `--no-such-flag` and `lifecontext status --json`.",
        ])
        problems = [_problems(parser, words) for _, words in _commands(markdown, top_level)]
        self.assertEqual(problems, [
            ["lifecontext wiki has no command search"],
            ["lifecontext recall has no --page"],
            ["lifecontext wiki lint --fail-on has no choice fatal"],
            ["lifecontext curate has no action fix"],
            ["lifecontext wiki show takes no argument EXTRA"],
            ["no lifecontext command has --no-such-flag"],
            [],
        ])

    def test_the_description_leads_with_answering_questions_on_supported_platforms(self) -> None:
        text = (skill_source() / "SKILL.md").read_text(encoding="utf-8")
        front_matter = text.split("---", 2)[1]
        description = re.search(r'^description: "(.*)"$', front_matter, re.MULTILINE).group(1)
        self.assertTrue(description.startswith("Answer questions from the user's own documents and notes"))
        self.assertIn("platforms: [linux, macos]", front_matter)
        body = text.split("---", 2)[2]
        self.assertIn("Use when the user asks what their documents or notes say", body)
        self.assertLess(body.index("## Answer a question"), body.index("## Operator boundaries"))


if __name__ == "__main__":
    unittest.main()
