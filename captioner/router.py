"""Pure routing rules: what a message is, whether it is a Vault item, how to name its archive file."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .jobs import IncomingMessage

PICTURE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif"}
VIDEO_EXT = {"mp4", "mov", "m4v", "mkv", "webm", "avi", "3gp"}
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def classify(msg: IncomingMessage) -> str | None:
    """Pre-download guess from Telegram's metadata: 'video', 'picture', 'unsupported', or None (no media)."""
    if msg.media is None:
        return None
    if msg.media == "photo":
        return "picture"
    mime = (msg.mime or "").lower()
    ext = Path(msg.file_name or "").suffix.lower().lstrip(".")
    if mime.startswith("video/") or ext in VIDEO_EXT:
        return "video"
    if mime.startswith("image/") or ext in PICTURE_EXT:
        return "picture"
    return "unsupported"


def maybe_compressed(msg: IncomingMessage) -> bool:
    """Heuristic: Telegram 'photo' media and video documents with a video attribute were probably
    sent through the compression path. Files sent with "Send as file" arrive as plain documents."""
    return msg.media == "photo" or (msg.media == "document" and msg.has_video_attr)


def sniff(path: Path) -> tuple[str, str] | None:
    """Detect the real type from magic bytes. Returns (kind, extension) or None if unsupported."""
    with path.open("rb") as fh:
        head = fh.read(32)
    if head.startswith(b"\xff\xd8\xff"):
        return "picture", "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "picture", "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "picture", "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "picture", "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video", "avi"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "webm" if b"webm" in head else "mkv"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "picture", "heic"
        if brand == b"qt  ":
            return "video", "mov"
        if brand.startswith(b"3g"):
            return "video", "3gp"
        return "video", "mp4"
    return None


def has_marker(text: str, markers: list[str]) -> bool:
    """Whole-word, case-insensitive match of any Vault marker such as #vault or #private."""
    for marker in markers:
        if re.search(rf"(?<![\w#]){re.escape(marker)}(?!\w)", text, flags=re.IGNORECASE):
            return True
    return False


def owner_caption(text: str) -> str | None:
    """The owner's own text, kept exactly as written. Whitespace-only text means no caption."""
    return text if text and text.strip() else None


def safe_stem(file_name: str | None, fallback: str) -> str:
    """A filesystem-safe base name. Never trusted as a path: no separators, no dots at the start."""
    stem = Path(file_name or "").stem if file_name else ""
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_.")[:60]
    if not stem or stem.lower() in WINDOWS_RESERVED:
        stem = fallback
    return stem


def archive_relpath(kind: str, ext: str, stem: str, sha256: str, year: int, month: int) -> Path:
    return Path("originals") / f"{year:04d}" / f"{month:02d}" / f"{stem}__{sha256[:12]}.{ext}"
