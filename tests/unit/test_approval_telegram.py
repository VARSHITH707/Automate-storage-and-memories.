from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.tl import types

from captioner import approval
from captioner.approval import Approve, Replace, Rewrite, Unclear
from captioner.bot import split_message
from captioner.telegram import ChannelFacts, inbox_problems, normalize, vault_problems


@pytest.mark.parametrize("text, n, expected", [
    ("ok", 1, Approve()),
    ("OK.", 3, Approve()),
    ("  okay ", 1, Approve()),
    ("change: make it shorter", 1, Rewrite(1, "make it shorter")),
    ("Change 1: warmer", 1, Rewrite(1, "warmer")),
    ("Beach day!\nWith the dog.", 1, Replace(1, "Beach day!\nWith the dog.")),
    ("A long day at the beach", 1, Replace(1, "A long day at the beach")),
    ("2: something happened today", 1, Replace(1, "2: something happened today")),
    ("1: Beach!", 1, Replace(1, "Beach!")),
    ("1:yes", 1, Replace(1, "yes")),
    ("change 2: mention the rain", 3, Rewrite(2, "mention the rain")),
    ("2: Exact words, kept.\nLine two ", 3, Replace(2, "Exact words, kept.\nLine two ")),
])
def test_parse_reply(text, n, expected) -> None:
    assert approval.parse_reply(text, n) == expected


@pytest.mark.parametrize("text, n", [
    ("change 9: x", 3), ("9: x", 3), ("looks nice", 3), ("change:", 1), ("   ", 1), ("2:   ", 3),
    ("okk", 1), ("k", 3), ("Oki!", 1), ("yes", 1), ("thanks!", 1), ("\U0001F44D", 1), ("Beach day!", 1),
])
def test_unclear_replies_change_nothing(text, n) -> None:
    assert isinstance(approval.parse_reply(text, n), Unclear)


def item(n: int, caption: str | None = "Line a\nLine b", compressed: bool = False, left: int = 5):
    return approval.ReviewItem(n, 10 + n, "picture", f"p{n}.jpg", 100 + n, "2026-10-01", caption, compressed, left)


def test_render_single_and_album() -> None:
    single = approval.render_question([item(1)])
    assert single.startswith("Review: 1 item") and "change: <what to change>" in single
    album = approval.render_question([item(1), item(2, None, True, 0)])
    assert album.startswith("Review: album of 2 items")
    assert "change 2:" in album and "No caption yet" in album
    assert "looks Telegram-compressed" in album and "rewrite limit reached" in album


def test_youtube_description_is_safe_and_bounded() -> None:
    assert approval.youtube_description("I <3 this\n<b>") == "I 3 this\nb"
    long = "é" * 4000  # 8000 bytes in UTF-8
    out = approval.youtube_description(long)
    assert len(out.encode("utf-8")) <= 5000 and out == "é" * 2500


@pytest.mark.parametrize("caption, title", [
    ("\n  First line here  \nsecond", "First line here"),
    ("<b>Tags</b> removed", "bTags/b removed"),
    ("x" * 150, "x" * 99 + "…"),
    ("   \n  ", "Memory 2026-10-01"),
])
def test_youtube_title(caption, title) -> None:
    assert approval.youtube_title(caption, "2026-10-01") == title


def test_split_message_respects_limit() -> None:
    text = "\n\n".join(["a" * 1500] * 5)
    parts = split_message(text, limit=4000)
    assert all(len(p) <= 4000 for p in parts) and "".join(parts).count("a") == 7500


def fake_message(**kw):
    base = dict(id=5, date=datetime(2026, 1, 1, tzinfo=UTC), message="hi", grouped_id=None, media=None,
                photo=None, document=None, file=None, via_bot_id=None, sender=None, sender_id=1)
    base.update(kw)
    return SimpleNamespace(**base)


def test_normalize_photo_and_document() -> None:
    photo = normalize(fake_message(photo=object(), media=object(),
                                   file=SimpleNamespace(mime_type="image/jpeg", name=None, size=10)), -100)
    assert (photo.media, photo.mime, photo.chat_id) == ("photo", "image/jpeg", -100)
    doc = types.Document(id=1, access_hash=1, file_reference=b"", date=None, mime_type="video/mp4", size=5,
                         dc_id=1, attributes=[types.DocumentAttributeVideo(duration=1, w=1, h=1)])
    video = normalize(fake_message(document=doc, media=object(),
                                   file=SimpleNamespace(mime_type="video/mp4", name="a.mp4", size=5)), -100)
    assert video.media == "document" and video.has_video_attr


def test_normalize_special_media_is_other() -> None:
    sticker = types.Document(id=1, access_hash=1, file_reference=b"", date=None, mime_type="image/webp", size=5,
                             dc_id=1, attributes=[types.DocumentAttributeSticker(alt="x",
                                                                                 stickerset=types.InputStickerSetEmpty())])
    assert normalize(fake_message(document=sticker, media=object()), 1).media == "other"
    poll = normalize(fake_message(media=types.MessageMediaGeo(geo=types.GeoPointEmpty())), 1)
    assert poll.media == "other"
    preview = normalize(fake_message(media=types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))), 1)
    assert preview.media is None


def test_normalize_flags_bots() -> None:
    m = normalize(fake_message(via_bot_id=9, sender=SimpleNamespace(bot=True)), 1)
    assert m.via_bot and m.sender_is_bot


def facts(**kw) -> ChannelFacts:
    base = dict(title="Inbox", is_broadcast=True, is_megagroup=False, has_username=False,
                admin_ids=frozenset({1}), owner_can_post=True)
    base.update(kw)
    return ChannelFacts(**base)


def test_inbox_policy_fails_closed() -> None:
    assert inbox_problems(facts(), owner_id=1) == []
    assert "other admin" in inbox_problems(facts(admin_ids=frozenset({1, 2})), 1)[0]
    assert "public" in inbox_problems(facts(has_username=True), 1)[0]
    assert "not an admin" in inbox_problems(facts(admin_ids=frozenset({2})), 1)[-1]
    assert inbox_problems(facts(is_broadcast=False, is_megagroup=True, admin_ids=frozenset({1, 7})), 1) == []
    assert "channel" in inbox_problems(facts(is_broadcast=False), 1)[0]


def test_vault_policy() -> None:
    assert vault_problems(facts()) == []
    assert vault_problems(facts(owner_can_post=False, has_username=True))
