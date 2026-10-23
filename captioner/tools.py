"""Safe calls to external programs (ffmpeg, ffprobe, ExifTool, rclone). Never uses a shell."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolResult:
    returncode: int
    stdout: str
    stderr: str


def which(name: str) -> str | None:
    """Find a program on PATH only. Unlike `shutil.which` on Windows, the current folder is never
    searched, so a program planted where the user happens to run `captioner` is never executed."""
    if os.path.isabs(name):
        return name if os.path.isfile(name) else None
    if os.sep in name or (os.altsep and os.altsep in name):
        return None  # relative paths are not accepted
    cwd = Path.cwd().resolve()
    exts = [""]
    if sys.platform == "win32":
        exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
        if Path(name).suffix.lower() in exts:
            exts = [""]
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder.strip() or folder.strip() == ".":
            continue
        base = Path(folder)
        try:
            if base.resolve() == cwd:
                continue
        except OSError:
            continue
        for ext in exts:
            candidate = base / f"{name}{ext}"
            if candidate.is_file() and (sys.platform == "win32" or os.access(candidate, os.X_OK)):
                return str(candidate)
    return None


def run(args: list[str], timeout: float = 120, check: bool = True) -> ToolResult:
    """Run a program with an argument list. Raises ToolError on a missing tool, timeout or failure."""
    if not args or not isinstance(args, list):
        raise ToolError("args must be a non-empty list")
    exe = which(args[0])
    if exe is None:
        raise ToolError(f"{args[0]} is not installed or not on PATH")
    try:
        proc = subprocess.run([exe, *args[1:]], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{args[0]} timed out after {timeout:.0f} s") from exc
    result = ToolResult(proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise ToolError(f"{args[0]} exited with {proc.returncode}: {proc.stderr.strip()[:300]}")
    return result


def version(name: str, flag: str = "-version") -> str | None:
    """First line of a tool's version output, or None if it is missing or broken."""
    if which(name) is None:
        return None
    try:
        out = run([name, flag], timeout=20, check=False)
    except ToolError:
        return None
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else None
