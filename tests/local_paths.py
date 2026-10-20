"""Private scratch paths shared by the isolated V1 tests."""

from __future__ import annotations

import os
from pathlib import Path
import stat


V1_ROOT = Path(__file__).resolve().parents[1]


def scratch_root() -> Path:
    """Return a private scratch directory that cannot escape the project root."""
    dev = V1_ROOT / ".dev"
    if dev.is_symlink():
        raise RuntimeError("V1 development directory must not be a symlink")
    dev.mkdir(mode=0o700, exist_ok=True)
    root = dev / "tmp"
    if root.is_symlink():
        raise RuntimeError("V1 temporary directory must not be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    resolved = root.resolve(strict=True)
    if not resolved.is_relative_to(V1_ROOT):
        raise RuntimeError("LifeContext temporary directory escapes the project root")
    if os.name == "posix":
        info = resolved.stat()
        if info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("V1 temporary directory has unsafe ownership or type")
        resolved.chmod(0o700)
    return resolved
