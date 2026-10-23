"""`captioner retry` and `captioner review`: inspect and safely reopen jobs from the terminal."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import typer

from . import jobs
from .config import Config
from .jobs import iso, tx
from .paths import Paths

# Where a failed job restarts. A failure during archiving restarts from a fresh download.
RESUME_FROM = {"new": "new", "downloaded": "new", "archived": "archived", "captioning": "captioning",
               "awaiting_approval": "awaiting_approval", "approved": "approved", "uploading": "uploading"}


def _open(paths: Paths) -> sqlite3.Connection | None:
    if not paths.database.exists():
        typer.echo("No database yet.")
        return None
    return jobs.connect(paths.database)


def _describe(job: sqlite3.Row) -> None:
    typer.echo(f"Job {job['id']}: {job['kind'] or 'unknown'} from Inbox message {job['message_id']}")
    typer.echo(f"State: {job['state']}")
    if job["error_category"] or job["error_message"]:
        typer.echo(f"Last error: {job['error_category'] or ''} {job['error_message'] or ''}".rstrip())


def retry(paths: Paths, cfg: Config, job_id: int, confirm_not_uploaded: bool,
          uploaded_as: str | None = None) -> int:
    conn = _open(paths)
    if conn is None:
        return 1
    try:
        job = jobs.get_job(conn, job_id)
        if job is None:
            typer.echo(f"No job {job_id}.")
            return 1
        _describe(job)
        if job["state"] == "failed":
            target = RESUME_FROM.get(job["failed_from"] or "", "new")
            with tx(conn):
                jobs.transition(conn, job_id, "failed", target, retry_count=0, next_attempt_at=None,
                                error_category=None, error_message=None, question_id=None)
            typer.echo(f"Reopened at '{target}'. It runs on the next `captioner run`. "
                       "Uploads resume their saved session, so this cannot create a duplicate video.")
            return 0
        if job["state"] == "upload_unknown" and uploaded_as:
            return _record_found_upload(conn, job, uploaded_as)
        if job["state"] == "upload_unknown":
            return _reconcile_upload(conn, cfg, job, confirm_not_uploaded)
        typer.echo("Nothing to retry for this state.")
        return 0
    finally:
        conn.close()


def _record_found_upload(conn: sqlite3.Connection, job: sqlite3.Row, video_id: str) -> int:
    """The owner found the video in YouTube Studio: record it, never upload again."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        typer.echo("That does not look like a YouTube video id (the part after youtu.be/).")
        return 1
    now = iso(jobs.utcnow())
    with tx(conn):
        attempt = conn.execute("SELECT COALESCE(MAX(attempt), 0) + 1 FROM uploads WHERE job_id = ?",
                               (job["id"],)).fetchone()[0]
        conn.execute("INSERT INTO uploads(job_id, attempt, state, video_id, error, started_at, updated_at) "
                     "VALUES (?, ?, 'done', ?, 'recorded by owner; privacy not checked by the app', ?, ?)",
                     (job["id"], attempt, video_id, now, now))
        jobs.transition(conn, job["id"], "upload_unknown", "done", retry_count=0, error_category=None,
                        error_message=None)
    typer.echo(f"Recorded https://youtu.be/{video_id}. Make sure it is set to Private in YouTube Studio.")
    return 0


def _reconcile_upload(conn: sqlite3.Connection, cfg: Config, job: sqlite3.Row, confirmed: bool) -> int:
    latest = conn.execute("SELECT * FROM uploads WHERE job_id = ? ORDER BY attempt DESC LIMIT 1",
                          (job["id"],)).fetchone()
    if latest is not None and latest["session_uri"] and cfg.youtube.enabled:
        from .youtube import YouTubeUploader, authorized_http, build_service, load_credentials

        creds = load_credentials()
        uploader = YouTubeUploader(lambda: build_service(authorized_http(creds)), lambda: authorized_http(creds),
                                   cfg.youtube.chunk_mb * 1024 * 1024)
        state, value = uploader.query_session(latest["session_uri"], Path(job["archive_path"]).stat().st_size)
        if state == "complete":
            privacy = (value.get("status") or {}).get("privacyStatus")
            with tx(conn):
                conn.execute("UPDATE uploads SET state='done', video_id=?, privacy_status=?, privacy_verified=?, "
                             "updated_at=? WHERE id=?", (value["id"], privacy, int(privacy == "private"),
                                                         iso(jobs.utcnow()), latest["id"]))
                jobs.transition(conn, job["id"], "upload_unknown", "done")
            typer.echo(f"YouTube confirms the upload: https://youtu.be/{value['id']} (privacy: {privacy}).")
            return 0
        if state == "incomplete":
            with tx(conn):
                jobs.transition(conn, job["id"], "upload_unknown", "uploading", next_attempt_at=None, retry_count=0)
            typer.echo("The upload was not finished; it will resume from the saved position on the next run.")
            return 0
        typer.echo(f"YouTube could not confirm the earlier session ({state}).")
    if not confirmed:
        typer.echo("Open YouTube Studio and look for this video. If it is NOT there, run:\n"
                   f"  captioner retry {job['id']} --confirm-not-uploaded\n"
                   "If it IS there, nothing more is needed; leave this job as it is.")
        return 1
    with tx(conn):
        if latest is not None:
            conn.execute("UPDATE uploads SET state='abandoned', error='owner confirmed not uploaded', updated_at=? "
                         "WHERE id=?", (iso(jobs.utcnow()), latest["id"]))
        jobs.transition(conn, job["id"], "upload_unknown", "uploading", next_attempt_at=None, retry_count=0)
    typer.echo("Marked as not uploaded. A new upload starts on the next `captioner run`.")
    return 0


def review(paths: Paths, cfg: Config, job_id: int, caption: str | None, reset_rewrites: bool, show: bool) -> int:
    conn = _open(paths)
    if conn is None:
        return 1
    try:
        job = jobs.get_job(conn, job_id)
        if job is None:
            typer.echo(f"No job {job_id}.")
            return 1
        _describe(job)
        typer.echo(f"AI rewrites used: {job['rewrite_count']} of {cfg.caption.max_rewrites}")
        typer.echo("Current caption:\n" + (job["caption"] or "(none)"))
        if show:
            return 0
        if job["state"] != "awaiting_approval":
            typer.echo("Only jobs waiting for review can be changed here.")
            return 1
        if caption is None and not reset_rewrites:
            choice = typer.prompt("[a]pprove current caption, [e]nter an exact caption, [r]eset rewrite limit, "
                                  "[q]uit", default="q").strip().lower()
            if choice == "a":
                if not job["caption"]:
                    typer.echo("There is no caption to approve.")
                    return 1
                caption = job["caption"]
            elif choice == "e":
                caption = typer.prompt("Caption (exactly as it should appear)")
            elif choice == "r":
                reset_rewrites = True
            else:
                return 0
        with tx(conn):
            _reask_album(conn, job)
            if reset_rewrites and caption is None:
                jobs.update_job(conn, job_id, "awaiting_approval", rewrite_count=0, question_id=None)
            else:
                source = "owner_cli" if caption != job["caption"] else job["caption_source"]
                conn.execute("INSERT INTO approvals(job_id, caption, caption_source, owner_id, question_id, "
                             "approved_at) VALUES (?, ?, ?, ?, ?, ?)",
                             (job_id, caption, source, cfg.telegram.owner_id, job["question_id"], iso(jobs.utcnow())))
                jobs.transition(conn, job_id, "awaiting_approval", "approved", caption=caption,
                                caption_source=source, needs_manual=0,
                                model_meta=job["model_meta"] or json.dumps({}))
        typer.echo("Rewrite limit reset; a new review will be sent on the next run." if caption is None
                   else "Approved. It continues on the next `captioner run`.")
        return 0
    finally:
        conn.close()


def _reask_album(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    """Other album items get a fresh, renumbered review question."""
    if job["album_id"] is None:
        return
    conn.execute("UPDATE jobs SET question_id = NULL WHERE album_id = ? AND id != ? AND state = 'awaiting_approval'",
                 (job["album_id"], job["id"]))
