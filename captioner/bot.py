"""Helper bot (Telethon in bot mode): every message the app sends to the owner goes through it.

It only talks to the owner's private chat and only reports replies that come from the owner.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events

log = logging.getLogger("captioner.bot")
MAX_MESSAGE_CHARS = 4000  # Telegram's limit is 4096


@dataclass(frozen=True)
class Reply:
    sender_id: int
    message_id: int
    text: str
    reply_to: int | None
    is_private: bool
    sender_is_bot: bool


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on paragraph boundaries so every part fits Telegram's message limit."""
    parts, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            parts.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        parts.append(current)
    return parts


class HelperBot:
    def __init__(self, api_id: int, api_hash: str, session_path: Path, bot_token: str, owner_id: int,
                 persistent: bool = False):
        self.client = TelegramClient(str(session_path), api_id, api_hash, catch_up=True,
                                     connection_retries=-1 if persistent else 5, retry_delay=5)
        self._token = bot_token
        self.owner_id = owner_id

    async def connect(self) -> int:
        await self.client.start(bot_token=self._token)
        me = await self.client.get_me()
        if not getattr(me, "bot", False):
            raise RuntimeError("The bot token did not log in as a bot account")
        return int(me.id)

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def send(self, text: str) -> int:
        """Send to the owner's private chat. Returns the id of the last part (the one to reply to)."""
        last_id = 0
        for part in split_message(text):
            sent = await self.client.send_message(self.owner_id, part, parse_mode=None, link_preview=False)
            last_id = int(sent.id)
        return last_id

    def on_reply(self, callback: Callable[[Reply], Awaitable[None]]) -> None:
        @self.client.on(events.NewMessage(incoming=True))
        async def _handler(event: Any) -> None:
            sender = await event.get_sender()
            await callback(Reply(sender_id=int(event.sender_id or 0), message_id=int(event.id),
                                 text=event.raw_text or "", reply_to=event.reply_to_msg_id,
                                 is_private=bool(event.is_private),
                                 sender_is_bot=bool(getattr(sender, "bot", False))))

    async def wait_for_start(self, timeout: float) -> bool:
        """Setup helper: wait until the owner sends /start in the bot's private chat."""
        started = asyncio.Event()

        @self.client.on(events.NewMessage(incoming=True, pattern=r"^/start"))
        async def _handler(event: Any) -> None:
            if event.is_private and int(event.sender_id or 0) == self.owner_id:
                started.set()

        try:
            await asyncio.wait_for(started.wait(), timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self.client.remove_event_handler(_handler)
