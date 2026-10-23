"""Per-user application-data locations. Nothing here lives inside the repository."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "PrivateMediaCaptioner"


def app_home() -> Path:
    """Return the per-user app-data folder. `CAPTIONER_HOME` overrides it (tests, demo)."""
    override = os.environ.get("CAPTIONER_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def database(self) -> Path:
        return self.home / "captioner.db"

    @property
    def sessions(self) -> Path:
        return self.home / "sessions"

    @property
    def temp(self) -> Path:
        return self.home / "tmp"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def default_archive(self) -> Path:
        return self.home / "archive"

    def ensure(self) -> None:
        for folder in (self.home, self.sessions, self.temp, self.logs):
            folder.mkdir(parents=True, exist_ok=True)
        restrict(self.home)
        restrict(self.sessions)


def get_paths() -> Paths:
    return Paths(app_home())


def restrict(path: Path) -> bool:
    """Limit a file or folder to the current user where the OS supports POSIX modes.

    On Windows, %LOCALAPPDATA% is already private to the user through its default ACL,
    so nothing is changed there and False is returned.
    """
    if sys.platform == "win32" or not path.exists():
        return False
    mode = stat.S_IRWXU if path.is_dir() else stat.S_IRUSR | stat.S_IWUSR
    os.chmod(path, mode)
    return True
