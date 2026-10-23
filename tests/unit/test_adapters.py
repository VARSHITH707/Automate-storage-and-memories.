"""Telethon adapters and the run loop, driven through fake clients (no network)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from telethon.tl import types

from captioner import config, demo, jobs, scheduler, secrets
from captioner.bot import HelperBot, Reply
from captioner.telegram import OwnerTelegram


class FakeClient:
    """Just enough of TelegramClient for the adapters."""

    def __init__(self, messages=(), entity=None, admins=(), perms=None):
        self.messages = list(messages)
        self.entity = entity
        self.admins = list(admins)
        self.perms = perms
        self.handlers: list = []
        self.sent: list = []
        self.downloads: list = []

    def on(self, event):
        def register(fn):
            self.handlers.append((event, fn))
            return fn
        return register

    def remove_event_handler(self, fn):
        self.handlers = [h for h in self.handlers if h[1] is not fn]

    async def get_entity(self, peer):
        if self.entity is None:
            raise ValueError("not cached")
        return self.entity

    def iter_dialogs(self):
        async def gen():
            yield SimpleNamespace(id=-1001, name="Inbox", entity=types.Channel(
                id=1001, title="Inbox", photo=types.ChatPhotoEmpty(), date=None, creator=True, broadcast=True))
            yield SimpleNamespace(id=-1002, name="Someone's channel", entity=types.Channel(
                id=1002, title="Other", photo=types.ChatPhotoEmpty(), date=None, broadcast=True))
        return gen()

    def iter_messages(self, entity, min_id=0, reverse=False, search=None, limit=None):
        async def gen():
            for m in self.messages:
                if m.id > min_id and (search is None or search in (m.message or "")):
                    yield m
        return gen()

    async def get_messages(self, entity, ids):
        return next((m for m in self.messages if m.id == ids), None)

    async def download_media(self, message, file):
        path = Path(file) / "photo.jpg"
        path.write_bytes(b"\xff\xd8\xff" + b"0" * 10)
        self.downloads.append(message.id)
        return str(path)

    async def send_file(self, entity, path, caption, force_document):
        self.sent.append(("file", path, caption, force_document))
        return SimpleNamespace(id=777)

    async def send_message(self, peer, text, parse_mode=None, link_preview=True):
        self.sent.append(("msg", peer, text))
        return SimpleNamespace(id=len(self.sent))

    async def get_participants(self, entity, filter=None):
        return self.admins

    async def get_permissions(self, entity, user):
        return self.perms

    async def start(self, bot_token=None):
        return self

    async def get_me(self):
        return SimpleNamespace(id=55, bot=True, username="helper_bot")

    async def disconnect(self):
        self.disconnected = True


def message(mid: int, text: str = "", grouped=None):
    return SimpleNamespace(id=mid, date=datetime(2026, 1, 1, tzinfo=UTC), message=text, grouped_id=grouped,
                           media=object(), photo=None, document=object(), via_bot_id=None, sender=None, sender_id=1,
                           file=SimpleNamespace(mime_type="image/jpeg", name=f"{mid}.jpg", size=13))


@pytest.fixture
def owner(tmp_path: Path) -> OwnerTelegram:
    o = OwnerTelegram(1, "0" * 32, tmp_path / "owner", inbox_id=-1001, vault_id=-1002)
    return o


def test_owner_scan_download_and_vault(owner: OwnerTelegram, tmp_path: Path) -> None:
    owner.client = FakeClient([message(1), message(2, "#vault x"), message(3)], entity=object())
    found = asyncio.run(owner.scan(1))
    assert [m.message_id for m in found] == [2, 3] and found[0].chat_id == -1001
    path = asyncio.run(owner.download(2, tmp_path / "dl"))
    assert path.exists() and owner.client.downloads == [2]
    assert asyncio.run(owner.send_to_vault(path, "abc123")) == 777
    assert owner.client.sent[-1][2:] == ("#pmc abc123", True)
    owner.client.messages.append(SimpleNamespace(id=9, message="#pmc abc123"))
    assert asyncio.run(owner.find_in_vault("abc123")) == 9
    with pytest.raises(FileNotFoundError):
        asyncio.run(owner.download(404, tmp_path / "dl2"))


def test_owner_channel_picker_only_lists_own_channels(owner: OwnerTelegram) -> None:
    owner.client = FakeClient()
    assert asyncio.run(owner.list_own_channels()) == [(-1001, "Inbox")]


def test_owner_channel_facts(owner: OwnerTelegram) -> None:
    channel = types.Channel(id=1001, title="Inbox", photo=types.ChatPhotoEmpty(), date=None, creator=True,
                            broadcast=True)
    owner.client = FakeClient(entity=channel, admins=[SimpleNamespace(id=1)],
                              perms=SimpleNamespace(is_creator=True))
    facts = asyncio.run(owner.channel_facts(-1001, 1))
    assert facts.is_broadcast and facts.admin_ids == frozenset({1}) and facts.owner_can_post


def test_owner_live_events_are_normalized(owner: OwnerTelegram) -> None:
    owner.client = FakeClient()
    received = []

    async def callback(msg):
        received.append(msg)

    owner.on_new_message(callback)
    (_, handler), = owner.client.handlers
    asyncio.run(handler(SimpleNamespace(message=message(5))))
    assert received[0].message_id == 5 and received[0].chat_id == -1001


def make_bot(tmp_path: Path) -> HelperBot:
    bot = HelperBot(1, "0" * 32, tmp_path / "bot", "123:abc", owner_id=42)
    bot.client = FakeClient()
    return bot


def test_bot_send_splits_and_returns_last_id(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    assert asyncio.run(bot.connect()) == 55
    last = asyncio.run(bot.send("\n\n".join(["x" * 3000] * 3)))
    assert last == 3 and all(peer == 42 for _, peer, _ in bot.client.sent)


def test_bot_reports_replies_with_sender_details(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    got: list[Reply] = []

    async def cb(reply):
        got.append(reply)

    bot.on_reply(cb)
    (_, handler), = bot.client.handlers

    async def sender():
        return SimpleNamespace(bot=False)

    event = SimpleNamespace(sender_id=42, id=8, raw_text="ok", reply_to_msg_id=3, is_private=True,
                            get_sender=sender)
    asyncio.run(handler(event))
    assert got == [Reply(42, 8, "ok", 3, True, False)]


def test_bot_wait_for_start_only_accepts_owner(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)

    async def scenario():
        waiter = asyncio.create_task(bot.wait_for_start(2))
        await asyncio.sleep(0)
        (_, handler), = bot.client.handlers
        await handler(SimpleNamespace(is_private=True, sender_id=999))  # stranger
        await handler(SimpleNamespace(is_private=True, sender_id=42))
        return await waiter

    assert asyncio.run(scenario()) is True
    assert bot.client.handlers == []  # handler removed afterwards


# ---------------------------------------------------------------- the run loop
class FakeOwner:
    def __init__(self, source: demo.FakeSource, owner_id: int):
        self.source, self.owner_id = source, owner_id
        self.disconnected = False
        self.admins = frozenset({demo.OWNER})

    async def channel_facts(self, peer, owner_id):
        from captioner.telegram import ChannelFacts

        return ChannelFacts("Inbox", True, False, False, self.admins, True)

    async def connect(self, interactive=False):
        return self.owner_id

    async def disconnect(self):
        self.disconnected = True

    def on_new_message(self, cb):
        self.cb = cb

    def __getattr__(self, name):
        return getattr(self.source, name)


class FakeBot(demo.FakeMessenger):
    def __init__(self):
        super().__init__()
        self.disconnected = False

    async def connect(self):
        return 55

    async def disconnect(self):
        self.disconnected = True

    def on_reply(self, cb):
        self.cb = cb


@pytest.fixture
def ready(paths, monkeypatch):
    cfg = config.Config()
    cfg.telegram.inbox_id, cfg.telegram.vault_id, cfg.telegram.owner_id = demo.INBOX, demo.VAULT, demo.OWNER
    cfg.telegram.owner_dm_ok = True
    cfg.caption.album_settle_seconds = 0
    for key in (secrets.API_ID, secrets.API_HASH, secrets.BOT_TOKEN):
        secrets.set_secret(key, "1" if key == secrets.API_ID else "value")
    source, bot = demo.FakeSource(), FakeBot()
    owner = FakeOwner(source, demo.OWNER)
    monkeypatch.setattr(scheduler, "OwnerTelegram", lambda *a, **k: owner)
    monkeypatch.setattr(scheduler, "HelperBot", lambda *a, **k: bot)
    monkeypatch.setattr(scheduler, "LocalCaptioner", lambda cfg: demo.FakeCaptioner())
    monkeypatch.setattr(scheduler, "_integrations", lambda cfg: {"Telegram": "connected"})
    return cfg, paths, source, owner, bot


def post_picture(source: demo.FakeSource, paths, mid: int) -> None:
    pic = paths.home / f"in-{mid}.jpg"
    Image.new("RGB", (8, 8), (mid, 0, 0)).save(pic)
    source.post(jobs.IncomingMessage(demo.INBOX, mid, demo.START, "", None, "document", "image/jpeg", pic.name,
                                     pic.stat().st_size), pic)


def test_run_once_catches_up_and_shuts_down_cleanly(ready) -> None:
    cfg, paths, source, owner, bot = ready
    post_picture(source, paths, 1)
    assert scheduler.main(cfg, paths, once=True, listen_seconds=0, verbose=False) == 0
    conn = jobs.connect(paths.database)
    assert jobs.counts_by_state(conn) == {"awaiting_approval": 1}
    assert jobs.get_state(conn, "last_heartbeat") is not None
    conn.close()
    assert owner.disconnected and bot.disconnected
    assert bot.reviews()


def test_restart_reports_downtime_and_catches_up(ready) -> None:
    cfg, paths, source, owner, bot = ready
    conn = jobs.connect(paths.database)
    jobs.set_state(conn, "last_heartbeat", "2026-01-01T00:00:00+00:00")
    conn.close()
    post_picture(source, paths, 1)
    post_picture(source, paths, 2)
    assert scheduler.main(cfg, paths, once=True, listen_seconds=0, verbose=False) == 0
    back = [t for _, t in bot.sent if t.startswith("Back online")]
    assert back and "2 new post(s)" in back[0]


def test_run_refuses_session_of_another_account(ready) -> None:
    cfg, paths, source, owner, bot = ready
    owner.owner_id = 999
    assert scheduler.main(cfg, paths, once=True, listen_seconds=0, verbose=False) == 2
    assert owner.disconnected


def test_scan_failure_does_not_stop_the_run(ready) -> None:
    cfg, paths, source, owner, bot = ready

    async def broken_scan(min_id):
        raise ConnectionError("network down")

    source.scan = broken_scan
    assert scheduler.main(cfg, paths, once=True, listen_seconds=0, verbose=False) == 0
    conn = jobs.connect(paths.database)
    assert jobs.get_state(conn, f"checkpoint:{demo.INBOX}") is None  # checkpoint untouched
    conn.close()


def test_connection_failure_exits_cleanly(ready) -> None:
    cfg, paths, source, owner, bot = ready

    async def no_network(interactive=False):
        raise ConnectionError("cannot reach Telegram token=abcdef123")

    owner.connect = no_network
    assert scheduler.main(cfg, paths, once=True, listen_seconds=0, verbose=False) == 1
    assert owner.disconnected and bot.disconnected
    log_text = (paths.logs / "captioner.log").read_text(encoding="utf-8")
    assert "Stopped: ConnectionError" in log_text and "abcdef123" not in log_text


def test_run_stops_when_someone_else_can_post_in_the_inbox(ready) -> None:
    cfg, paths, source, owner, bot = ready
    owner.admins = frozenset({demo.OWNER, 31337})  # a co-admin was added after setup
    post_picture(source, paths, 1)
    assert scheduler.main(cfg, paths, once=True, listen_seconds=0, verbose=False) == 2
    assert any("Processing stopped for safety" in t for _, t in bot.sent)
    conn = jobs.connect(paths.database)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # nothing from the Inbox was processed
    conn.close()


def test_second_instance_is_refused(ready) -> None:
    cfg, paths, *_ = ready
    with scheduler.single_instance(paths.home / "run.lock"):
        assert scheduler.main(cfg, paths, once=True, listen_seconds=0, verbose=False) == 3
