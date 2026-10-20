"""Private local capability token required by every HTTP route except liveness."""
from __future__ import annotations

import os
import re
import secrets
import stat

from lifecontext.paths import LifeContextPaths


def protected_path(path: str) -> bool:
    """Every route needs the token, the schema included; only ``/healthz`` liveness is public."""
    return path != "/healthz"


def local_token(paths: LifeContextPaths, *, create: bool = False) -> str:
    """Read a private token through a no-follow directory descriptor."""
    if create:
        paths.ensure_private_home()
    directory = os.open(paths.lifeweave_home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("unsafe local capability")
        if create:
            try:
                fd = os.open("api.token", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
            except FileExistsError:
                pass
            else:
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, secrets.token_hex(32).encode("ascii"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
        fd = os.open("api.token", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise OSError("unsafe local capability")
            raw = os.read(fd, 65)
            if not re.fullmatch(rb"[0-9a-f]{64}", raw):
                raise OSError("unsafe local capability")
            return raw.decode("ascii")
        finally:
            os.close(fd)
    finally:
        os.close(directory)
