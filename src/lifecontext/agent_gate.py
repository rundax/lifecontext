"""``lifecontext agent-gate``: an SSH forced command that limits what a remote agent can run.

Use it as the only command of a dedicated key in ``~/.ssh/authorized_keys``::

    restrict,command="/home/USER/.local/bin/lifecontext agent-gate" ssh-ed25519 AAAA... agent

The client's command line arrives in ``SSH_ORIGINAL_COMMAND``. It is split with
``shlex`` (never a shell) and must start with the word ``lifecontext`` followed
by one of the everyday agent commands in ``ALLOWED_COMMANDS``. Anything else is
refused with ``{"error":"command_not_allowed"}`` and exit status 1.

Files travel over the SSH channel, never as server paths:

* ``@stdin:NAME`` in place of an input file (``retain --input``, ``curate
  --input``, ``categories propose --input``, ``documents import``): standard
  input is saved as ``NAME`` in a fresh private directory below
  ``~/.local/state/lifecontext/agent-stage/``, the path is substituted, and the
  directory is removed afterwards, also on errors and on SIGTERM, SIGHUP and
  SIGINT. Directories a SIGKILL left behind are swept on the next call.
* ``@stdout`` as the destination of ``documents download``: the original is
  downloaded to a private path and sent back in one frame (see ``run``).

Any other path that names a server file is refused with ``path_not_allowed``.
Nothing is logged here; the commands print what they print.
"""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ALLOWED_COMMANDS = frozenset({
    "status", "doctor", "retain", "recall", "reflect", "evidence", "curate", "relationships",
    "documents", "categories", "wiki", "brief", "operations", "mcp",
})
STDIN_PREFIX = "@stdin:"
STDOUT_TOKEN = "@stdout"
FRAME_MARKER = "lifecontext-file 1"
MAX_NAME_BYTES = 200
STALE_AFTER_SECONDS = 24 * 3600
_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Terminated(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.code = 128 + signum


def _terminate(signum: int, frame: object) -> None:
    raise _Terminated(signum)


def valid_name(name: str) -> bool:
    """A plain file name: no slash, no leading dot, no control characters, at most 200 bytes."""
    try:
        size = len(name.encode("utf-8"))
    except UnicodeError:
        return False
    return (
        0 < size <= MAX_NAME_BYTES
        and not name.startswith(".")
        and "/" not in name
        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
    )


def command_word(argv: Sequence[str]) -> str | None:
    """The sub-command: the first word that is not a root option or the value of ``--port``."""
    skip = False
    for word in argv:
        if skip:
            skip = False
        elif word in {"-h", "--help", "--version"}:
            return None
        elif word == "--port":
            skip = True
        elif not word.startswith("-"):
            return word
    return None


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    return exc.code if isinstance(exc.code, int) else 1


def _call(main: Callable[[Sequence[str]], int], argv: Sequence[str]) -> int:
    try:
        return int(main(argv))
    except SystemExit as exc:
        return _exit_code(exc)


def _refuse(code: str) -> int:
    sys.stdout.write(f'{{"error":"{code}"}}\n')
    sys.stdout.flush()
    return 1


def _command_line(text: str | None) -> list[str]:
    if not text:
        raise _Refused("command_not_allowed")
    try:
        words = shlex.split(text)
    except ValueError:
        raise _Refused("invalid_request") from None
    if not words or words[0] != "lifecontext":
        raise _Refused("command_not_allowed")
    return words[1:]


def _stage_root() -> Path:
    root = Path.home() / ".local" / "state" / "lifecontext" / "agent-stage"
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise _Refused("input_unavailable")
        os.chmod(root, 0o700)
    except OSError:
        raise _Refused("input_unavailable") from None
    cutoff = time.time() - STALE_AFTER_SECONDS
    with contextlib.suppress(OSError):
        for entry in list(root.iterdir()):
            if entry.lstat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
    return root


def _receive(path: Path, limit: int) -> None:
    try:
        data = sys.stdin.buffer.read(limit + 1)
        if len(data) > limit:
            raise _Refused("body_too_large")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
            stream.write(data)
    except OSError:
        raise _Refused("input_unavailable") from None


def _read_output(path: Path, limit: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise _Refused("output_unavailable")
            return stream.read()
    except OSError:
        raise _Refused("output_unavailable") from None


def _substitute(argv: Sequence[str], mapping: Mapping[str, str]) -> list[str]:
    result = []
    for word in argv:
        if word in mapping:
            word = mapping[word]
        elif word.startswith("--input=") and word[len("--input="):] in mapping:
            word = "--input=" + mapping[word[len("--input="):]]
        result.append(word)
    return result


def _serve(argv: list[str], main: Callable[[Sequence[str]], int] | None) -> int:
    from lifecontext import cli

    main = main or cli.main
    if command_word(argv) not in ALLOWED_COMMANDS | {None}:
        raise _Refused("command_not_allowed")
    parser = cli._build_parser()
    try:
        parsed = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version or a usage error, printed by argparse
        return _exit_code(exc)
    if parsed.command not in ALLOWED_COMMANDS:
        raise _Refused("command_not_allowed")

    source = getattr(parsed, "input", None)
    destination = getattr(parsed, "destination", None)
    staged_input = source is not None and source.startswith(STDIN_PREFIX)
    if (
        getattr(parsed, "import_dirs", None)
        or (source not in (None, "-") and not staged_input)
        or (destination is not None and destination != STDOUT_TOKEN)
    ):
        raise _Refused("path_not_allowed")
    if staged_input and not valid_name(source[len(STDIN_PREFIX):]):
        raise _Refused("invalid_request")
    if not staged_input and destination is None:
        return _call(main, argv)

    if parsed.command == "documents":
        limit = cli.MAX_DOCUMENT_BYTES
    elif parsed.command == "categories":
        limit = cli.MAX_REQUEST_BYTES
    else:
        limit = cli.MAX_TEXT_BYTES
    previous = {number: signal.signal(number, _terminate) for number in _SIGNALS}
    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=_stage_root()))
        mapping: dict[str, str] = {}
        if staged_input:
            (stage / "in").mkdir(mode=0o700)
            mapping[source] = os.fspath(stage / "in" / source[len(STDIN_PREFIX):])
            _receive(Path(mapping[source]), limit)
        output = stage / "out" / "download"
        if destination is not None:
            output.parent.mkdir(mode=0o700)
            mapping[destination] = os.fspath(output)
        final = _substitute(argv, mapping)
        try:  # an abbreviated option could have kept the token; the command must see the paths
            checked = parser.parse_args(final)
        except SystemExit:
            raise _Refused("invalid_request") from None
        if getattr(checked, "input", None) != mapping.get(source) or getattr(checked, "destination", None) != mapping.get(destination):
            raise _Refused("invalid_request")
        if destination is None:
            return _call(main, final)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = _call(main, final)
        text = captured.getvalue()
        if code != 0:
            sys.stdout.write(text)
            sys.stdout.flush()
            return code
        content = _read_output(output, cli.MAX_DOCUMENT_BYTES)
        result = text.encode("utf-8")
        sys.stdout.flush()
        sys.stdout.buffer.write(f"{FRAME_MARKER} {len(content)} {len(result)}\n".encode("ascii") + content + result)
        sys.stdout.buffer.flush()
        return 0
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        for number, handler in previous.items():
            signal.signal(number, handler)


def run(main: Callable[[Sequence[str]], int] | None = None) -> int:
    """Run the command in ``SSH_ORIGINAL_COMMAND`` if it is allowed; the exit status is the command's.

    ``main`` is the CLI entry point (``lifecontext.cli.main``); tests pass a stand-in.

    Output of ``documents download ID @stdout``: when the command succeeds,
    standard output is one frame and nothing else::

        lifecontext-file 1 <FILE_BYTES> <RESULT_BYTES>\\n
        <FILE_BYTES bytes: the original document>
        <RESULT_BYTES bytes: what the command printed, usually its JSON>

    When it fails, standard output is the command's own error output, with no
    frame and the command's non-zero exit status.
    """
    try:
        return _serve(_command_line(os.environ.get("SSH_ORIGINAL_COMMAND")), main)
    except _Refused as refusal:
        return _refuse(refusal.code)
    except _Terminated as terminated:
        return terminated.code
