from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from captioner import archive, router
from tests.conftest import make_msg

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 60


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("kwargs, expected", [
    ({"media": None, "mime": None, "file_name": None}, None),
    ({"media": "photo", "mime": "image/jpeg"}, "picture"),
    ({"mime": "video/mp4", "file_name": "a.mp4"}, "video"),
    ({"mime": "application/octet-stream", "file_name": "clip.MOV"}, "video"),
    ({"mime": "application/octet-stream", "file_name": "pic.HEIC"}, "picture"),
    ({"mime": "application/pdf", "file_name": "doc.pdf"}, "unsupported"),
    ({"media": "other", "mime": None, "file_name": None}, "unsupported"),
])
def test_classify(kwargs, expected) -> None:
    assert router.classify(make_msg(1, **kwargs)) == expected


def test_compression_heuristic() -> None:
    assert router.maybe_compressed(make_msg(1, media="photo"))
    assert router.maybe_compressed(make_msg(1, mime="video/mp4", has_video_attr=True))
    assert not router.maybe_compressed(make_msg(1, mime="image/jpeg"))


@pytest.mark.parametrize("head, expected", [
    (JPEG, ("picture", "jpg")),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, ("picture", "png")),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ("picture", "webp")),
    (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20, ("picture", "heic")),
    (MP4, ("video", "mp4")),
    (b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 20, ("video", "mov")),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 20, ("video", "mkv")),
    (b"%PDF-1.7 not media", None),
    (b"", None),
])
def test_sniff_uses_real_bytes(tmp_path: Path, head: bytes, expected) -> None:
    assert router.sniff(write(tmp_path / "f.bin", head)) == expected


@pytest.mark.parametrize("text, markers, expected", [
    ("#vault keep", ["#vault"], True),
    ("Private stuff #VAULT", ["#vault"], True),
    ("#private", ["#vault", "#private"], True),
    ("#private", ["#vault"], False),  # alias disabled
    ("#vaulted is not the marker", ["#vault"], False),
    ("email me@x#vault", ["#vault"], False),
    ("##vault", ["#vault"], False),
    ("no marker here", ["#vault", "#private"], False),
    ("line one\n#vault", ["#vault"], True),
])
def test_vault_marker(text, markers, expected) -> None:
    assert router.has_marker(text, markers) is expected


def test_owner_caption_is_exact() -> None:
    raw = "  Breakfast!\n\nWith  everyone, at 9:00 :)  "
    assert router.owner_caption(raw) == raw
    assert router.owner_caption("   \n ") is None
    assert router.owner_caption("") is None


@pytest.mark.parametrize("name, expected", [
    ("../../etc/passwd", "passwd"),
    ("..\\..\\Windows\\win.ini", "win"),
    ("CON.jpg", "fallback"),
    ("Ünïcödé fïlé.jpg", "Unicode-file"),
    (None, "fallback"),
    ("....", "fallback"),
    ("a" * 200 + ".jpg", "a" * 60),
])
def test_safe_stem(name, expected) -> None:
    assert router.safe_stem(name, "fallback") == expected


def test_archive_relpath_layout() -> None:
    rel = router.archive_relpath("picture", "jpg", "beach", "ab" * 32, 2026, 3)
    assert rel.as_posix() == "originals/2026/03/beach__abababababab.jpg"


def test_archive_original_is_byte_identical_and_read_only(tmp_path: Path) -> None:
    src = write(tmp_path / "in" / "x.jpg", JPEG * 50)
    sha = archive.sha256_file(src)
    stored = archive.archive_original(src, tmp_path / "archive", Path("originals/2026/01/x__a.jpg"), sha)
    assert stored.path.read_bytes() == src.read_bytes()
    assert archive.sha256_file(stored.path) == sha
    assert not os.access(stored.path, os.W_OK) or not (stored.path.stat().st_mode & stat.S_IWUSR)
    assert archive.leftover_temp_files(tmp_path / "archive") == []


def test_archive_reuses_identical_file_after_crash(tmp_path: Path) -> None:
    src = write(tmp_path / "x.jpg", JPEG)
    sha = archive.sha256_file(src)
    rel = Path("originals/2026/01/x.jpg")
    first = archive.archive_original(src, tmp_path / "a", rel, sha)
    again = archive.archive_original(src, tmp_path / "a", rel, sha)
    assert first.path == again.path


def test_archive_never_overwrites_a_different_file(tmp_path: Path) -> None:
    rel = Path("originals/2026/01/x.jpg")
    a = write(tmp_path / "a.jpg", JPEG)
    b = write(tmp_path / "b.jpg", JPEG + b"different")
    archive.archive_original(a, tmp_path / "arc", rel, archive.sha256_file(a))
    with pytest.raises(archive.ArchiveError, match="different file"):
        archive.archive_original(b, tmp_path / "arc", rel, archive.sha256_file(b))


def test_archive_rejects_hash_mismatch_and_leaves_no_temp(tmp_path: Path) -> None:
    src = write(tmp_path / "x.jpg", JPEG)
    with pytest.raises(archive.ArchiveError, match="does not match"):
        archive.archive_original(src, tmp_path / "arc", Path("originals/x.jpg"), "0" * 64)
    assert archive.leftover_temp_files(tmp_path / "arc") == []
    assert not (tmp_path / "arc" / "originals" / "x.jpg").exists()


def test_archive_refuses_path_escape(tmp_path: Path) -> None:
    src = write(tmp_path / "x.jpg", JPEG)
    with pytest.raises(archive.ArchiveError, match="escaped"):
        archive.archive_original(src, tmp_path / "arc", Path("../outside.jpg"), archive.sha256_file(src))


def test_sidecar_is_atomic_json(tmp_path: Path) -> None:
    import json

    asset = write(tmp_path / "originals" / "x__abc.jpg", JPEG)
    path = archive.write_sidecar(asset, "f" * 64, "Line one\nLine two", "owner", "2026-10-01T00:00:00+00:00",
                                 False, {"text_model": "m"})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "x__abc.jpg.json"
    assert data == {"schema_version": 1, "asset_sha256": "f" * 64, "caption": "Line one\nLine two",
                    "caption_source": "owner", "approved_at": "2026-10-01T00:00:00+00:00",
                    "include_location": False, "models": {"text_model": "m"}}
    assert "transcript" not in data
    assert list(tmp_path.rglob(".sidecar-*")) == []


def test_remove_job_temp_refuses_outside_paths(tmp_path: Path) -> None:
    temp_root = tmp_path / "tmp"
    job = temp_root / "job-1"
    job.mkdir(parents=True)
    archive.remove_job_temp(temp_root, str(job))
    assert not job.exists()
    with pytest.raises(archive.ArchiveError):
        archive.remove_job_temp(temp_root, str(tmp_path / "archive"))


def test_check_space(tmp_path: Path) -> None:
    archive.check_space(tmp_path, 10, 0)
    with pytest.raises(archive.ArchiveError, match="Not enough disk space"):
        archive.check_space(tmp_path, 10**18, 0)


def test_metadata_copy_never_touches_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "arc"
    src = write(tmp_path / "x.jpg", JPEG)
    sha = archive.sha256_file(src)
    stored = archive.archive_original(src, root, Path("originals/2026/01/x.jpg"), sha)
    calls = []

    def fake_run(args, timeout=120, check=True):
        calls.append(args)
        if "-s3" in args:
            return archive.tools.ToolResult(0, "Caption\n", "")
        Path(args[-1]).write_bytes(JPEG + b"with-metadata")  # exiftool rewrites the copy only
        return archive.tools.ToolResult(0, "", "")

    monkeypatch.setattr(archive.tools, "run", fake_run)
    dest = archive.write_metadata_copy(stored.path, root, "Caption", sha, tmp_path / "work")
    assert dest == root / "enriched" / "2026" / "01" / "x.jpg"
    assert archive.sha256_file(stored.path) == sha
    assert dest.read_bytes().endswith(b"with-metadata")
    assert all(str(stored.path) not in a for a in calls)


@pytest.mark.local_tools
def test_metadata_copy_with_real_exiftool(tmp_path: Path) -> None:
    if archive.tools.which("exiftool") is None:
        pytest.skip("ExifTool is not installed")
    from PIL import Image

    pic = tmp_path / "p.jpg"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(pic)
    sha = archive.sha256_file(pic)
    stored = archive.archive_original(pic, tmp_path / "arc", Path("originals/2026/01/p.jpg"), sha)
    dest = archive.write_metadata_copy(stored.path, tmp_path / "arc", "Sunny day\nAt the park", sha, tmp_path / "w")
    assert dest.exists() and archive.sha256_file(stored.path) == sha
