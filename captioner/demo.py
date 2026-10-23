"""`captioner demo`: a deterministic, offline run of the real pipeline with generated media.

Telegram, the bot, the AI models and YouTube are replaced by in-memory fakes. Nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from . import archive, jobs, tools
from .bot import Reply
from .captions import CaptionResult
from .config import Config
from .jobs import IncomingMessage
from .paths import Paths
from .pipeline import Pipeline, Services
from .youtube import UploadDone, insert_body

INBOX, VAULT, OWNER = -100111, -100222, 4242
START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeSource:
    """Stands in for the owner's Telegram account."""

    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []
        self.files: dict[int, Path] = {}
        self.vault: list[tuple[str, Path]] = []

    def post(self, msg: IncomingMessage, file: Path | None = None) -> None:
        self.messages.append(msg)
        if file is not None:
            self.files[msg.message_id] = file

    async def scan(self, min_id: int) -> list[IncomingMessage]:
        return [m for m in self.messages if m.message_id > min_id]

    async def download(self, message_id: int, dest_dir: Path) -> Path:
        src = self.files[message_id]
        dest_dir.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copyfile(src, dest_dir / src.name))

    async def find_in_vault(self, tag: str) -> int | None:
        return next((9000 + i for i, (t, _) in enumerate(self.vault) if t == tag), None)

    async def send_to_vault(self, path: Path, tag: str) -> int:
        self.vault.append((tag, path))
        return 9000 + len(self.vault) - 1


class FakeMessenger:
    """Stands in for the helper bot: records what would be sent to the owner."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.next_id = 500

    async def send(self, text: str) -> int:
        self.next_id += 1
        self.sent.append((self.next_id, text))
        return self.next_id

    def reviews(self) -> list[tuple[int, str]]:
        return [(i, t) for i, t in self.sent if t.startswith("Review:")]


class FakeCaptioner:
    """Deterministic captions in place of the local models."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def caption(self, kind: str, path: Path, work: Path) -> CaptionResult:
        self.calls.append(("caption", kind))
        return CaptionResult(f"A quiet moment kept in this {kind}.\nSaved safely in the family archive.",
                             "generated", {"vision_model": "demo-fake", "text_model": "demo-fake"})

    def rewrite(self, kind: str, path: Path, work: Path, current: str | None, instruction: str) -> CaptionResult:
        self.calls.append(("rewrite", kind))
        return CaptionResult(f"A short, quiet {kind} moment.\nKept safe.", "generated",
                             {"vision_model": "demo-fake", "text_model": "demo-fake"})


@dataclass
class FakeUploader:
    """Builds the exact insert body the real uploader would send, and returns a fake video id."""

    bodies: list[dict] = field(default_factory=list)

    def upload(self, job_id: int, path: Path, title: str, description: str, store: object) -> UploadDone:
        body = insert_body(title, description)
        self.bodies.append(body)
        video_id = f"DEMO{job_id:07d}"
        upload_id = store.start_attempt(job_id, path.stat().st_size)  # type: ignore[attr-defined]
        store.finish(upload_id, video_id, body["status"]["privacyStatus"])  # type: ignore[attr-defined]
        return UploadDone(video_id, body["status"]["privacyStatus"])


def _picture(path: Path, color: tuple[int, int, int], taken: str | None = None) -> Path:
    img = Image.new("RGB", (320, 240), color)
    exif = Image.Exif()
    if taken:
        exif.get_ifd(0x8769)[36867] = taken
    img.save(path, exif=exif.tobytes() if taken else b"")
    return path


def make_media(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    media = {
        "breakfast": _picture(folder / "breakfast.jpg", (220, 160, 90), "2026:09:14 10:30:00"),
        "beach-1": _picture(folder / "beach-1.png", (60, 140, 200)),
        "beach-2": _picture(folder / "beach-2.png", (40, 120, 180)),
        "secret": _picture(folder / "document-scan.jpg", (250, 250, 250)),
    }
    notes = folder / "notes.txt"
    notes.write_text("not a photo", encoding="utf-8")
    media["notes"] = notes
    if tools.which("ffmpeg"):
        clip = folder / "garden.mp4"
        tools.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", "-pix_fmt", "yuv420p",
                   str(clip)], timeout=120)
        media["garden"] = clip
    return media


def _msg(mid: int, file: Path, text: str = "", grouped: int | None = None, media: str = "document") -> IncomingMessage:
    mime = {"jpg": "image/jpeg", "png": "image/png", "mp4": "video/mp4", "txt": "text/plain"}[file.suffix[1:]]
    return IncomingMessage(INBOX, mid, START, text, grouped, media, mime, file.name, file.stat().st_size)


def build(home: Path) -> tuple[Pipeline, FakeSource, FakeMessenger, FakeCaptioner, FakeUploader, Clock, Config]:
    paths = Paths(home)
    paths.ensure()
    cfg = Config()
    cfg.telegram.inbox_id, cfg.telegram.vault_id, cfg.telegram.owner_id = INBOX, VAULT, OWNER
    cfg.telegram.owner_dm_ok = True
    cfg.caption.album_settle_seconds = 30
    cfg.youtube.enabled, cfg.youtube.client_secret_path = True, "demo-not-used.json"
    source, messenger, captioner = FakeSource(), FakeMessenger(), FakeCaptioner()
    uploader, clock = FakeUploader(), Clock(START)
    conn = jobs.connect(paths.database)
    services = Services(source, messenger, captioner, uploader, None, exiftool_ok=tools.which("exiftool") is not None)
    return Pipeline(conn, paths.database, cfg, paths, services, clock=clock, rand=lambda: 0.5), source, messenger, \
        captioner, uploader, clock, cfg


def reply(text: str, to: int) -> Reply:
    return Reply(sender_id=OWNER, message_id=0, text=text, reply_to=to, is_private=True, sender_is_bot=False)


async def scenario(home: Path, out: list[str]) -> bool:
    pipe, source, messenger, captioner, uploader, clock, cfg = build(home)
    media = make_media(home / "demo-media")
    source.post(_msg(1, media["breakfast"], "Sunday breakfast with everyone.\nPancakes again!"), media["breakfast"])
    source.post(_msg(2, media["beach-1"], grouped=77), media["beach-1"])
    source.post(_msg(3, media["beach-2"], grouped=77), media["beach-2"])
    source.post(_msg(4, media["breakfast"]), media["breakfast"])  # same file again: duplicate
    source.post(_msg(5, media["secret"], "#vault passport scan"), media["secret"])
    source.post(_msg(6, media["notes"]), media["notes"])
    if "garden" in media:
        source.post(_msg(7, media["garden"]), media["garden"])
    out.append(f"1. Inbox has {len(source.messages)} posts (synthetic). Catch-up scan registers them.")
    out.append(f"   new jobs: {await pipe.scan()}  (a second scan finds {await pipe.scan()}: idempotent)")

    await pipe.process_ready()
    clock.advance(31)  # album settle time passes
    await pipe.process_ready()
    out.append(f"2. After processing: {jobs.counts_by_state(pipe.conn)}")
    out.append(f"   Vault copies (mocked Telegram): {len(source.vault)}; review messages sent: "
               f"{len(messenger.reviews())}")

    reviews = {text.splitlines()[0]: qid for qid, text in messenger.reviews()}
    album_q = next(q for head, q in reviews.items() if "album" in head)
    for qid, _text in messenger.reviews():
        if qid != album_q:
            await pipe.handle_reply(reply("ok", qid))
    await pipe.handle_reply(reply("change 2: make it shorter", album_q))
    await pipe.process_ready()
    new_album_q = messenger.reviews()[-1][0]
    await pipe.handle_reply(reply("ok", album_q))  # stale question: must be ignored
    await pipe.handle_reply(reply("ok", new_album_q))
    await pipe.process_ready()
    out.append("3. Owner replies (mocked): ok, change 2: ..., a stale ok (ignored), ok")
    out.append(f"   Final states: {jobs.counts_by_state(pipe.conn)}")

    ok = True
    rows = pipe.conn.execute("SELECT a.archive_path, a.sha256 FROM assets a").fetchall()
    for r in rows:
        if archive.sha256_file(Path(r["archive_path"])) != r["sha256"]:
            ok = False
    sidecars = sorted(pipe.archive_root.rglob("*.json"))
    out.append(f"4. Archived originals: {len(rows)} (hash re-checked: {'all match' if ok else 'MISMATCH'}); "
               f"sidecars: {len(sidecars)}")
    for b in uploader.bodies:
        out.append(f"   Mock YouTube insert: title={b['snippet']['title']!r} "
                   f"privacyStatus={b['status']['privacyStatus']}")
    out.append(f"   Archive folder: {pipe.archive_root}")
    counts = jobs.counts_by_state(pipe.conn)
    expected = {"vaulted": 1, "duplicate": 1, "ignored_unsupported": 1}
    ok = ok and all(counts.get(k) == v for k, v in expected.items())
    ok = ok and counts.get("done") == (4 if "garden" in media else 3)
    ok = ok and all(b["status"]["privacyStatus"] == "private" for b in uploader.bodies)
    ok = ok and len(sidecars) == counts.get("done")
    pipe.close()
    pipe.conn.close()
    return ok


def main(output: Path | None) -> int:
    home = output or Path(tempfile.mkdtemp(prefix="captioner-demo-"))
    if output is not None and output.exists() and any(output.iterdir()):
        print(f"Demo folder {output} is not empty; choose an empty or new folder.")  # noqa: T201
        return 2
    lines: list[str] = ["Private Media Captioner offline demo (all external services are mocked)"]
    ok = asyncio.run(scenario(home, lines))
    lines.append(f"Result: {'PASS' if ok else 'FAIL'}  (demo data in {home})")
    print("\n".join(lines))  # noqa: T201
    return 0 if ok else 1
