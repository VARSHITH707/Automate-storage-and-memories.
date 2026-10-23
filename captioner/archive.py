"""Immutable, verified archive of originals; atomic JSON sidecars; metadata-enriched copies."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import tools

CHUNK = 1024 * 1024
SIDECAR_SCHEMA = 1
TEMP_PREFIX = ".incoming-"


class ArchiveError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def check_space(folder: Path, needed: int, min_free_mb: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if free - needed < min_free_mb * 1024 * 1024:
        raise ArchiveError(f"Not enough disk space in {folder}: {free // 2**20} MB free")


@dataclass(frozen=True)
class Archived:
    path: Path
    sha256: str
    size: int


def archive_original(src: Path, root: Path, relpath: Path, expected_sha: str) -> Archived:
    """Copy `src` into the archive, verify its hash, then atomically rename it into place.

    If the destination already exists with the same hash (a crash after the rename but before the
    database commit), it is reused. A different file at that path is an error, never overwritten.
    """
    dest = root / relpath
    if root.resolve() not in dest.resolve().parents:
        raise ArchiveError("Archive path escaped the archive root")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if sha256_file(dest) == expected_sha:
            os.chmod(dest, stat.S_IREAD)  # a crash may have happened before the chmod
            return Archived(dest, expected_sha, dest.stat().st_size)
        raise ArchiveError("A different file already exists at the archive path")
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=TEMP_PREFIX)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            while chunk := inp.read(CHUNK):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        actual = sha256_file(tmp)
        if actual != expected_sha:
            raise ArchiveError("Archived copy does not match the received file")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(dest, stat.S_IREAD)  # read-only, owner only: the original is immutable
    return Archived(dest, expected_sha, dest.stat().st_size)


def sidecar_path(asset_path: Path) -> Path:
    return asset_path.with_name(asset_path.name + ".json")


def write_json_atomic(path: Path, data: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".sidecar-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_sidecar(asset_path: Path, sha256: str, caption: str, caption_source: str, approved_at: str,
                  include_location: bool, model_meta: dict | None) -> Path:
    data = {
        "schema_version": SIDECAR_SCHEMA,
        "asset_sha256": sha256,
        "caption": caption,
        "caption_source": caption_source,
        "approved_at": approved_at,
        "include_location": include_location,
        "models": model_meta or {},
    }
    path = sidecar_path(asset_path)
    write_json_atomic(path, data)
    return path


def write_metadata_copy(original: Path, archive_root: Path, caption: str, expected_sha: str,
                        work_dir: Path) -> Path:
    """Write the caption into a separate copy with ExifTool. The original is never touched."""
    rel = original.relative_to(archive_root / "originals")
    dest = archive_root / "enriched" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(TEMP_PREFIX + dest.name)
    caption_file = work_dir / "caption.txt"
    caption_file.write_text(caption, encoding="utf-8")
    try:
        shutil.copyfile(original, tmp)
        os.chmod(tmp, stat.S_IREAD | stat.S_IWRITE)
        tools.run(["exiftool", "-q", "-overwrite_original", "-charset", "utf8",
                   f"-XMP-dc:Description<={caption_file}", f"-EXIF:ImageDescription<={caption_file}",
                   str(tmp)], timeout=120)
        read_back = tools.run(["exiftool", "-s3", "-charset", "utf8", "-XMP-dc:Description", str(tmp)],
                              timeout=60).stdout.strip()
        if read_back.replace("\r\n", "\n") != caption.strip():
            raise ArchiveError("Metadata copy did not read back the caption")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
        caption_file.unlink(missing_ok=True)
    if sha256_file(original) != expected_sha:
        raise ArchiveError("Original changed while writing the metadata copy")  # should be impossible
    return dest


def leftover_temp_files(archive_root: Path) -> list[Path]:
    """Incomplete archive copies left by a crash. Only our own temp prefix is ever matched."""
    if not archive_root.exists():
        return []
    return sorted(p for p in archive_root.rglob(TEMP_PREFIX + "*") if p.is_file())


def remove_job_temp(temp_root: Path, job_dir: str | None) -> None:
    """Delete one job's temp folder. Refuses anything outside the app's temp root."""
    if not job_dir:
        return
    target = Path(job_dir).resolve()
    if temp_root.resolve() not in target.parents:
        raise ArchiveError("Refusing to delete a folder outside the temp root")
    shutil.rmtree(target, ignore_errors=True)
