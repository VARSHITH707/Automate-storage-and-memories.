"""Wizard steps that talk to Telegram, driven through fake clients."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer

from captioner import bot as bot_mod
from captioner import config, secrets, telegram, wizard
from captioner.telegram import ChannelFacts

OWNER = 1


def facts(**kw) -> ChannelFacts:
    base = dict(title="Inbox", is_broadcast=True, is_megagroup=False, has_username=False,
                admin_ids=frozenset({OWNER}), owner_can_post=True)
    base.update(kw)
    return ChannelFacts(**base)


class FakeOwnerClient:
    def __init__(self, inbox_facts: ChannelFacts, vault_facts: ChannelFacts):
        self.f = {-1: inbox_facts, -2: vault_facts}

    async def connect(self, interactive=False):
        return OWNER

    async def disconnect(self):
        pass

    async def list_own_channels(self):
        return [(-1, "Inbox"), (-2, "Vault")]

    async def channel_facts(self, peer, owner_id):
        return self.f[peer]


@pytest.fixture
def telegram_keys():
    secrets.set_secret(secrets.API_ID, "12345")
    secrets.set_secret(secrets.API_HASH, "a" * 32)


def pick(monkeypatch, *answers) -> None:
    it = iter(answers)
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(it))


def test_channels_step_accepts_owner_only_inbox(paths, monkeypatch, telegram_keys) -> None:
    monkeypatch.setattr(telegram, "OwnerTelegram", lambda *a, **k: FakeOwnerClient(facts(), facts(title="Vault")))
    pick(monkeypatch, 1, 2)
    assert wizard.run_wizard(only="channels") == 0
    cfg = config.load(paths.config_file)
    assert (cfg.telegram.inbox_id, cfg.telegram.vault_id, cfg.telegram.inbox_kind) == (-1, -2, "channel")


def test_channels_step_fails_closed_with_other_admins(paths, monkeypatch, telegram_keys, capsys) -> None:
    risky = facts(admin_ids=frozenset({OWNER, 99}))
    monkeypatch.setattr(telegram, "OwnerTelegram", lambda *a, **k: FakeOwnerClient(risky, facts()))
    pick(monkeypatch, 1, 2)
    assert wizard.run_wizard(only="channels") == 1
    assert config.load(paths.config_file).telegram.inbox_id == 0
    assert "other admin" in capsys.readouterr().out


def test_channels_step_refuses_same_channel_twice(paths, monkeypatch, telegram_keys) -> None:
    monkeypatch.setattr(telegram, "OwnerTelegram", lambda *a, **k: FakeOwnerClient(facts(), facts()))
    pick(monkeypatch, 1, 1)
    assert wizard.run_wizard(only="channels") == 1


def test_channels_step_group_inbox_records_kind(paths, monkeypatch, telegram_keys) -> None:
    group = facts(is_broadcast=False, is_megagroup=True, admin_ids=frozenset({OWNER, 5}))
    monkeypatch.setattr(telegram, "OwnerTelegram", lambda *a, **k: FakeOwnerClient(group, facts()))
    pick(monkeypatch, 1, 2)
    assert wizard.run_wizard(only="channels") == 0
    assert config.load(paths.config_file).telegram.inbox_kind == "group"


def test_telegram_step_stores_keys_and_owner(paths, monkeypatch) -> None:
    monkeypatch.setattr(telegram, "OwnerTelegram", lambda *a, **k: FakeOwnerClient(facts(), facts()))
    pick(monkeypatch, 12345, "b" * 32)
    assert wizard.run_wizard(only="telegram") == 0
    assert secrets.get_secret(secrets.API_HASH) == "b" * 32
    assert config.load(paths.config_file).telegram.owner_id == OWNER


class FakeBot:
    started = True

    def __init__(self, *a, **k):
        self.sent = []
        self.client = SimpleNamespace(get_me=self._me)

    async def _me(self):
        return SimpleNamespace(username="helper_bot")

    async def connect(self):
        return 55

    async def disconnect(self):
        pass

    async def wait_for_start(self, timeout):
        return FakeBot.started

    async def send(self, text):
        self.sent.append(text)
        return 1


@pytest.mark.parametrize("started, code", [(True, 0), (False, 1)])
def test_bot_step_requires_owner_start(paths, monkeypatch, telegram_keys, started, code) -> None:
    FakeBot.started = started
    monkeypatch.setattr(bot_mod, "HelperBot", FakeBot)
    pick(monkeypatch, "123456:" + "x" * 35)
    assert wizard.run_wizard(only="bot") == code
    cfg = config.load(paths.config_file)
    assert cfg.telegram.owner_dm_ok is started
    assert cfg.telegram.bot_id == 55  # the bot itself connected in both cases
    assert ("bot" in cfg.setup_completed) is started


def test_bot_step_rejects_malformed_token(paths, monkeypatch, telegram_keys) -> None:
    pick(monkeypatch, "not-a-token")
    assert wizard.run_wizard(only="bot") == 1
    assert secrets.get_secret(secrets.BOT_TOKEN) is None
