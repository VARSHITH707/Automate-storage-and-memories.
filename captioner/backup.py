"""Optional extra copy of the originals (external drive folder or rclone remote). Off by default.

Copies always read from the immutable original and are verified by SHA-256 at the destination.
A backup failure never touches the canonical archive.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import tools
from .archive import CHUNK, sha256_file
from .config import BackupConfig


class BackupError(RuntimeError):
    pass


class BackupTarget:
    def __init__(self, cfg: BackupConfig, archive_root: Path):
        self.kind = cfg.kind
        self.target = cfg.target
        self.archive_root = archive_root

    @property
    def destination(self) -> str:
        return f"{self.kind}:{self.target}"

    def _rel(self, asset: Path) -> Path:
        return asset.relative_to(self.archive_root)

    def copy(self, asset: Path, sha256: str) -> None:
        if self.kind == "path":
            self._copy_path(asset, sha256)
        else:
            self._copy_rclone(asset, sha256)

    def _copy_path(self, asset: Path, sha256: str) -> None:
        dest = Path(self.target) / self._rel(asset)
        if dest.exists() and sha256_file(dest) == sha256:
            return
        if not Path(self.target).exists():
            raise BackupError("Backup folder is not available (is the drive connected?)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".backup-")
        try:
            with os.fdopen(fd, "wb") as out, asset.open("rb") as inp:
                while chunk := inp.read(CHUNK):
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if sha256_file(Path(tmp)) != sha256:
                raise BackupError("Backup copy failed verification")
            os.replace(tmp, dest)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _copy_rclone(self, asset: Path, sha256: str) -> None:
        remote = f"{self.target.rstrip('/')}/{self._rel(asset).as_posix()}"
        try:
            tools.run(["rclone", "copyto", str(asset), remote], timeout=3600)
            out = tools.run(["rclone", "hashsum", "sha256", "--download", remote], timeout=3600).stdout
        except tools.ToolError as exc:
            raise BackupError(f"rclone failed: {exc}") from exc
        found = out.split()[0].lower() if out.strip() else ""
        if found != sha256:
            raise BackupError("rclone copy failed verification")
