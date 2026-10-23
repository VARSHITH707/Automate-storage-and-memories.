from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from captioner import jobs
from tests.conftest import NOW, make_msg


def register(conn, msg, kind="picture"):
    with jobs.tx(conn):
        return jobs.register_message(conn, msg, kind, False, NOW)


def test_migration_creates_schema_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    conn = jobs.connect(db)
    assert jobs.schema_version(conn) == jobs.SCHEMA_VERSION
    conn.close()
    conn = jobs.connect(db)  # second open must not re-run migrations
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"albums", "assets", "inbox_messages", "jobs", "approvals", "uploads", "backup_jobs",
            "app_state"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_newer_schema_is_refused(conn) -> None:
    jobs.set_state(conn, "schema_version", "99")
    with pytest.raises(RuntimeError, match="newer than this app"):
        jobs.migrate(conn)


def test_register_is_idempotent(conn) -> None:
    first = register(conn, make_msg(10))
    again = register(conn, make_msg(10))
    assert first is not None and again is None
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_text_only_message_creates_no_job(conn) -> None:
    assert register(conn, make_msg(11, media=None, mime=None, file_name=None, text="hi"), kind=None) is None
    assert conn.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_unsupported_is_recorded_as_ignored(conn) -> None:
    job_id = register(conn, make_msg(12, mime="application/pdf", file_name="x.pdf"), kind="unsupported")
    assert jobs.get_job(conn, job_id)["state"] == "ignored_unsupported"


def test_album_items_share_album_but_keep_own_jobs(conn) -> None:
    a = register(conn, make_msg(20, grouped_id=777))
    b = register(conn, make_msg(21, grouped_id=777))
    ja, jb = jobs.get_job(conn, a), jobs.get_job(conn, b)
    assert ja["album_id"] == jb["album_id"] is not None
    assert a != b
    assert conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 1


def test_transition_is_compare_and_set(conn) -> None:
    job_id = register(conn, make_msg(30))
    jobs.transition(conn, job_id, "new", "downloaded", temp_dir="t")
    with pytest.raises(jobs.StaleStateError):
        jobs.transition(conn, job_id, "new", "downloaded")
    with pytest.raises(jobs.InvalidTransitionError):
        jobs.transition(conn, job_id, "downloaded", "approved")
    with pytest.raises(ValueError, match="Unknown job fields"):
        jobs.transition(conn, job_id, "downloaded", "archived", evil="x")


def test_failed_job_keeps_reason(conn) -> None:
    job_id = register(conn, make_msg(31))
    jobs.fail(conn, job_id, "new", "network", "connection reset")
    job = jobs.get_job(conn, job_id)
    assert (job["state"], job["failed_from"], job["error_category"]) == ("failed", "new", "network")


def test_transaction_rolls_back(conn) -> None:
    with pytest.raises(RuntimeError):
        with jobs.tx(conn):
            jobs.register_message(conn, make_msg(40), "picture", False, NOW)
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0] == 0


def test_asset_hash_and_path_are_unique(conn) -> None:
    conn.execute("INSERT INTO assets(sha256, archive_path, kind, size, archived_at) VALUES (?, 'a', 'picture', 1, 'x')",
                 ("a" * 64,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO assets(sha256, archive_path, kind, size, archived_at) "
                     "VALUES (?, 'b', 'picture', 1, 'x')", ("a" * 64,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO assets(sha256, archive_path, kind, size, archived_at) "
                     "VALUES (?, 'c', 'picture', 1, 'x')", ("short",))


def test_same_hash_can_arrive_under_different_messages(conn) -> None:
    a = register(conn, make_msg(50))
    b = register(conn, make_msg(51))
    assert a != b  # the dedupe happens at archive time, not at registration
