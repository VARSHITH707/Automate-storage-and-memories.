from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from captioner import backup, health, jobs
from captioner.config import BackupConfig
from tests.conftest import NOW, make_msg


def add_job(conn, mid: int, state: str = "new", registered=NOW) -> int:
    with jobs.tx(conn):
        job_id = jobs.register_message(conn, make_msg(mid), "picture", False, registered)
        if state != "new":
            conn.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job_id))
    return job_id


def add_asset(conn, n: int, archived_at) -> None:
    conn.execute("INSERT INTO assets(sha256, archive_path, kind, size, archived_at) VALUES (?, ?, 'picture', 1, ?)",
                 (f"{n:064d}", f"p{n}", jobs.iso(archived_at)))


def test_report_clock_starts_on_first_call(conn) -> None:
    assert health.report_due(conn, NOW, 7) is False
    assert health.report_due(conn, NOW + timedelta(days=6), 7) is False
    assert health.report_due(conn, NOW + timedelta(days=7), 7) is True


def test_no_inactivity_alert_without_posts(conn) -> None:
    assert health.inactivity_alert(conn, NOW + timedelta(days=30), 3) is None


def test_inactivity_alert_when_posts_wait_and_nothing_archived(conn) -> None:
    add_job(conn, 1, "new", registered=NOW)
    add_asset(conn, 1, NOW - timedelta(days=10))
    assert health.inactivity_alert(conn, NOW + timedelta(days=2), 3) is None  # waiting less than 3 days
    alert = health.inactivity_alert(conn, NOW + timedelta(days=4), 3)
    assert alert and "1 Inbox item(s)" in alert
    assert health.inactivity_alert(conn, NOW + timedelta(days=4, hours=2), 3) is None  # once per day
    assert health.inactivity_alert(conn, NOW + timedelta(days=5, hours=1), 3) is not None


def test_no_alert_when_archive_is_recent(conn) -> None:
    add_job(conn, 1, "new", registered=NOW)
    add_asset(conn, 1, NOW + timedelta(days=3))
    assert health.inactivity_alert(conn, NOW + timedelta(days=4), 3) is None


def test_archived_posts_do_not_count_as_waiting(conn) -> None:
    add_job(conn, 1, "done", registered=NOW)
    assert health.inactivity_alert(conn, NOW + timedelta(days=10), 3) is None


def test_status_text_has_no_captions(conn) -> None:
    job_id = add_job(conn, 1, "awaiting_approval")
    conn.execute("UPDATE jobs SET caption = 'PRIVATE WORDS' WHERE id = ?", (job_id,))
    text = health.status_text(conn)
    assert "PRIVATE WORDS" not in text and f"job {job_id} picture" in text


def test_path_backup_copies_and_verifies(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    asset = root / "originals" / "2026" / "01" / "x.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"photo bytes")
    sha = backup.sha256_file(asset)
    target = tmp_path / "drive"
    target.mkdir()
    b = backup.BackupTarget(BackupConfig(enabled=True, kind="path", target=str(target)), root)
    b.copy(asset, sha)
    copied = target / "originals" / "2026" / "01" / "x.jpg"
    assert copied.read_bytes() == b"photo bytes"
    b.copy(asset, sha)  # already there and verified: no error
    assert asset.read_bytes() == b"photo bytes"


def test_path_backup_reports_missing_drive_and_bad_copy(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "archive"
    asset = root / "originals" / "x.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"data")
    missing = backup.BackupTarget(BackupConfig(True, "path", str(tmp_path / "unplugged")), root)
    with pytest.raises(backup.BackupError, match="not available"):
        missing.copy(asset, backup.sha256_file(asset))
    (tmp_path / "drive").mkdir()
    ok_target = backup.BackupTarget(BackupConfig(True, "path", str(tmp_path / "drive")), root)
    with pytest.raises(backup.BackupError, match="verification"):
        ok_target.copy(asset, "0" * 64)
    assert not (tmp_path / "drive" / "originals" / "x.jpg").exists()
    assert asset.read_bytes() == b"data"


def test_rclone_backup_verifies_hash(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "archive"
    asset = root / "originals" / "x.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"data")
    sha = backup.sha256_file(asset)
    calls = []

    def fake_run(args, timeout=0, check=True):
        calls.append(args)
        out = f"{sha}  x.jpg\n" if args[1] == "hashsum" else ""
        return backup.tools.ToolResult(0, out, "")

    monkeypatch.setattr(backup.tools, "run", fake_run)
    target = backup.BackupTarget(BackupConfig(True, "rclone", "gdrive:memories/"), root)
    target.copy(asset, sha)
    assert calls[0] == ["rclone", "copyto", str(asset), "gdrive:memories/originals/x.jpg"]
    assert calls[1][:4] == ["rclone", "hashsum", "sha256", "--download"]
    with pytest.raises(backup.BackupError, match="verification"):
        target.copy(asset, "f" * 64)
