"""Owner-account Telegram client (Telethon): read the Inbox, download originals, copy to the Vault.

It never reads or writes any other chat.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.tl import types

from .jobs import IncomingMessage

log = logging.getLogger("captioner.telegram")
FLOOD_SLEEP_THRESHOLD = 60
VAULT_TAG = "#pmc"


class TelegramSetupError(RuntimeError):
    pass


def _special_document(doc: Any) -> bool:
    special = (types.DocumentAttributeSticker, types.DocumentAttributeAudio, types.DocumentAttributeAnimated)
    return any(isinstance(a, special) or getattr(a, "round_message", False)
               for a in getattr(doc, "attributes", None) or [])


def normalize(message: Any, chat_id: int) -> IncomingMessage:
    """Turn a Telethon message into a plain IncomingMessage. Pure apart from attribute reads."""
    media: str | None = None
    raw_media = getattr(message, "media", None)
    doc = getattr(message, "document", None)
    if getattr(message, "photo", None) is not None:
        media = "photo"
    elif doc is not None:
        media = "other" if _special_document(doc) else "document"
    elif raw_media is not None and not isinstance(raw_media, types.MessageMediaWebPage):
        media = "other"  # polls, locations, contacts, ...
    file = getattr(message, "file", None) if media in ("photo", "document") else None
    sender = getattr(message, "sender", None)
    has_video = any(isinstance(a, types.DocumentAttributeVideo) for a in getattr(doc, "attributes", None) or [])
    return IncomingMessage(
        chat_id=chat_id,
        message_id=message.id,
        posted_at=getattr(message, "date", None),
        text=getattr(message, "message", None) or "",
        grouped_id=getattr(message, "grouped_id", None),
        media=media,
        mime=getattr(file, "mime_type", None),
        file_name=getattr(file, "name", None),
        file_size=getattr(file, "size", None),
        has_video_attr=has_video,
        via_bot=bool(getattr(message, "via_bot_id", None)),
        sender_is_bot=bool(getattr(sender, "bot", False)),
        sender_id=getattr(message, "sender_id", None),
    )


@dataclass(frozen=True)
class ChannelFacts:
    title: str
    is_broadcast: bool
    is_megagroup: bool
    has_username: bool
    admin_ids: frozenset[int]
    owner_can_post: bool


def inbox_problems(facts: ChannelFacts, owner_id: int) -> list[str]:
    """Owner-only rule (fail closed). Broadcast posts do not reliably name the human poster,
    so a broadcast Inbox must have the owner as its only admin."""
    problems = []
    if facts.has_username:
        problems.append("The Inbox is public (it has a @username). Make it private.")
    if facts.is_broadcast:
        others = facts.admin_ids - {owner_id}
        if others:
            problems.append(f"The Inbox has {len(others)} other admin(s) or bot(s) who can post. "
                            "Remove them so only you can post.")
    elif not facts.is_megagroup:
        problems.append("The Inbox must be a channel (recommended) or a supergroup.")
    if owner_id not in facts.admin_ids:
        problems.append("Your account is not an admin of the Inbox.")
    return problems


def vault_problems(facts: ChannelFacts) -> list[str]:
    problems = []
    if facts.has_username:
        problems.append("The Vault is public (it has a @username). Make it private.")
    if not facts.owner_can_post:
        problems.append("Your account cannot post in the Vault.")
    return problems


class OwnerTelegram:
    def __init__(self, api_id: int, api_hash: str, session_path: Path, inbox_id: int, vault_id: int,
                 persistent: bool = False):
        # persistent (captioner run): keep reconnecting after sleep or a Wi-Fi drop instead of giving up
        self.client = TelegramClient(str(session_path), api_id, api_hash,
                                     flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD,
                                     connection_retries=-1 if persistent else 5, retry_delay=5)
        self.inbox_id = inbox_id
        self.vault_id = vault_id
        self._inbox: Any = None
        self._vault: Any = None

    async def connect(self, interactive: bool = False) -> int:
        """Connect and return the owner's user id. Interactive login prompts in this terminal only."""
        await self.client.connect()
        if not await self.client.is_user_authorized():
            if not interactive:
                raise TelegramSetupError("The Telegram session is not logged in. Run `captioner setup`.")
            await self.client.start()  # asks for phone, login code and 2FA password locally
        me = await self.client.get_me()
        return int(me.id)

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def _entity(self, peer_id: int) -> Any:
        try:
            return await self.client.get_entity(peer_id)
        except ValueError:
            async for _ in self.client.iter_dialogs():  # fills the entity cache once
                pass
            return await self.client.get_entity(peer_id)

    async def inbox(self) -> Any:
        if self._inbox is None:
            self._inbox = await self._entity(self.inbox_id)
        return self._inbox

    async def vault(self) -> Any:
        if self._vault is None:
            self._vault = await self._entity(self.vault_id)
        return self._vault

    async def list_own_channels(self) -> list[tuple[int, str]]:
        """Channels and groups where the owner is creator or admin (for the setup picker)."""
        found = []
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, types.Channel) and (entity.creator or entity.admin_rights):
                found.append((int(dialog.id), dialog.name))
        return found

    async def channel_facts(self, peer_id: int, owner_id: int) -> ChannelFacts:
        entity = await self._entity(peer_id)
        if not isinstance(entity, types.Channel):
            raise TelegramSetupError("Choose a channel or supergroup")
        admins = await self.client.get_participants(entity, filter=types.ChannelParticipantsAdmins)
        perms = await self.client.get_permissions(entity, owner_id)
        can_post = bool(getattr(perms, "is_creator", False) or getattr(perms, "post_messages", False)
                        or (entity.megagroup and not getattr(perms, "is_banned", False)))
        return ChannelFacts(title=entity.title, is_broadcast=bool(entity.broadcast),
                            is_megagroup=bool(entity.megagroup), has_username=bool(entity.username),
                            admin_ids=frozenset(int(a.id) for a in admins), owner_can_post=can_post)

    async def scan(self, min_id: int) -> list[IncomingMessage]:
        """Every Inbox message newer than `min_id`, oldest first."""
        inbox = await self.inbox()
        return [normalize(m, self.inbox_id)
                async for m in self.client.iter_messages(inbox, min_id=min_id, reverse=True)]

    def on_new_message(self, callback: Callable[[IncomingMessage], Awaitable[None]]) -> None:
        @self.client.on(events.NewMessage(chats=[self.inbox_id]))
        async def _handler(event: Any) -> None:
            await callback(normalize(event.message, self.inbox_id))

    async def download(self, message_id: int, dest_dir: Path) -> Path:
        inbox = await self.inbox()
        message = await self.client.get_messages(inbox, ids=message_id)
        if message is None:
            raise FileNotFoundError(f"Inbox message {message_id} no longer exists")
        dest_dir.mkdir(parents=True, exist_ok=True)
        result = await self.client.download_media(message, file=str(dest_dir) + "/")
        if not result:
            raise FileNotFoundError(f"Inbox message {message_id} has no downloadable media")
        return Path(str(result))

    async def find_in_vault(self, tag: str) -> int | None:
        vault = await self.vault()
        async for m in self.client.iter_messages(vault, search=tag, limit=1):
            return int(m.id)
        return None

    async def send_to_vault(self, path: Path, tag: str) -> int:
        """Copy the archived original to the Vault as a file. The tag lets a retry find it again."""
        vault = await self.vault()
        sent = await self.client.send_file(vault, str(path), caption=f"{VAULT_TAG} {tag}", force_document=True)
        if not getattr(sent, "id", None):
            raise RuntimeError("Telegram did not confirm the Vault upload")
        return int(sent.id)
