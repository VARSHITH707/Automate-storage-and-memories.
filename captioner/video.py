"""Video helpers: ffprobe checks and ffmpeg frame/audio extraction (argument lists, no shell)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import tools


class VideoError(RuntimeError):
    pass


# Inputs are untrusted: read local files only (no network URLs inside playlists), never stdin.
SAFE_INPUT = ["-nostdin", "-protocol_whitelist", "file"]
MAX_AUDIO_SECONDS = 1800  # speech context beyond 30 minutes is not needed for a 3-line caption


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    has_audio: bool
    created: datetime | None


def probe(path: Path, timeout: float = 60) -> VideoInfo:
    try:
        out = tools.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-print_format", "json",
                         "-show_format", "-show_streams", str(path)], timeout=timeout)
        data = json.loads(out.stdout or "{}")
    except (tools.ToolError, json.JSONDecodeError) as exc:
        raise VideoError(f"Not a readable video: {exc}") from exc
    streams = data.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise VideoError("The file has no video stream")
    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration") or 0)
    except ValueError:
        duration = 0.0
    created = None
    raw = (fmt.get("tags") or {}).get("creation_time")
    if raw:
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            created = None
    return VideoInfo(duration=max(duration, 0.0), has_audio=any(s.get("codec_type") == "audio" for s in streams),
                     created=created)


def frame_times(duration: float, interval: float, max_frames: int) -> list[float]:
    """About one frame every `interval` seconds, at most `max_frames`, spread over the whole video."""
    if duration <= 0:
        return [0.0]
    times = [interval / 2 + k * interval for k in range(int(duration // interval) + 1)]
    times = [t for t in times if t < duration] or [duration / 2]
    if len(times) > max_frames:
        times = [duration * (i + 0.5) / max_frames for i in range(max_frames)]
    return [round(t, 3) for t in times]


def extract_frames(path: Path, times: list[float], out_dir: Path, timeout: float = 60) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, t in enumerate(times):
        target = out_dir / f"frame-{i:02d}.jpg"
        tools.run(["ffmpeg", *SAFE_INPUT, "-v", "error", "-y", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                   "-vf", "scale='min(1024,iw)':-2", "-q:v", "3", str(target)], timeout=timeout)
        if target.exists() and target.stat().st_size > 0:
            frames.append(target)
    if not frames:
        raise VideoError("No frames could be extracted")
    return frames


def extract_audio(path: Path, out: Path, timeout: float = 300) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tools.run(["ffmpeg", *SAFE_INPUT, "-v", "error", "-y", "-i", str(path), "-t", str(MAX_AUDIO_SECONDS), "-vn",
               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)], timeout=timeout)
    if not out.exists() or out.stat().st_size == 0:
        raise VideoError("No audio could be extracted")
    return out
