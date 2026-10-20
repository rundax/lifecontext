"""Time cold imports of LifeContext and the Hindsight stack; print Markdown rows.

Run with the Python of the environment under test:
    VENV/bin/python -I .github/scripts/spike_probe.py
"""

from __future__ import annotations

import subprocess
import sys
import time

MODULES = (
    "lifecontext",
    "lifecontext.cli",
    "hindsight",
    "hindsight_api",
    "hindsight_embed",
    "hindsight_client",
    "sentence_transformers",
    "torch",
)

failed = False
print("| Import | Seconds (fresh process) | Result |")
print("|---|---|---|")
for module in MODULES:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    if result.returncode == 0:
        outcome = "ok"
    else:
        failed = True
        last = (result.stderr.strip().splitlines() or ["no output"])[-1]
        outcome = "failed: " + last.replace("|", "/")[:200]
    print(f"| `{module}` | {elapsed:.1f} | {outcome} |")

torch_info = subprocess.run(
    [sys.executable, "-I", "-c",
     "import torch; print(torch.__version__, torch.cuda.is_available(), torch.version.cuda)"],
    capture_output=True,
    text=True,
    check=False,
)
print()
print(f"torch version, CUDA available, CUDA build: `{torch_info.stdout.strip() or torch_info.stderr.strip()[-200:]}`")
sys.exit(1 if failed else 0)
