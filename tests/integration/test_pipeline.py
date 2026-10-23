from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from telethon import errors as tg_errors

from captioner import archive, demo, jobs
from captioner.bot import Reply
from captioner.captions import CaptionResult, CaptionUnavailableError
from captioner.jobs import IncomingMessage
from captioner.youtube import UploadRetry, UploadUnknown

OWNER = demo.OWNER


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path: Path):
    pipe, source, messenger, captioner, uploader, clock, cfg = demo.build(tmp_path / "home")
    cfg.caption.album_settle_seconds = 0
    yield dict(pipe=pipe, source=source, messenger=messenger, captioner=captioner, uploader=uploader, clock=clock,
               cfg=cfg, media=tmp_path / "media")
    pipe.close()
    pipe.conn.close()


def picture(folder: Path, name: str, color=(10, 20, 30)) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    Image.new("RGB", (32, 32), color).save(path)
    return path


def fake_video(folder: Path, name: str = "clip.mp4", payload: bytes = b"v") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"\x00\x00\x00\x20ftypisom" + payload * 64)
    return path


def post(env, mid: int, file: Path | None, text: str = "", grouped: int | None = None, **kw) -> IncomingMessage:
    mime = "video/mp4" if file and file.suffix == ".mp4" else "image/jpeg"
    msg = IncomingMessage(demo.INBOX, mid, demo.START, text, grouped, "document" if file else None,
                          mime if file else None, file.name if file else None, file.stat().st_size if file else None)
    msg = replace(msg, **kw)
    env["source"].post(msg, file)
    return msg


def owner_reply(text: str, to: int | None, **kw) -> Reply:
    base = dict(sender_id=OWNER, message_id=1, text=text, reply_to=to, is_private=True, sender_is_bot=False)
    base.update(kw)
    return Reply(**base)


def state(env, job_id: int) -> str:
    return jobs.get_job(env["pipe"].conn, job_id)["state"]


def job_ids(env) -> list[int]:
    return [r[0] for r in env["pipe"].conn.execute("SELECT id FROM jobs ORDER BY id")]


def last_review(env) -> int:
    return env["messenger"].reviews()[-1][0]


def process(env) -> None:
    run(env["pipe"].process_ready())


# ------------------------------------------------------------------ intake
def test_catch_up_and_live_events_share_one_idempotent_path(env) -> None:
    pipe = env["pipe"]
    m1 = post(env, 1, picture(env["media"], "a.jpg"))
    assert run(pipe.register_live(m1)) is True  # live event first
    post(env, 2, picture(env["media"], "b.jpg", (1, 1, 1)))
    assert run(pipe.scan()) == 1  # catch-up skips the already-registered message 1
    assert run(pipe.register_live(m1)) is False
    assert run(pipe.scan()) == 0
    assert jobs.get_state(pipe.conn, pipe.checkpoint_key) == "2"
    assert len(job_ids(env)) == 2


def test_checkpoint_is_only_moved_by_scans(env) -> None:
    pipe = env["pipe"]
    late = post(env, 50, picture(env["media"], "late.jpg"))
    run(pipe.register_live(late))
    assert jobs.get_state(pipe.conn, pipe.checkpoint_key) is None


def test_other_chats_bots_and_group_strangers_are_ignored(env) -> None:
    pipe, cfg = env["pipe"], env["cfg"]
    pic = picture(env["media"], "x.jpg")
    post(env, 1, pic, chat_id=-999)
    post(env, 2, pic, via_bot=True)
    post(env, 3, pic, sender_is_bot=True)
    assert run(pipe.scan()) == 0
    cfg.telegram.inbox_kind = "group"
    post(env, 4, pic, sender_id=777)
    post(env, 5, pic, sender_id=OWNER)
    assert run(pipe.scan()) == 1


def test_unsupported_files_are_reported_once_and_ignored(env) -> None:
    notes = env["media"] / "n.txt"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("hello", encoding="utf-8")
    env["source"].post(IncomingMessage(demo.INBOX, 1, demo.START, "", None, "document", "text/plain", "n.txt", 5),
                       notes)
    disguised = env["media"] / "fake.jpg"
    disguised.write_bytes(b"%PDF-1.4 not really a jpeg")
    post(env, 2, disguised)
    run(env["pipe"].scan())
    process(env)
    process(env)
    assert [state(env, j) for j in job_ids(env)] == ["ignored_unsupported", "ignored_unsupported"]
    notices = [t for _, t in env["messenger"].sent if "not a supported photo or video" in t]
    assert len(notices) == 1 and notices[0].count("Ignored") == 2
    assert env["pipe"].conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0


# ------------------------------------------------------------------ archive and duplicates
def test_original_is_archived_byte_identical_before_any_model_call(env) -> None:
    pic = picture(env["media"], "a.jpg")
    post(env, 1, pic)
    run(env["pipe"].scan())
    process(env)
    row = env["pipe"].conn.execute("SELECT * FROM assets").fetchone()
    assert Path(row["archive_path"]).read_bytes() == pic.read_bytes()
    assert row["sha256"] == archive.sha256_file(pic)
    assert Path(row["archive_path"]).is_relative_to(env["pipe"].archive_root / "originals")
    assert env["captioner"].calls == [("caption", "picture")]


def test_duplicate_hash_is_linked_and_never_uploaded_twice(env) -> None:
    clip = fake_video(env["media"])
    post(env, 1, clip)
    post(env, 2, clip)
    run(env["pipe"].scan())
    process(env)
    first, second = job_ids(env)
    assert state(env, second) == "duplicate"
    conn = env["pipe"].conn
    assert jobs.get_job(conn, second)["asset_id"] == jobs.get_job(conn, first)["asset_id"]
    linked = conn.execute("SELECT COUNT(*) FROM inbox_messages WHERE asset_id IS NOT NULL").fetchone()[0]
    assert linked == 2
    run(env["pipe"].handle_reply(owner_reply("ok", last_review(env))))
    process(env)
    assert state(env, first) == "done" and len(env["uploader"].bodies) == 1


# ------------------------------------------------------------------ vault
@pytest.mark.parametrize("text", ["#vault", "keep this #VAULT safe", "#private (alias)"])
def test_vault_items_stop_before_caption_review_and_upload(env, text) -> None:
    post(env, 1, fake_video(env["media"]), text)
    run(env["pipe"].scan())
    process(env)
    (job_id,) = job_ids(env)
    assert state(env, job_id) == "vaulted"
    assert len(env["source"].vault) == 1
    assert env["captioner"].calls == [] and env["messenger"].reviews() == [] and env["uploader"].bodies == []
    assert not (env["pipe"].paths.temp / f"job-{job_id}").exists()


def test_alias_can_be_disabled(env) -> None:
    env["cfg"].vault.aliases = []
    post(env, 1, picture(env["media"], "a.jpg"), "#private")
    run(env["pipe"].scan())
    process(env)
    assert state(env, job_ids(env)[0]) == "awaiting_approval"


def test_vault_marker_on_one_album_item_routes_whole_album(env) -> None:
    post(env, 1, picture(env["media"], "a.jpg"), "#vault", grouped=9)
    post(env, 2, picture(env["media"], "b.jpg", (2, 2, 2)), grouped=9)
    run(env["pipe"].scan())
    process(env)
    assert [state(env, j) for j in job_ids(env)] == ["vaulted", "vaulted"]


def test_vault_retry_does_not_send_twice(env) -> None:
    post(env, 1, picture(env["media"], "a.jpg"), "#vault")
    run(env["pipe"].scan())
    source = env["source"]
    original_send = source.send_to_vault

    async def send_then_crash(path, tag):
        await original_send(path, tag)
        raise ConnectionError("lost after sending")

    source.send_to_vault = send_then_crash
    process(env)
    (job_id,) = job_ids(env)
    assert state(env, job_id) == "archived" and len(source.vault) == 1
    source.send_to_vault = original_send
    env["clock"].advance(3600)
    process(env)
    assert state(env, job_id) == "vaulted" and len(source.vault) == 1  # found by tag, not sent again


# ------------------------------------------------------------------ captions and review
def test_owner_caption_is_preserved_exactly(env) -> None:
    exact = "  Grandma's 80th!\n\nCake, candles & 🎉  "
    post(env, 1, picture(env["media"], "a.jpg"), exact)
    run(env["pipe"].scan())
    process(env)
    (job_id,) = job_ids(env)
    job = jobs.get_job(env["pipe"].conn, job_id)
    assert job["caption"] == exact and job["caption_source"] == "owner"
    assert env["captioner"].calls == []
    run(env["pipe"].handle_reply(owner_reply("ok", last_review(env))))
    process(env)
    sidecar = archive.sidecar_path(Path(job["archive_path"]))
    assert json.loads(sidecar.read_text(encoding="utf-8"))["caption"] == exact


def test_album_gets_one_review_with_per_item_captions(env) -> None:
    for mid, color in ((1, (1, 1, 1)), (2, (2, 2, 2)), (3, (3, 3, 3))):
        post(env, mid, picture(env["media"], f"{mid}.jpg", color), "Item one text" if mid == 1 else "", grouped=5)
    run(env["pipe"].scan())
    process(env)
    reviews = env["messenger"].reviews()
    assert len(reviews) == 1 and reviews[0][1].startswith("Review: album of 3 items")
    rows = env["pipe"].conn.execute("SELECT item_no, caption, caption_source FROM jobs ORDER BY item_no").fetchall()
    assert [r["item_no"] for r in rows] == [1, 2, 3]
    assert rows[0]["caption"] == "Item one text" and rows[0]["caption_source"] == "owner"
    assert rows[1]["caption_source"] == "generated"


def test_album_waits_for_settle_time(env) -> None:
    env["cfg"].caption.album_settle_seconds = 30
    post(env, 1, picture(env["media"], "1.jpg"), grouped=5)
    run(env["pipe"].scan())
    process(env)
    assert state(env, job_ids(env)[0]) == "archived" and env["messenger"].reviews() == []
    env["clock"].advance(31)
    process(env)
    assert len(env["messenger"].reviews()) == 1


def album_of_two(env) -> int:
    post(env, 1, picture(env["media"], "1.jpg", (1, 1, 1)), grouped=5)
    post(env, 2, picture(env["media"], "2.jpg", (2, 2, 2)), grouped=5)
    run(env["pipe"].scan())
    process(env)
    return last_review(env)


def test_album_ok_approves_exact_displayed_captions(env) -> None:
    q = album_of_two(env)
    shown = [r["caption"] for r in env["pipe"].conn.execute("SELECT caption FROM jobs ORDER BY item_no")]
    run(env["pipe"].handle_reply(owner_reply("ok", q)))
    approved = [r["caption"] for r in env["pipe"].conn.execute("SELECT caption FROM approvals ORDER BY job_id")]
    assert approved == shown
    process(env)
    assert all(state(env, j) == "done" for j in job_ids(env))
    assert env["uploader"].bodies == []  # pictures are never uploaded


def test_album_exact_replacement_reasks_then_approves(env) -> None:
    q = album_of_two(env)
    run(env["pipe"].handle_reply(owner_reply("2: Our dog.\nAsleep again.", q)))
    process(env)
    q2 = last_review(env)
    assert q2 != q and "Our dog.\nAsleep again." in env["messenger"].sent[-1][1]
    run(env["pipe"].handle_reply(owner_reply("ok", q2)))
    second = jobs.get_job(env["pipe"].conn, job_ids(env)[1])
    assert second["state"] == "approved" and second["caption"] == "Our dog.\nAsleep again."
    assert second["caption_source"] == "owner_reply"


def test_album_rewrite_only_changes_that_item(env) -> None:
    q = album_of_two(env)
    first_before = jobs.get_job(env["pipe"].conn, job_ids(env)[0])["caption"]
    run(env["pipe"].handle_reply(owner_reply("change 2: shorter", q)))
    process(env)
    a, b = (jobs.get_job(env["pipe"].conn, j) for j in job_ids(env))
    assert a["caption"] == first_before and b["caption"] == "A short, quiet picture moment.\nKept safe."
    assert b["rewrite_count"] == 1 and env["captioner"].calls[-1] == ("rewrite", "picture")


def test_ambiguous_album_reply_changes_nothing(env) -> None:
    q = album_of_two(env)
    before = [tuple(r) for r in env["pipe"].conn.execute("SELECT state, caption, question_id FROM jobs")]
    run(env["pipe"].handle_reply(owner_reply("the second one is wrong", q)))
    after = [tuple(r) for r in env["pipe"].conn.execute("SELECT state, caption, question_id FROM jobs")]
    assert before == after and "Nothing was changed" in env["messenger"].sent[-1][1]


@pytest.mark.parametrize("bad", [
    {"sender_id": 999}, {"is_private": False}, {"sender_is_bot": True},
])
def test_replies_from_anyone_else_are_ignored(env, bad) -> None:
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    process(env)
    sent_before = len(env["messenger"].sent)
    run(env["pipe"].handle_reply(owner_reply("ok", last_review(env), **bad)))
    assert state(env, job_ids(env)[0]) == "awaiting_approval"
    assert len(env["messenger"].sent) == sent_before  # silently ignored


def test_reply_must_target_the_current_question(env) -> None:
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    process(env)
    run(env["pipe"].handle_reply(owner_reply("ok", None)))
    assert "Please reply to a review message" in env["messenger"].sent[-1][1]
    run(env["pipe"].handle_reply(owner_reply("ok", 123456)))
    assert "no longer active" in env["messenger"].sent[-1][1]
    assert state(env, job_ids(env)[0]) == "awaiting_approval"


def test_single_item_exact_replacement_is_approved_as_written(env) -> None:
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    process(env)
    run(env["pipe"].handle_reply(owner_reply("My words,\nexactly.", last_review(env))))
    job = jobs.get_job(env["pipe"].conn, job_ids(env)[0])
    assert (job["state"], job["caption"], job["caption_source"]) == ("approved", "My words,\nexactly.", "owner_reply")


def test_rewrite_limit_then_manual_replacement(env) -> None:
    env["cfg"].caption.max_rewrites = 2
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    process(env)
    for _ in range(2):
        run(env["pipe"].handle_reply(owner_reply("change: different", last_review(env))))
        process(env)
    run(env["pipe"].handle_reply(owner_reply("change: again", last_review(env))))
    assert "reached the limit of 2 AI rewrites" in env["messenger"].sent[-1][1]
    (job_id,) = job_ids(env)
    assert state(env, job_id) == "awaiting_approval" and jobs.get_job(env["pipe"].conn, job_id)["rewrite_count"] == 2
    run(env["pipe"].handle_reply(owner_reply("Final words.\nFrom me.", last_review(env))))
    assert state(env, job_id) == "approved"


def test_ok_is_refused_when_a_caption_is_missing(env) -> None:
    env["captioner"].caption = lambda kind, path, work: CaptionResult(None, None, {}, "no evidence")
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    process(env)
    assert "No caption yet" in env["messenger"].sent[-1][1]
    run(env["pipe"].handle_reply(owner_reply("ok", last_review(env))))
    assert "have no caption yet" in env["messenger"].sent[-1][1]
    assert state(env, job_ids(env)[0]) == "awaiting_approval"


def test_nothing_is_uploaded_before_approval(env) -> None:
    post(env, 1, fake_video(env["media"]))
    run(env["pipe"].scan())
    for _ in range(3):
        process(env)
    assert state(env, job_ids(env)[0]) == "awaiting_approval" and env["uploader"].bodies == []


# ------------------------------------------------------------------ upload outcomes
def approve_video(env) -> int:
    post(env, 1, fake_video(env["media"]))
    run(env["pipe"].scan())
    process(env)
    run(env["pipe"].handle_reply(owner_reply("ok", last_review(env))))
    return job_ids(env)[0]


def test_approved_video_is_uploaded_private_and_linked(env) -> None:
    job_id = approve_video(env)
    process(env)
    assert state(env, job_id) == "done"
    assert env["uploader"].bodies[0]["status"]["privacyStatus"] == "private"
    assert "Uploaded privately: https://youtu.be/DEMO" in env["messenger"].sent[-1][1]
    up = env["pipe"].conn.execute("SELECT * FROM uploads").fetchone()
    assert up["privacy_verified"] == 1 and up["video_id"].startswith("DEMO")


def test_caption_with_angle_brackets_still_uploads(env) -> None:
    post(env, 1, fake_video(env["media"]), "We <3 this beach\nSo much fun")
    run(env["pipe"].scan())
    process(env)
    run(env["pipe"].handle_reply(owner_reply("ok", last_review(env))))
    process(env)
    (job_id,) = job_ids(env)
    assert state(env, job_id) == "done"
    assert env["uploader"].bodies[0]["snippet"]["description"] == "We 3 this beach\nSo much fun"
    job = jobs.get_job(env["pipe"].conn, job_id)
    sidecar = json.loads(archive.sidecar_path(Path(job["archive_path"])).read_text(encoding="utf-8"))
    assert sidecar["caption"] == "We <3 this beach\nSo much fun"  # the archive keeps the exact words


def test_unknown_upload_waits_for_owner(env) -> None:
    env["uploader"].upload = lambda *a: UploadUnknown("cannot tell")
    job_id = approve_video(env)
    process(env)
    assert state(env, job_id) == "upload_unknown"
    process(env)  # never retried automatically
    assert state(env, job_id) == "upload_unknown"
    assert f"captioner retry {job_id}" in env["messenger"].sent[-1][1]


def test_quota_retry_is_bounded(env) -> None:
    env["uploader"].upload = lambda *a: UploadRetry("quota", "quota", delay_s=60)
    job_id = approve_video(env)
    for _ in range(env["cfg"].run.max_attempts + 1):
        process(env)
        env["clock"].advance(61)
    job = jobs.get_job(env["pipe"].conn, job_id)
    assert job["state"] == "failed" and job["error_category"] == "quota" and job["failed_from"] == "uploading"


def test_youtube_disabled_finishes_video_without_upload(env) -> None:
    env["cfg"].youtube.enabled = False
    job_id = approve_video(env)
    process(env)
    assert state(env, job_id) == "done" and env["uploader"].bodies == []


# ------------------------------------------------------------------ failures and recovery
def test_transient_download_failure_backs_off_then_recovers(env) -> None:
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    source = env["source"]
    real = source.download
    calls = {"n": 0}

    async def flaky(mid, dest):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("network down")
        return await real(mid, dest)

    source.download = flaky
    process(env)
    job = jobs.get_job(env["pipe"].conn, job_ids(env)[0])
    assert job["state"] == "new" and job["retry_count"] == 1 and job["next_attempt_at"]
    process(env)
    assert calls["n"] == 1  # not retried before its time (no tight loop)
    env["clock"].advance(3600)
    process(env)
    assert state(env, job_ids(env)[0]) == "awaiting_approval"


def test_flood_wait_uses_telegram_delay(env) -> None:
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())

    async def flood(mid, dest):
        raise tg_errors.FloodWaitError(request=None, capture=900)

    env["source"].download = flood
    process(env)
    job = jobs.get_job(env["pipe"].conn, job_ids(env)[0])
    waited = jobs.parse_iso(job["next_attempt_at"]) - env["clock"]()
    assert job["error_category"] == "flood_wait" and waited.total_seconds() == 900


def test_permanent_failure_is_inspectable(env) -> None:
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())

    async def gone(mid, dest):
        raise FileNotFoundError("message deleted")

    env["source"].download = gone
    process(env)
    job = jobs.get_job(env["pipe"].conn, job_ids(env)[0])
    assert (job["state"], job["error_category"], job["failed_from"]) == ("failed", "missing_source", "new")
    assert "captioner retry" in env["messenger"].sent[-1][1]


def test_lost_temp_file_is_downloaded_again(env) -> None:
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    (job_id,) = job_ids(env)
    job = jobs.get_job(env["pipe"].conn, job_id)
    run(env["pipe"]._download(job))
    archive.remove_job_temp(env["pipe"].paths.temp, jobs.get_job(env["pipe"].conn, job_id)["temp_dir"])
    process(env)
    assert state(env, job_id) == "awaiting_approval"


def test_crash_after_archive_rename_reuses_the_file(env) -> None:
    pic = picture(env["media"], "a.jpg")
    post(env, 1, pic)
    run(env["pipe"].scan())
    (job_id,) = job_ids(env)
    pipe = env["pipe"]
    rel = Path("originals/2026/10") / f"a__{archive.sha256_file(pic)[:12]}.jpg"
    archive.archive_original(pic, pipe.archive_root, rel, archive.sha256_file(pic))  # as if the DB commit was lost
    process(env)
    assert state(env, job_id) == "awaiting_approval"
    assert pipe.conn.execute("SELECT archive_path FROM assets").fetchone()[0].endswith(rel.name)


def test_models_unavailable_retry_then_manual_caption(env) -> None:
    def down(kind, path, work):
        raise CaptionUnavailableError("ollama down")

    env["captioner"].caption = down
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    for _ in range(env["cfg"].run.max_attempts + 1):
        process(env)
        env["clock"].advance(7200)
    job = jobs.get_job(env["pipe"].conn, job_ids(env)[0])
    assert job["state"] == "awaiting_approval" and job["needs_manual"] == 1


def test_metadata_copy_failure_keeps_sidecar(env, monkeypatch) -> None:
    env["pipe"].s.exiftool_ok = True

    def broken(*a, **k):
        raise archive.tools.ToolError("exiftool exited with 1")

    monkeypatch.setattr(archive, "write_metadata_copy", broken)
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    process(env)
    run(env["pipe"].handle_reply(owner_reply("ok", last_review(env))))
    process(env)
    job = jobs.get_job(env["pipe"].conn, job_ids(env)[0])
    assert job["state"] == "done" and archive.sidecar_path(Path(job["archive_path"])).exists()
    assert any("metadata copy could not be written" in t for _, t in env["messenger"].sent)


def test_bot_outage_never_blocks_archiving(env) -> None:
    async def down(text):
        raise ConnectionError("bot offline")

    env["messenger"].send = down
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    process(env)
    job = jobs.get_job(env["pipe"].conn, job_ids(env)[0])
    assert job["state"] == "awaiting_approval" and job["question_id"] is None  # asked again later
    assert env["pipe"].conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


# ------------------------------------------------------------------ backup and health
class FlakyBackup:
    destination = "path:test"

    def __init__(self, fail_times: int):
        self.fail_times, self.calls = fail_times, 0

    def copy(self, asset, sha):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OSError("drive unplugged")


def test_backup_failures_are_queued_and_retried(env) -> None:
    env["pipe"].s.backup = FlakyBackup(fail_times=1)
    post(env, 1, picture(env["media"], "a.jpg"))
    run(env["pipe"].scan())
    process(env)
    row = env["pipe"].conn.execute("SELECT * FROM backup_jobs").fetchone()
    assert row["state"] == "pending" and row["retry_count"] == 1 and "unplugged" in row["last_error"]
    env["clock"].advance(3600)
    process(env)
    assert tuple(env["pipe"].conn.execute("SELECT state, checksum_ok FROM backup_jobs").fetchone()) == ("done", 1)


def test_weekly_report_once_per_week_without_captions(env) -> None:
    pipe = env["pipe"]
    post(env, 1, picture(env["media"], "a.jpg"), "SECRET CAPTION TEXT")
    run(pipe.scan())
    process(env)
    run(pipe.health_tick({"Telegram": "connected"}))
    assert not any(t.startswith("Weekly health report") for _, t in env["messenger"].sent)
    env["clock"].advance(7 * 86400)
    run(pipe.health_tick({"Telegram": "connected"}))
    run(pipe.health_tick({"Telegram": "connected"}))
    reports = [t for _, t in env["messenger"].sent if t.startswith("Weekly health report")]
    assert len(reports) == 1 and "SECRET CAPTION TEXT" not in reports[0] and "Waiting for your review: 1" in reports[0]
