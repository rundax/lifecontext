"""Paths owned by the standalone LifeContext process."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Replace ``path`` with ``data`` through a synced temporary file in the same directory."""
    descriptor, staged = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise
    # Make the rename itself durable; a filesystem that cannot sync a directory still has the file.
    try:
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class LifeContextPaths:
    """Resolve application and Hindsight state from the process HOME only."""

    home: Path

    @classmethod
    def current(cls) -> "LifeContextPaths":
        return cls(Path.home())

    @property
    def lifeweave_home(self) -> Path:
        return self.home / ".lifeweave"

    @property
    def config_path(self) -> Path:
        return self.lifeweave_home / "config.yaml"

    @property
    def credentials_path(self) -> Path:
        return self.lifeweave_home / ".env"

    @property
    def archive_dir(self) -> Path:
        return self.lifeweave_home / "archive"

    @property
    def derived_dir(self) -> Path:
        """Managed extraction/wiki artifacts, outside immutable custody inventory."""
        return self.lifeweave_home / "derived"

    @property
    def catalog_path(self) -> Path:
        return self.lifeweave_home / "catalog.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.lifeweave_home / "lifeweave.lock"

    @property
    def service_lock_path(self) -> Path:
        """Lock held by the running V1 service, separate from startup checks."""
        return self.lifeweave_home / "service.lock"

    @property
    def tiktoken_cache_dir(self) -> Path:
        """Persistent tokenizer cache for Hindsight (tiktoken's default lives in TMPDIR)."""
        return self.lifeweave_home / "cache" / "tiktoken"

    @property
    def hindsight_start_record_path(self) -> Path:
        """How LifeContext last started the Hindsight daemon (offline or not); no secrets."""
        return self.lifeweave_home / "hindsight-start.json"

    @property
    def maintenance_record_path(self) -> Path:
        """Outcome and counts of the last ``lifecontext maintain`` run."""
        return self.lifeweave_home / "maintenance.json"

    @property
    def hindsight_api_token_path(self) -> Path:
        """Bearer token the ``lifeweave`` Hindsight daemon requires; made when missing, never backed up."""
        return self.lifeweave_home / "hindsight-api.token"

    @property
    def postgres_socket_dir(self) -> Path:
        """The only place the profile's Postgres listens: a Unix socket in a private directory."""
        return self.lifeweave_home / "postgres"

    @property
    def pg0_instance_dir(self) -> Path:
        """The ``lifeweave`` profile's embedded Postgres: ``instance.json`` and ``data``."""
        return self.home / ".pg0" / "instances" / "hindsight-embed-lifeweave"

    @property
    def hindsight_profile(self) -> str:
        return "lifeweave"

    @property
    def hindsight_profiles_dir(self) -> Path:
        return self.hindsight_home / "profiles"

    @property
    def hindsight_home(self) -> Path:
        return self.home / ".hindsight"

    @property
    def hindsight_profile_env_path(self) -> Path:
        return self.hindsight_profiles_dir / f"{self.hindsight_profile}.env"

    def ensure_private_home(self) -> None:
        """Create or tighten ``~/.lifeweave`` before reading or writing state."""
        directory = self.lifeweave_home
        try:
            if not self.home.is_absolute():
                raise OSError
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise OSError
            if os.name == "posix":
                if info.st_uid != os.geteuid():
                    raise OSError
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(directory, flags)
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or (opened.st_dev, opened.st_ino)
                        != (info.st_dev, info.st_ino)
                        or opened.st_uid != os.geteuid()
                    ):
                        raise OSError
                    os.fchmod(descriptor, 0o700)
                finally:
                    os.close(descriptor)
                after = directory.lstat()
                if (
                    not stat.S_ISDIR(after.st_mode)
                    or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
                    or after.st_uid != os.geteuid()
                    or stat.S_IMODE(after.st_mode) != 0o700
                ):
                    raise OSError
        except OSError:
            raise OSError("unsafe LifeContext home") from None
