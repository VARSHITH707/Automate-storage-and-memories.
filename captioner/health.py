"""Status, weekly health report and the three-day inactivity alert. Never includes captions or media."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import jobs

PRE_ARCHIVE = ("new", "downloaded")
PENDING_REVIEW = ("captioning", "awaiting_approval")


def _one(conn: sqlite3.Connection, sql: str, *args: object) -> object:
    return conn.execute(sql, args).fetchone()[0]


def last_archive_at(conn: sqlite3.Connection) -> datetime | None:
    return jobs.parse_iso(_one(conn, "SELECT MAX(archived_at) FROM assets"))  # type: ignore[arg-type]


def status_text(conn: sqlite3.Connection) -> str:
    counts = jobs.counts_by_state(conn)
    lines = ["Jobs by state:"]
    lines += [f"  {state:20} {n}" for state, n in sorted(counts.items())] or ["  (none)"]
    total = _one(conn, "SELECT COUNT(*) FROM assets")
    lines.append(f"Archived originals: {total}")
    last = last_archive_at(conn)
    lines.append(f"Last archive: {jobs.iso(last) if last else 'never'}")
    checkpoints = conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'checkpoint:%'").fetchall()
    for row in checkpoints:
        lines.append(f"Inbox checkpoint: message {row['value']}")
    waiting = conn.execute("SELECT id, kind, needs_manual FROM jobs WHERE state = 'awaiting_approval' "
                           "ORDER BY id").fetchall()
    if waiting:
        lines.append("Waiting for your review (reply in the bot chat, or `captioner review JOB_ID`):")
        lines += [f"  job {r['id']} {r['kind']}{' (needs your caption)' if r['needs_manual'] else ''}"
                  for r in waiting]
    problems = conn.execute("SELECT id, state, error_category, error_message FROM jobs "
                            "WHERE state IN ('failed', 'upload_unknown') ORDER BY id").fetchall()
    if problems:
        lines.append("Needs attention (`captioner retry JOB_ID`):")
        lines += [f"  job {r['id']} {r['state']}: {r['error_category'] or ''} {r['error_message'] or ''}".rstrip()
                  for r in problems]
    backups = dict(conn.execute("SELECT state, COUNT(*) FROM backup_jobs GROUP BY state").fetchall())
    if backups:
        lines.append(f"Backup queue: {backups}")
    return "\n".join(lines)


def report_due(conn: sqlite3.Connection, now: datetime, interval_days: int) -> bool:
    """True once per interval. The first call only starts the clock (no report right after install)."""
    last = jobs.parse_iso(jobs.get_state(conn, "last_report_at"))
    if last is None:
        jobs.set_state(conn, "last_report_at", jobs.iso(now))
        return False
    return now - last >= timedelta(days=interval_days)


def weekly_report(conn: sqlite3.Connection, now: datetime, integrations: dict[str, str]) -> str:
    week_ago = jobs.iso(now - timedelta(days=7))
    counts = jobs.counts_by_state(conn)
    archived_week = _one(conn, "SELECT COUNT(*) FROM assets WHERE archived_at >= ?", week_ago)
    last = last_archive_at(conn)
    backups = dict(conn.execute("SELECT state, COUNT(*) FROM backup_jobs GROUP BY state").fetchall())
    total = _one(conn, "SELECT COUNT(*) FROM assets")
    uploaded = _one(conn, "SELECT COUNT(*) FROM uploads WHERE state = 'done'")
    lines = [
        "Weekly health report",
        f"Originals archived: {total} total, {archived_week} this week",
        f"Last archive: {jobs.iso(last) if last else 'never'}",
        f"Waiting for your review: {sum(counts.get(s, 0) for s in PENDING_REVIEW)}",
        f"Videos uploaded (private): {uploaded}",
        f"Saved to Vault: {counts.get('vaulted', 0)}; duplicates skipped: {counts.get('duplicate', 0)}",
        f"Failed: {counts.get('failed', 0)}; upload unknown: {counts.get('upload_unknown', 0)}",
        f"Backup queue: {backups.get('pending', 0)} pending, {backups.get('failed', 0)} failed, "
        f"{backups.get('done', 0)} verified" if backups else "Backup: off",
    ]
    lines += [f"{name}: {state}" for name, state in integrations.items()]
    lines.append("The app only works while the laptop is on; anything sent while it was off is caught up on start.")
    return "\n".join(lines)


def inactivity_alert(conn: sqlite3.Connection, now: datetime, days: int) -> str | None:
    """Alert when Inbox posts are waiting but nothing was archived for `days`. Silent when there are no posts."""
    cutoff = jobs.iso(now - timedelta(days=days))
    waiting = _one(conn,
                   "SELECT COUNT(*) FROM jobs j JOIN inbox_messages m ON m.id = j.inbox_message_id "
                   "WHERE (j.state IN ('new', 'downloaded') OR (j.state = 'failed' AND j.failed_from IN "
                   "('new', 'downloaded'))) AND m.registered_at <= ?", cutoff)
    if not waiting:
        return None
    last = last_archive_at(conn)
    if last is not None and now - last < timedelta(days=days):
        return None
    last_alert = jobs.parse_iso(jobs.get_state(conn, "last_inactivity_alert_at"))
    if last_alert is not None and now - last_alert < timedelta(days=1):
        return None  # at most one alert per day
    jobs.set_state(conn, "last_inactivity_alert_at", jobs.iso(now))
    since = jobs.iso(last) if last else "never"
    return (f"Attention: {waiting} Inbox item(s) have waited more than {days} days and nothing has been "
            f"archived since {since}. Run `captioner doctor` and `captioner status` on the laptop.")
