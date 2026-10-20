"""A stand-in for lifecontext.cli.main behind lifecontext.agent_gate, for the gate and remote tests.

Run as a script it is the forced command: `python agent_gate_stub.py` reads
SSH_ORIGINAL_COMMAND like `lifecontext agent-gate`, but runs `stub_main`.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

CALLS: list[list[str]] = []
ORIGINAL = b"\x00original\xff bytes\n"


def stub_main(argv) -> int:
    """Report the arguments, the staged files and stdin; write a download; exit as asked."""
    argv = list(argv)
    CALLS.append(argv)
    if os.environ.get("STUB_LOG"):
        with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps(argv) + "\n")
    files = {}
    for word in argv:
        path = word.removeprefix("--input=")
        if f"{os.sep}agent-stage{os.sep}" in path and os.path.isfile(path):
            data = Path(path).read_bytes()
            files[path] = {
                "text": data.decode("utf-8", "replace"),
                "size": len(data),
                "mode": oct(stat.S_IMODE(os.stat(path).st_mode)),
                "dir_mode": oct(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)),
            }
    if os.environ.get("STUB_READY"):
        Path(os.environ["STUB_READY"]).write_text("ready")
        time.sleep(60)
    if argv[:2] == ["documents", "download"]:
        if argv[2] == "fail_download":
            print('{"error":"api_unavailable"}')
            return 1
        if argv[2] != "no_file":
            with open(argv[3], "xb") as stream:
                stream.write(ORIGINAL)
        print(json.dumps({"source_id": argv[2], "size_bytes": len(ORIGINAL)}))
        return 0
    stdin = sys.stdin.read() if "-" in argv or argv[:1] == ["mcp"] else None
    print(json.dumps({"args": argv, "files": files, "stdin": stdin}))
    return 3 if "--fail-on" in argv else 0


if __name__ == "__main__":
    from lifecontext import agent_gate

    sys.exit(agent_gate.run(main=stub_main))
