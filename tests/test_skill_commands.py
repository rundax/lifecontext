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


def _segments(tokens: list[str]) -> list[list[str]]:
    """The calls on one line: `a | b && c; d` is four."""
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _commands(markdown: str, parser: argparse.ArgumentParser) -> list[tuple[str, list[str]]]:
    """Each command the Markdown names, as (its text, the words after the program name).

    Code blocks count every call of ``lifecontext`` or ``"$CLI"`` on a line. Code spans
    also count a bare command (``wiki show PAGE_ID``), a bare ``--flag``, and a span
    that looks like a call of a command the parser lacks: an unknown lowercase word
    followed by a lifecontext flag or subcommand name (``wikis show PAGE_ID``)."""
    top_level = set(_subcommands(parser).choices)
    flags, names = _all_flags(parser), _all_subcommand_names(parser)
    lines = [(line, False) for block in _FENCE.findall(markdown)
             for line in block.replace("\\\n", " ").splitlines()]
    lines += [(" ".join(span.split()), True) for span in _SPAN.findall(_FENCE.sub("", markdown))]
    found = []
    for line, bare in lines:
        for segment in _segments(_tokens(line)):
            starts = [index for index, token in enumerate(segment) if token in _PROGRAM]
            if starts:
                words = segment[starts[0] + 1:]
            elif bare and (segment[0] in top_level or segment[0].startswith("--")):
                words = segment
            elif bare and re.fullmatch(r"[a-z][a-z-]*", segment[0]) and len(segment) > 1 \
                    and (segment[1] in names or any(word.split("=", 1)[0] in flags for word in segment[1:])):
                words = segment
            else:
                continue
            if words:
                found.append((line, words))
    return found


def _subcommands(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    return next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)


def _all_subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    subcommands = _subcommands(parser)
    names = set(subcommands.choices) if subcommands else set()
    for child in (subcommands.choices.values() if subcommands else ()):
        names |= _all_subcommand_names(child)
    return names


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


def _skill_problems(files: list[Path]) -> dict[tuple[str, str], list[str]]:
    parser = cli._build_parser()
    problems = {}
    for path in files:
        for line, words in _commands(path.read_text(encoding="utf-8"), parser):
            found = _problems(parser, words)
            if found:
                problems[(path.name, line)] = found
    return problems


class SkillCommandTests(unittest.TestCase):
    def test_every_command_and_flag_the_skill_names_exists_in_the_parser(self) -> None:
        files = _skill_files()
        self.assertGreater(len(files), 1)
        self.assertTrue(any(path.parent.name == "references" for path in files))
        problems = _skill_problems(files)
        report = "\n".join(f"{name}: `{line}`: {'; '.join(found)}" for (name, line), found in sorted(problems.items()))
        self.assertEqual(problems, {}, "commands or flags the parser does not have:\n" + report)

    def test_the_recipes_are_read_and_checked(self) -> None:
        parser = cli._build_parser()
        text = (skill_source() / "SKILL.md").read_text(encoding="utf-8")
        commands = [words for _, words in _commands(text, parser)]
        for expected in (["wiki", "list", "--json"], ["wiki", "show", "source_<SOURCE_ID>", "--json"],
                         ["documents", "import", "/path/to/lease.pdf", "--idempotency-key", "UNIQUE_KEY", "--json"],
                         ["retain", "SOURCE_ID", "1", "--idempotency-key", "UNIQUE_KEY", "--json"]):
            self.assertIn(expected, commands)
        self.assertIn(["recall", "--query", "What does my lease say about the deposit?", "--json"], commands)

    def test_a_made_up_command_flag_or_choice_is_reported(self) -> None:
        parser = cli._build_parser()
        markdown = "\n".join([
            "```bash",
            '"$CLI" wiki search --json',
            '"$CLI" recall --query "x" --page 3 --json',
            '"$CLI" wiki lint --fail-on fatal --json',
            '"$CLI" status --json && "$CLI" wiki serch',
            "```",
            "Run `curate fix SOURCE_ID`, `wiki show PAGE_ID EXTRA`, `--no-such-flag`, `wikis show PAGE_ID --json`,"
            " `tesseract --list-langs` and `lifecontext status --json`.",
        ])
        problems = [_problems(parser, words) for _, words in _commands(markdown, parser)]
        self.assertEqual(problems, [
            ["lifecontext wiki has no command search"],
            ["lifecontext recall has no --page"],
            ["lifecontext wiki lint --fail-on has no choice fatal"],
            [],
            ["lifecontext wiki has no command serch"],
            ["lifecontext curate has no action fix"],
            ["lifecontext wiki show takes no argument EXTRA"],
            ["no lifecontext command has --no-such-flag"],
            ["lifecontext has no command wikis"],
            [],
        ])

    def test_the_description_leads_with_answering_questions_on_supported_platforms(self) -> None:
        text = (skill_source() / "SKILL.md").read_text(encoding="utf-8")
        front_matter = text.split("---", 2)[1]
        match = re.search(r'^description: "(.*)"$', front_matter, re.MULTILINE)
        self.assertIsNotNone(match)
        description = match.group(1)
        self.assertTrue(description.startswith("Answer questions from the user's own documents and notes"))
        self.assertIn("platforms: [linux, macos]", front_matter)
        body = text.split("---", 2)[2]
        self.assertIn("Use when the user asks what their documents or notes say", body)
        self.assertLess(body.index("## Answer a question"), body.index("## Operator boundaries"))


if __name__ == "__main__":
    unittest.main()
