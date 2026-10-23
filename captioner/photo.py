"""Picture helpers: local EXIF reading and JPEG thumbnails for the vision model."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

# Untrusted pictures: refuse anything above 50 megapixels instead of decoding it (bomb protection).
Image.MAX_IMAGE_PIXELS = 50_000_000
EXIF_IFD, GPS_IFD = 0x8769, 0x8825
DATETIME_ORIGINAL, DATETIME = 36867, 306
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


@dataclass(frozen=True)
class PhotoMeta:
    taken_at: datetime | None
    gps: tuple[float, float] | None


def _parse_exif_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _dms(values: Any, ref: object) -> float | None:
    try:
        d, m, s = (float(v) for v in values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    deg = d + m / 60 + s / 3600
    return -deg if ref in ("S", "W") else deg


def read_exif(path: Path) -> PhotoMeta:
    """Date and GPS from the file's own EXIF. Unreadable files give empty metadata, never an error."""
    try:
        with warnings.catch_warnings(), Image.open(path) as img:
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            exif = img.getexif()
            taken = _parse_exif_date(exif.get_ifd(EXIF_IFD).get(DATETIME_ORIGINAL)) or \
                _parse_exif_date(exif.get(DATETIME))
            gps_ifd = exif.get_ifd(GPS_IFD)
    except Exception:  # noqa: BLE001 - untrusted files: Pillow can raise SyntaxError, struct.error, ...
        return PhotoMeta(None, None)
    gps = None
    if gps_ifd:
        lat, lon = _dms(gps_ifd.get(2), gps_ifd.get(1)), _dms(gps_ifd.get(4), gps_ifd.get(3))
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            gps = (lat, lon)
    return PhotoMeta(taken, gps)


def date_text(dt: datetime | None) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}" if dt else ""


def thumbnail(path: Path, out: Path, max_side: int = 1024) -> Path | None:
    """JPEG copy for the vision model, or None if Pillow cannot read the file (for example HEIC)."""
    try:
        with warnings.catch_warnings(), Image.open(path) as img:
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img.thumbnail((max_side, max_side))
            out.parent.mkdir(parents=True, exist_ok=True)
            img.convert("RGB").save(out, "JPEG", quality=85)
    except Exception:  # noqa: BLE001 - untrusted files: any decoder error means "cannot analyse"
        return None
    return out
