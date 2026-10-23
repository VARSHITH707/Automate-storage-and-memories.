"""Orchestration: moves each job through the state machine.

External effects (download, Vault copy, model calls, upload) happen first; the state change is then
committed with a compare-and-set, so a crash or retry never repeats a finished step. Adapters are
injected, so tests and the demo run the same code with fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from telethon import errors as tg_errors

from . import approval, archive, health, jobs, router, tools
from .bot import Reply
from .captions import CaptionResult, CaptionUnavailableError
from .config import Config
from .jobs import IncomingMessage, StaleStateError, iso, parse_iso, tx
from .paths import Paths
from .youtube import UploadDone, UploadFailed, UploadOutcome, UploadRetry, UploadUnknown

log = logging.getLogger("captioner.pipeline")
PRE_REVIEW = ("new", "downloaded", "archived", "captioning")
WORK_STATES = ("new", "downloaded", "archived", "captioning", "approved", "uploading")
MAX_VISITS = 2


class Source(Protocol):
    async def scan(self, min_id: int) -> list[IncomingMessage]: ...
    async def download(self, message_id: int, dest_dir: Path) -> Path: ...
    async def find_in_vault(self, tag: str) -> int | None: ...
    async def send_to_vault(self, path: Path, tag: str) -> int: ...


class Messenger(Protocol):
    async def send(self, text: str) -> int: ...


class Captioner(Protocol):
    def caption(self, kind: str, path: Path, work: Path) -> CaptionResult: ...
    def rewrite(self, kind: str, path: Path, work: Path, current: str | None,
                instruction: str) -> CaptionResult: ...


class Uploader(Protocol):
    def upload(self, job_id: int, path: Path, title: str, description: str, store: Any) -> UploadOutcome: ...


class Backup(Protocol):
    destination: str

    def copy(self, asset: Path, sha256: str) -> None: ...


@dataclass
class Services:
    source: Source
    messenger: Messenger
    captioner: Captioner
    uploader: Uploader | None = None
    backup: Backup | None = None
    exiftool_ok: bool = False


def classify_error(exc: BaseException) -> tuple[str, bool, float | None]:
    """(category, retryable, delay seconds)."""
    if isinstance(exc, tg_errors.FloodWaitError):
        return "flood_wait", True, float(exc.seconds)
    if isinstance(exc, CaptionUnavailableError):
        return "model_unavailable", True, None
    if isinstance(exc, ConnectionError | TimeoutError | asyncio.TimeoutError):
        return "network", True, None
    if isinstance(exc, archive.ArchiveError):
        return "archive", False, None
    if isinstance(exc, FileNotFoundError):
        return "missing_source", False, None
    if isinstance(exc, tools.ToolError):
        return "tool", "not installed" not in str(exc), None
    if isinstance(exc, OSError):
        return "io", True, None
    return "internal", False, None


class DbUploadStore:
    """Upload progress, written from the upload thread through its own connection (WAL allows it)."""

    def __init__(self, db_path: Path, clock: Callable[[], datetime]):
        self.db_path, self.clock = db_path, clock
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = jobs.connect(self.db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def latest(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM uploads WHERE job_id = ? ORDER BY attempt DESC LIMIT 1",
                                (job_id,)).fetchone()
        return dict(row) if row else None

    def start_attempt(self, job_id: int, total: int) -> int:
        now = iso(self.clock())
        attempt = self.conn.execute("SELECT COALESCE(MAX(attempt), 0) + 1 FROM uploads WHERE job_id = ?",
                                    (job_id,)).fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO uploads(job_id, attempt, state, total_bytes, started_at, updated_at) "
            "VALUES (?, ?, 'in_progress', ?, ?, ?)", (job_id, attempt, total, now, now))
        assert cur.lastrowid is not None
        return cur.lastrowid

    def save_session(self, upload_id: int, uri: str) -> None:
        self.conn.execute("UPDATE uploads SET session_uri = ?, updated_at = ? WHERE id = ?",
                          (uri, iso(self.clock()), upload_id))

    def save_progress(self, upload_id: int, sent: int, final_chunk_sent: bool) -> None:
        self.conn.execute("UPDATE uploads SET bytes_sent = ?, final_chunk_sent = MAX(final_chunk_sent, ?), "
                          "updated_at = ? WHERE id = ?", (sent, int(final_chunk_sent), iso(self.clock()), upload_id))

    def finish(self, upload_id: int, video_id: str, privacy: str | None) -> None:
        self.conn.execute("UPDATE uploads SET state = 'done', video_id = ?, privacy_status = ?, "
                          "privacy_verified = ?, updated_at = ? WHERE id = ?",
                          (video_id, privacy, int(privacy == "private"), iso(self.clock()), upload_id))

    def mark(self, upload_id: int, state: str, error: str = "") -> None:
        self.conn.execute("UPDATE uploads SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                          (state, error[:300], iso(self.clock()), upload_id))


class Pipeline:
    def __init__(self, conn: sqlite3.Connection, db_path: Path, cfg: Config, paths: Paths, services: Services,
                 clock: Callable[[], datetime] = jobs.utcnow, rand: Callable[[], float] = random.random):
        self.conn, self.cfg, self.paths, self.s = conn, cfg, paths, services
        self.clock, self.rand = clock, rand
        self.archive_root = cfg.archive_root(paths.home)
        self.upload_store = DbUploadStore(db_path, clock)

    # ------------------------------------------------------------------ messages to the owner
    async def say(self, text: str) -> int | None:
        try:
            return await self.s.messenger.send(text)
        except Exception as exc:  # noqa: BLE001 - a bot outage must never stop archiving
            log.warning("Could not message the owner: %s", type(exc).__name__)
            return None

    # ------------------------------------------------------------------ intake
    @property
    def checkpoint_key(self) -> str:
        return f"checkpoint:{self.cfg.telegram.inbox_id}"

    def accept(self, msg: IncomingMessage) -> bool:
        """Owner-only intake: the configured Inbox, never bots, and the owner as sender in a group Inbox."""
        if msg.chat_id != self.cfg.telegram.inbox_id or msg.via_bot or msg.sender_is_bot:
            return False
        return not (self.cfg.telegram.inbox_kind == "group" and msg.sender_id != self.cfg.telegram.owner_id)

    def _register(self, msg: IncomingMessage) -> int | None:
        if not self.accept(msg):
            log.info("Ignored message %s (not from the owner's Inbox)", msg.message_id)
            return None
        kind = router.classify(msg)
        return jobs.register_message(self.conn, msg, kind, router.maybe_compressed(msg), self.clock())

    async def scan(self) -> int:
        """Catch up from the stored checkpoint. Messages are registered and the checkpoint advanced in
        the same transaction, so the checkpoint never passes an unregistered message."""
        checkpoint = int(jobs.get_state(self.conn, self.checkpoint_key) or 0)
        messages = await self.s.source.scan(checkpoint)
        new = 0
        with tx(self.conn):
            for msg in messages:
                if self._register(msg) is not None:
                    new += 1
            if messages:
                jobs.set_state(self.conn, self.checkpoint_key, str(max(checkpoint, *(m.message_id for m in messages))))
        return new

    async def safe_scan(self) -> int:
        """A scan that never stops the run loop: a network or flood-wait problem is logged and the
        next periodic scan tries again from the same checkpoint."""
        try:
            return await self.scan()
        except Exception as exc:  # noqa: BLE001 - the checkpoint did not move, so nothing is lost
            category, _, delay = classify_error(exc)
            log.warning("Inbox scan failed (%s); will try again%s", category,
                        f" after {delay:.0f} s" if delay else "")
            return 0

    async def register_live(self, msg: IncomingMessage) -> bool:
        """Live event: same idempotent registration, but the checkpoint is only moved by scans."""
        with tx(self.conn):
            return self._register(msg) is not None

    # ------------------------------------------------------------------ processing loop
    def _due_jobs(self) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(WORK_STATES))
        return self.conn.execute(
            f"SELECT id, state FROM jobs WHERE state IN ({marks}) "  # noqa: S608 - fixed placeholders
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id",
            (*WORK_STATES, iso(self.clock()))).fetchall()

    async def process_ready(self) -> int:
        """Advance every due job as far as it can go now. Returns the number of steps taken."""
        steps, visits = 0, Counter[tuple[int, str]]()
        while True:
            # A job may revisit a state (for example a lost temp file sends it back to download),
            # but never more than MAX_VISITS times in one pass, so a pass always ends.
            pending = [r for r in self._due_jobs() if visits[(r["id"], r["state"])] < MAX_VISITS]
            if not pending:
                break
            row = pending[0]
            visits[(row["id"], row["state"])] += 1
            job = jobs.get_job(self.conn, row["id"])
            if job is not None:
                await self._step(job)
                steps += 1
        await self.notify_pending()
        await self.dispatch_reviews()
        await self.process_backups()
        return steps

    async def _step(self, job: sqlite3.Row) -> None:
        handler = {"new": self._download, "downloaded": self._archive, "archived": self._route,
                   "captioning": self._caption, "approved": self._finalize, "uploading": self._upload}[job["state"]]
        try:
            await handler(job)
        except StaleStateError:
            log.info("Job %s moved on before this step finished", job["id"])
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job, never swallowed
            category, retryable, delay = classify_error(exc)
            log.warning("Job %s step %s failed: %s (%s)", job["id"], job["state"], category, type(exc).__name__)
            message = f"{type(exc).__name__}: {exc}"
            try:
                if retryable:
                    await self._defer(job, category, message, delay)
                else:
                    with tx(self.conn):
                        jobs.fail(self.conn, job["id"], job["state"], category, message, self.clock())
                    await self.say(f"Job {job['id']} (message {job['message_id']}) failed: {category}. "
                                   f"Run `captioner retry {job['id']}` on the laptop after fixing the cause.")
            except StaleStateError:
                # The step committed its state change before the error (for example during cleanup).
                log.info("Job %s already moved on; the error is not recorded on it", job["id"])

    async def _defer(self, job: sqlite3.Row, category: str, message: str, delay: float | None = None) -> None:
        attempt = job["retry_count"] + 1
        if attempt >= self.cfg.run.max_attempts:
            with tx(self.conn):
                jobs.fail(self.conn, job["id"], job["state"], category, message, self.clock())
            await self.say(f"Job {job['id']} gave up after {attempt} attempts ({category}). "
                           f"Run `captioner retry {job['id']}` after fixing the cause.")
            return
        wait = delay if delay is not None else min(30 * 2 ** (attempt - 1), 3600) * (0.5 + self.rand())
        with tx(self.conn):
            jobs.update_job(self.conn, job["id"], job["state"], retry_count=attempt,
                            next_attempt_at=iso(self.clock() + timedelta(seconds=wait)),
                            error_category=category, error_message=message[:500])

    def _advance(self, job: sqlite3.Row, to_state: str, **fields: Any) -> None:
        jobs.transition(self.conn, job["id"], job["state"], to_state, self.clock(), retry_count=0,
                        next_attempt_at=None, error_category=None, error_message=None, **fields)

    def _job_temp(self, job: sqlite3.Row) -> Path:
        return Path(job["temp_dir"]) if job["temp_dir"] else self.paths.temp / f"job-{job['id']}"

    def _cleanup(self, job: sqlite3.Row) -> None:
        archive.remove_job_temp(self.paths.temp, str(self._job_temp(job)))

    # ------------------------------------------------------------------ steps
    async def _download(self, job: sqlite3.Row) -> None:
        size = job["file_size"] or 0
        if size > self.cfg.archive.max_file_mb * 1024 * 1024:
            raise archive.ArchiveError(f"File is larger than archive.max_file_mb ({self.cfg.archive.max_file_mb} MB)")
        archive.check_space(self.paths.temp, size, self.cfg.archive.min_free_mb)
        temp = self._job_temp(job)
        target = temp / "download"
        shutil.rmtree(target, ignore_errors=True)  # a partial download from a crash is discarded
        path = await self.s.source.download(job["message_id"], target)
        actual = path.stat().st_size
        if actual == 0:
            raise archive.ArchiveError("Downloaded file is empty")
        if actual > self.cfg.archive.max_file_mb * 1024 * 1024:  # do not trust Telegram's reported size
            shutil.rmtree(target, ignore_errors=True)
            raise archive.ArchiveError(f"File is larger than archive.max_file_mb ({self.cfg.archive.max_file_mb} MB)")
        sniffed = router.sniff(path)
        with tx(self.conn):
            if sniffed is None:
                self._advance(job, "ignored_unsupported", notified=0)
            else:
                self._advance(job, "downloaded", temp_dir=str(temp), kind=sniffed[0])
        if sniffed is None:
            self._cleanup(job)

    async def _archive(self, job: sqlite3.Row) -> None:
        download_dir = self._job_temp(job) / "download"
        files = [p for p in download_dir.iterdir() if p.is_file()] if download_dir.exists() else []
        if len(files) != 1:
            with tx(self.conn):
                self._advance(job, "new")  # temp lost (crash or cleanup): download again
            return
        src = files[0]
        sniffed = router.sniff(src)
        if sniffed is None:
            with tx(self.conn):
                self._advance(job, "ignored_unsupported", notified=0)
            self._cleanup(job)
            return
        kind, ext = sniffed
        sha = await asyncio.to_thread(archive.sha256_file, src)
        if self._mark_duplicate(job, sha):
            shutil.rmtree(download_dir, ignore_errors=True)
            return
        posted = parse_iso(job["posted_at"]) or self.clock()
        rel = router.archive_relpath(kind, ext, router.safe_stem(job["file_name"], f"{kind}-{job['message_id']}"),
                                     sha, posted.year, posted.month)
        archive.check_space(self.archive_root, src.stat().st_size, self.cfg.archive.min_free_mb)
        stored = await asyncio.to_thread(archive.archive_original, src, self.archive_root, rel, sha)
        now = iso(self.clock())
        try:
            with tx(self.conn):
                asset_id = self.conn.execute(
                    "INSERT INTO assets(sha256, archive_path, kind, size, archived_at) VALUES (?, ?, ?, ?, ?)",
                    (sha, str(stored.path), kind, stored.size, now)).lastrowid
                self.conn.execute("UPDATE inbox_messages SET asset_id = ? WHERE id = ?",
                                  (asset_id, job["inbox_message_id"]))
                self._advance(job, "archived", asset_id=asset_id, kind=kind)
                if self.s.backup is not None:
                    self.conn.execute("INSERT OR IGNORE INTO backup_jobs(asset_id, destination, state, updated_at) "
                                      "VALUES (?, ?, 'pending', ?)", (asset_id, self.s.backup.destination, now))
        except sqlite3.IntegrityError:
            if not self._mark_duplicate(job, sha):  # same file archived by a parallel path
                raise
        shutil.rmtree(download_dir, ignore_errors=True)

    def _mark_duplicate(self, job: sqlite3.Row, sha: str) -> bool:
        existing = self.conn.execute("SELECT id FROM assets WHERE sha256 = ?", (sha,)).fetchone()
        if existing is None:
            return False
        with tx(self.conn):
            self.conn.execute("UPDATE inbox_messages SET asset_id = ? WHERE id = ?",
                              (existing["id"], job["inbox_message_id"]))
            self._advance(job, "duplicate", asset_id=existing["id"], notified=0)
        return True

    def _album_texts(self, job: sqlite3.Row) -> list[str]:
        if job["album_id"] is None:
            return [job["source_text"]]
        return [r["text"] for r in self.conn.execute("SELECT text FROM inbox_messages WHERE album_id = ?",
                                                     (job["album_id"],))]

    async def _route(self, job: sqlite3.Row) -> None:
        if job["album_id"] is not None:
            last = parse_iso(self.conn.execute("SELECT last_item_at FROM albums WHERE id = ?",
                                               (job["album_id"],)).fetchone()[0]) or self.clock()
            settle_at = last + timedelta(seconds=self.cfg.caption.album_settle_seconds)
            if self.clock() < settle_at:
                with tx(self.conn):
                    jobs.update_job(self.conn, job["id"], "archived", next_attempt_at=iso(settle_at))
                return
        if any(router.has_marker(t, self.cfg.markers) for t in self._album_texts(job)):
            await self._vault(job)
            return
        own = router.owner_caption(job["source_text"])
        with tx(self.conn):
            if own is not None:
                self._advance(job, "awaiting_approval", caption=own, caption_source="owner", needs_manual=0)
            else:
                self._advance(job, "captioning")

    async def _vault(self, job: sqlite3.Row) -> None:
        tag = job["sha256"][:16]
        message_id = await self.s.source.find_in_vault(tag)
        if message_id is None:
            message_id = await self.s.source.send_to_vault(Path(job["archive_path"]), tag)
        with tx(self.conn):
            self._advance(job, "vaulted", vault_message_id=message_id)
        self._cleanup(job)
        await self.say(f"Saved to Vault (Inbox message {job['message_id']}). #vault items get no caption "
                       "and no upload.")

    async def _caption(self, job: sqlite3.Row) -> None:
        work = self._job_temp(job) / "work"
        path = Path(job["archive_path"])
        instruction = job["rewrite_instruction"]
        try:
            if instruction:
                result = await asyncio.to_thread(self.s.captioner.rewrite, job["kind"], path, work, job["caption"],
                                                 instruction)
            else:
                result = await asyncio.to_thread(self.s.captioner.caption, job["kind"], path, work)
        except CaptionUnavailableError:
            if job["retry_count"] + 1 < self.cfg.run.max_attempts:
                raise
            result = CaptionResult(job["caption"], None, {}, "local models unavailable")
        except Exception as exc:  # noqa: BLE001 - a broken file must still reach the owner for a manual caption
            log.warning("Captioning job %s failed (%s); asking the owner instead", job["id"], type(exc).__name__)
            result = CaptionResult(job["caption"], None, {}, "automatic caption failed")
        source = result.source or job["caption_source"] if result.caption else None
        meta = json.dumps(result.model_meta) if result.source else job["model_meta"]
        with tx(self.conn):
            self._advance(job, "awaiting_approval", caption=result.caption, caption_source=source,
                          needs_manual=int(result.caption is None), model_meta=meta,
                          rewrite_instruction=None, question_id=None)
        if instruction and result.source is None:
            await self.say(f"Could not rewrite job {job['id']}; the previous caption is kept.")

    # ------------------------------------------------------------------ review
    async def dispatch_reviews(self) -> None:
        rows = self.conn.execute("SELECT id, album_id FROM jobs WHERE state = 'awaiting_approval' "
                                 "AND question_id IS NULL ORDER BY id").fetchall()
        groups: dict[tuple[str, int], None] = {}
        for r in rows:
            groups[("album", r["album_id"]) if r["album_id"] is not None else ("job", r["id"])] = None
        marks = ",".join("?" * len(PRE_REVIEW))
        for scope, key in groups:
            if scope == "album":
                busy = self.conn.execute(f"SELECT COUNT(*) FROM jobs WHERE album_id = ? AND state IN ({marks})",  # noqa: S608
                                         (key, *PRE_REVIEW)).fetchone()[0]
                if busy:
                    continue
                ids = [r["id"] for r in self.conn.execute(
                    "SELECT j.id FROM jobs j JOIN inbox_messages m ON m.id = j.inbox_message_id "
                    "WHERE j.album_id = ? AND j.state = 'awaiting_approval' ORDER BY m.message_id", (key,))]
            else:
                ids = [key]
            rows = [r for r in (jobs.get_job(self.conn, i) for i in ids) if r is not None]
            items = [self._review_item(n, r) for n, r in enumerate(rows, start=1)]
            if not items:
                continue
            question_id = await self.say(approval.render_question(items))
            if question_id is None:
                continue  # bot unavailable: try again next pass
            try:
                with tx(self.conn):
                    for item in items:
                        jobs.update_job(self.conn, item.job_id, "awaiting_approval", question_id=question_id,
                                        item_no=item.number)
            except StaleStateError:
                # An item changed while the question was being sent (for example approved from the CLI).
                # The whole update rolled back, so the album is asked again, renumbered, on the next pass.
                log.info("Review for job(s) %s changed while sending; it will be re-sent", [i.job_id for i in items])

    def _review_item(self, number: int, job: sqlite3.Row) -> approval.ReviewItem:
        posted = parse_iso(job["posted_at"])
        return approval.ReviewItem(
            number=number, job_id=job["id"], kind=job["kind"], file_name=job["file_name"] or job["kind"],
            message_id=job["message_id"], posted=posted.date().isoformat() if posted else "unknown date",
            caption=job["caption"], maybe_compressed=bool(job["maybe_compressed"]),
            rewrites_left=max(self.cfg.caption.max_rewrites - job["rewrite_count"], 0))

    async def handle_reply(self, reply: Reply) -> None:
        owner = self.cfg.telegram.owner_id
        if owner == 0 or not reply.is_private or reply.sender_is_bot or reply.sender_id != owner:
            log.info("Ignored a bot-chat message that was not from the owner")
            return
        if reply.text.strip().startswith("/start"):
            await self.say("Hello. Review requests for your photos and videos will appear here.")
            return
        if reply.reply_to is None:
            await self.say("Please reply to a review message (tap Reply on it), so I know which item you mean.")
            return
        items = self.conn.execute("SELECT * FROM jobs WHERE question_id = ? AND state = 'awaiting_approval' "
                                  "ORDER BY item_no", (reply.reply_to,)).fetchall()
        if not items:
            await self.say("That review is no longer active. Please reply to the most recent review message.")
            return
        numbered = {r["item_no"]: r for r in items}
        is_album = max(numbered) > 1
        command = approval.parse_reply(reply.text, max(numbered) if is_album else 1)
        if isinstance(command, approval.Rewrite | approval.Replace) and command.item not in numbered:
            await self.say(f"Item {command.item} is no longer waiting for review. Nothing was changed.")
            return
        try:
            await self._apply(command, items, numbered, is_album, reply)
        except StaleStateError:
            await self.say("That review changed while I was working. Please reply to the newest review message.")

    async def _apply(self, command: approval.Command, items: list[sqlite3.Row], numbered: dict[int, sqlite3.Row],
                     is_album: bool, reply: Reply) -> None:
        now = self.clock()
        if isinstance(command, approval.Approve):
            missing = [r["item_no"] for r in items if not r["caption"]]
            if missing:
                await self.say(f"Item(s) {', '.join(map(str, missing))} have no caption yet. "
                               "Reply with N: <caption> first.")
                return
            with tx(self.conn):
                for r in items:
                    self._approve(r, r["caption"], r["caption_source"], reply, now)
            await self.say(f"Approved {len(items)} item(s). Thank you.")
        elif isinstance(command, approval.Replace):
            target = numbered[command.item]
            with tx(self.conn):
                if not is_album:
                    self._approve(target, command.text, "owner_reply", reply, now)
                else:
                    for r in items:
                        jobs.update_job(self.conn, r["id"], "awaiting_approval", question_id=None)
                    jobs.update_job(self.conn, target["id"], "awaiting_approval", caption=command.text,
                                    caption_source="owner_reply", needs_manual=0)
            if not is_album:
                await self.say("Saved your caption exactly as written and approved it.")
        elif isinstance(command, approval.Rewrite):
            target = numbered[command.item]
            if target["rewrite_count"] >= self.cfg.caption.max_rewrites:
                await self.say(f"Item {command.item} reached the limit of {self.cfg.caption.max_rewrites} AI rewrites. "
                               f"Reply with {command.item}: <exact caption>, or run "
                               f"`captioner review {target['id']}` on the laptop.")
                return
            with tx(self.conn):
                for r in items:
                    if r["id"] != target["id"]:
                        jobs.update_job(self.conn, r["id"], "awaiting_approval", question_id=None)
                jobs.transition(self.conn, target["id"], "awaiting_approval", "captioning", now,
                                rewrite_instruction=command.instruction,
                                rewrite_count=target["rewrite_count"] + 1, question_id=None)
            await self.say(f"Rewriting item {command.item}. A new review will follow.")
        else:
            await self.say(f"{command.reason} Nothing was changed.\n"
                           "Reply with: ok, change N: <what to change>, or N: <exact caption>.")

    def _approve(self, job: sqlite3.Row, caption: str, source: str, reply: Reply, now: datetime) -> None:
        """Snapshot the exact caption shown to the owner before anything external happens."""
        self.conn.execute("INSERT INTO approvals(job_id, caption, caption_source, owner_id, question_id, "
                          "approved_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (job["id"], caption, source, reply.sender_id, reply.reply_to, iso(now)))
        jobs.transition(self.conn, job["id"], "awaiting_approval", "approved", now, caption=caption,
                        caption_source=source, needs_manual=0)

    # ------------------------------------------------------------------ after approval
    def _approved_caption(self, job: sqlite3.Row) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM approvals WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                                (job["id"],)).fetchone()
        if row is None:
            raise RuntimeError(f"job {job['id']} is approved but has no approval record")
        return row

    async def _finalize(self, job: sqlite3.Row) -> None:
        appr = self._approved_caption(job)
        asset = Path(job["archive_path"])
        meta = json.loads(job["model_meta"]) if job["model_meta"] else {}
        await asyncio.to_thread(archive.write_sidecar, asset, job["sha256"], appr["caption"], appr["caption_source"],
                                appr["approved_at"], self.cfg.caption.include_location,
                                meta if appr["caption_source"] == "generated" else {})
        if job["kind"] == "picture" and self.cfg.caption.write_metadata_copy and self.s.exiftool_ok:
            try:
                await asyncio.to_thread(archive.write_metadata_copy, asset, self.archive_root, appr["caption"],
                                        job["sha256"], self._job_temp(job) / "meta")
            except (tools.ToolError, archive.ArchiveError, OSError) as exc:
                log.warning("Metadata copy failed for job %s: %s", job["id"], type(exc).__name__)
                await self.say(f"Job {job['id']}: the metadata copy could not be written; the caption is safe "
                               "in the JSON sidecar.")
        upload = job["kind"] == "video" and self.cfg.youtube.enabled and self.s.uploader is not None
        with tx(self.conn):
            self._advance(job, "uploading" if upload else "done")
        if not upload:
            self._cleanup(job)

    async def _upload(self, job: sqlite3.Row) -> None:
        assert self.s.uploader is not None
        appr = self._approved_caption(job)
        posted = parse_iso(job["posted_at"]) or parse_iso(job["archived_at"]) or self.clock()
        title = approval.youtube_title(appr["caption"], posted.date().isoformat())
        outcome = await asyncio.to_thread(self.s.uploader.upload, job["id"], Path(job["archive_path"]), title,
                                          approval.youtube_description(appr["caption"]), self.upload_store)
        if isinstance(outcome, UploadDone):
            with tx(self.conn):
                self._advance(job, "done")
            self._cleanup(job)
            if outcome.privacy_status == "private":
                await self.say(f"Uploaded privately: https://youtu.be/{outcome.video_id}")
            else:
                await self.say(f"WARNING: YouTube reported privacy '{outcome.privacy_status}' for "
                               f"https://youtu.be/{outcome.video_id}. Open YouTube Studio and set it to Private now.")
        elif isinstance(outcome, UploadUnknown):
            with tx(self.conn):
                jobs.transition(self.conn, job["id"], "uploading", "upload_unknown", self.clock(),
                                error_category="upload_unknown", error_message=outcome.reason)
            await self.say(f"Job {job['id']}: I cannot tell whether YouTube received the video. Check YouTube "
                           f"Studio, then run `captioner retry {job['id']}` on the laptop.")
        elif isinstance(outcome, UploadRetry):
            await self._defer(job, outcome.category, outcome.message, outcome.delay_s)
        elif isinstance(outcome, UploadFailed):
            with tx(self.conn):
                jobs.fail(self.conn, job["id"], "uploading", outcome.category, outcome.message, self.clock())
            await self.say(f"Job {job['id']}: upload failed. {outcome.message}")

    # ------------------------------------------------------------------ notices, backup, health
    async def notify_pending(self) -> None:
        rows = self.conn.execute(
            "SELECT j.id, j.state, j.asset_id, m.message_id FROM jobs j "
            "JOIN inbox_messages m ON m.id = j.inbox_message_id "
            "WHERE j.state IN ('duplicate', 'ignored_unsupported') AND j.notified = 0 ORDER BY j.id").fetchall()
        if not rows:
            return
        lines = []
        for r in rows:
            if r["state"] == "duplicate":
                first = self.conn.execute("SELECT archived_at FROM assets WHERE id = ?", (r["asset_id"],)).fetchone()
                lines.append(f"Message {r['message_id']}: same file as one already archived on "
                             f"{first['archived_at'][:10]}. Not processed again.")
            else:
                lines.append(f"Message {r['message_id']}: not a supported photo or video. Ignored.")
        if await self.say("\n".join(lines)) is None:
            return
        with tx(self.conn):
            for r in rows:
                jobs.update_job(self.conn, r["id"], r["state"], notified=1)

    async def process_backups(self) -> None:
        if self.s.backup is None:
            return
        due = self.conn.execute(
            "SELECT b.*, a.archive_path, a.sha256 FROM backup_jobs b JOIN assets a ON a.id = b.asset_id "
            "WHERE b.state = 'pending' AND (b.next_attempt_at IS NULL OR b.next_attempt_at <= ?) ORDER BY b.id",
            (iso(self.clock()),)).fetchall()
        for b in due:
            try:
                await asyncio.to_thread(self.s.backup.copy, Path(b["archive_path"]), b["sha256"])
            except Exception as exc:  # noqa: BLE001 - backup failures are queued, never fatal
                attempt = b["retry_count"] + 1
                gave_up = attempt >= self.cfg.run.max_attempts
                wait = min(60 * 2 ** attempt, 6 * 3600)
                self.conn.execute(
                    "UPDATE backup_jobs SET state = ?, retry_count = ?, next_attempt_at = ?, last_error = ?, "
                    "updated_at = ? WHERE id = ?",
                    ("failed" if gave_up else "pending", attempt, iso(self.clock() + timedelta(seconds=wait)),
                     f"{type(exc).__name__}: {exc}"[:300], iso(self.clock()), b["id"]))
                if gave_up:
                    await self.say(f"Backup to {self.s.backup.destination} failed {attempt} times for one file. "
                                   "The main archive is fine. See `captioner status`.")
                continue
            self.conn.execute("UPDATE backup_jobs SET state = 'done', checksum_ok = 1, last_error = NULL, "
                              "updated_at = ? WHERE id = ?", (iso(self.clock()), b["id"]))

    async def health_tick(self, integrations: dict[str, str]) -> None:
        now = self.clock()
        with tx(self.conn):
            due = health.report_due(self.conn, now, self.cfg.run.report_interval_days)
            alert = health.inactivity_alert(self.conn, now, self.cfg.run.inactivity_days)
        if due and await self.say(health.weekly_report(self.conn, now, integrations)) is not None:
            jobs.set_state(self.conn, "last_report_at", iso(now))
        if alert:
            await self.say(alert)

    def close(self) -> None:
        self.upload_store.close()
