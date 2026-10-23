"""SQLite schema, migrations, the job state machine and idempotent registration."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE albums (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        grouped_id INTEGER NOT NULL,
        last_item_at TEXT NOT NULL,
        UNIQUE (chat_id, grouped_id)
    );
    CREATE TABLE assets (
        id INTEGER PRIMARY KEY,
        sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
        archive_path TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('video', 'picture')),
        size INTEGER NOT NULL,
        archived_at TEXT NOT NULL
    );
    CREATE TABLE inbox_messages (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        grouped_id INTEGER,
        album_id INTEGER REFERENCES albums(id),
        posted_at TEXT,
        text TEXT NOT NULL DEFAULT '',
        media_kind TEXT,
        mime TEXT,
        file_name TEXT,
        file_size INTEGER,
        maybe_compressed INTEGER NOT NULL DEFAULT 0,
        asset_id INTEGER REFERENCES assets(id),
        registered_at TEXT NOT NULL,
        UNIQUE (chat_id, message_id)
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        inbox_message_id INTEGER NOT NULL UNIQUE REFERENCES inbox_messages(id),
        album_id INTEGER REFERENCES albums(id),
        asset_id INTEGER REFERENCES assets(id),
        kind TEXT,
        state TEXT NOT NULL,
        caption TEXT,
        caption_source TEXT,
        model_meta TEXT,
        needs_manual INTEGER NOT NULL DEFAULT 0,
        rewrite_count INTEGER NOT NULL DEFAULT 0,
        rewrite_instruction TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        question_id INTEGER,
        item_no INTEGER,
        notified INTEGER NOT NULL DEFAULT 0,
        temp_dir TEXT,
        error_category TEXT,
        error_message TEXT,
        failed_from TEXT,
        vault_message_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX jobs_state ON jobs(state);
    CREATE INDEX jobs_question ON jobs(question_id);
    CREATE INDEX jobs_album ON jobs(album_id);
    CREATE TABLE approvals (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs(id),
        caption TEXT NOT NULL,
        caption_source TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        question_id INTEGER,
        approved_at TEXT NOT NULL
    );
    CREATE INDEX approvals_job ON approvals(job_id);
    CREATE TABLE uploads (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs(id),
        attempt INTEGER NOT NULL,
        state TEXT NOT NULL,
        session_uri TEXT,
        bytes_sent INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        final_chunk_sent INTEGER NOT NULL DEFAULT 0,
        video_id TEXT,
        privacy_status TEXT,
        privacy_verified INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (job_id, attempt)
    );
    CREATE TABLE backup_jobs (
        id INTEGER PRIMARY KEY,
        asset_id INTEGER NOT NULL REFERENCES assets(id),
        destination TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'done', 'failed')),
        checksum_ok INTEGER NOT NULL DEFAULT 0,
        retry_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        last_error TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE (asset_id, destination)
    );
    """,
}

# Allowed transitions. `failed` can only be left through `retry`, which restores `failed_from`.
TRANSITIONS: dict[str, set[str]] = {
    "new": {"downloaded", "ignored_unsupported", "failed"},
    "downloaded": {"archived", "duplicate", "ignored_unsupported", "new", "failed"},
    "archived": {"vaulted", "captioning", "awaiting_approval", "failed"},
    "captioning": {"awaiting_approval", "failed"},
    "awaiting_approval": {"captioning", "approved", "failed"},
    "approved": {"uploading", "done", "failed"},
    "uploading": {"done", "upload_unknown", "failed"},
    "upload_unknown": {"done", "uploading"},
    "failed": {"new", "archived", "captioning", "awaiting_approval", "approved", "uploading"},
}
TERMINAL = {"done", "vaulted", "duplicate", "ignored_unsupported"}
ALL_STATES = set(TRANSITIONS) | TERMINAL


JOB_FIELDS = {"asset_id", "caption", "caption_source", "model_meta", "needs_manual", "rewrite_count",
              "rewrite_instruction", "retry_count", "next_attempt_at", "question_id", "item_no", "notified",
              "temp_dir", "error_category", "error_message", "failed_from", "vault_message_id", "kind"}


class StaleStateError(RuntimeError):
    """The job was not in the expected state (another step already moved it)."""


class InvalidTransitionError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction. Rolls back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def schema_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_state'").fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT value FROM app_state WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def migrate(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"Database schema {current} is newer than this app ({SCHEMA_VERSION}); upgrade the app")
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with tx(conn):
            for statement in MIGRATIONS[version].split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO app_state(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(version),),
            )


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


@dataclass(frozen=True)
class IncomingMessage:
    """A Telegram Inbox message, already stripped of Telethon objects."""

    chat_id: int
    message_id: int
    posted_at: datetime | None
    text: str
    grouped_id: int | None
    media: str | None  # 'photo' (Telegram-compressed photo), 'document', or None
    mime: str | None
    file_name: str | None
    file_size: int | None
    has_video_attr: bool = False
    via_bot: bool = False
    sender_is_bot: bool = False
    sender_id: int | None = None


def register_message(conn: sqlite3.Connection, msg: IncomingMessage, kind: str | None,
                     maybe_compressed: bool, now: datetime) -> int | None:
    """Record one message idempotently. Returns the new job id, or None if already known or no media.

    `kind` is the pre-download guess: 'video', 'picture', 'unsupported', or None (no media).
    Must be called inside `tx`. Startup catch-up and live events both use this function.
    """
    album_id = None
    if msg.grouped_id is not None and kind is not None:
        conn.execute(
            "INSERT INTO albums(chat_id, grouped_id, last_item_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, grouped_id) DO NOTHING",
            (msg.chat_id, msg.grouped_id, iso(now)),
        )
        album_id = conn.execute(
            "SELECT id FROM albums WHERE chat_id = ? AND grouped_id = ?", (msg.chat_id, msg.grouped_id)
        ).fetchone()["id"]
    cur = conn.execute(
        "INSERT OR IGNORE INTO inbox_messages(chat_id, message_id, grouped_id, album_id, posted_at, text, "
        "media_kind, mime, file_name, file_size, maybe_compressed, registered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (msg.chat_id, msg.message_id, msg.grouped_id, album_id,
         iso(msg.posted_at) if msg.posted_at else None, msg.text, msg.media, msg.mime, msg.file_name,
         msg.file_size, int(maybe_compressed), iso(now)),
    )
    if cur.rowcount == 0 or kind is None:
        return None
    if album_id is not None:
        conn.execute("UPDATE albums SET last_item_at = ? WHERE id = ?", (iso(now), album_id))
    state = "ignored_unsupported" if kind == "unsupported" else "new"
    job = conn.execute(
        "INSERT INTO jobs(inbox_message_id, album_id, kind, state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cur.lastrowid, album_id, None if kind == "unsupported" else kind, state, iso(now), iso(now)),
    )
    return job.lastrowid


def transition(conn: sqlite3.Connection, job_id: int, from_state: str, to_state: str,
               now: datetime | None = None, **fields: Any) -> None:
    """Compare-and-set state change. Raises StaleStateError if the job moved on already."""
    if to_state not in TRANSITIONS.get(from_state, set()):
        raise InvalidTransitionError(f"{from_state} -> {to_state} is not allowed")
    unknown = set(fields) - JOB_FIELDS
    if unknown:
        raise ValueError(f"Unknown job fields: {sorted(unknown)}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    sql = f"UPDATE jobs SET state = ?, updated_at = ?{', ' + sets if sets else ''} WHERE id = ? AND state = ?"  # noqa: S608
    cur = conn.execute(sql, (to_state, iso(now or utcnow()), *fields.values(), job_id, from_state))
    if cur.rowcount != 1:
        raise StaleStateError(f"job {job_id} is no longer in state {from_state}")


def update_job(conn: sqlite3.Connection, job_id: int, state: str, **fields: Any) -> None:
    """Change fields without changing state; still guarded by the expected state."""
    unknown = set(fields) - JOB_FIELDS
    if unknown or not fields:
        raise ValueError(f"Unknown or missing job fields: {sorted(unknown)}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = conn.execute(f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ? AND state = ?",  # noqa: S608
                       (*fields.values(), iso(utcnow()), job_id, state))
    if cur.rowcount != 1:
        raise StaleStateError(f"job {job_id} is no longer in state {state}")


def fail(conn: sqlite3.Connection, job_id: int, from_state: str, category: str, message: str,
         now: datetime | None = None) -> None:
    transition(conn, job_id, from_state, "failed", now, error_category=category,
               error_message=message[:500], failed_from=from_state)


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT j.*, m.message_id, m.chat_id, m.text AS source_text, m.posted_at, m.file_name, m.mime, "
        "m.file_size, m.media_kind, m.maybe_compressed, m.grouped_id, a.archive_path, a.sha256, "
        "a.archived_at "
        "FROM jobs j JOIN inbox_messages m ON m.id = j.inbox_message_id "
        "LEFT JOIN assets a ON a.id = j.asset_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()


def counts_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}
