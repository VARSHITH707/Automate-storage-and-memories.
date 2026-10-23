from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from captioner import jobs
from captioner.jobs import IncomingMessage
from captioner.paths import Paths

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class MemoryKeyring(KeyringBackend):
    """In-memory keyring so tests never touch the real OS credential store."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    """Every test gets its own app home and an in-memory keyring."""
    monkeypatch.setenv("CAPTIONER_HOME", str(tmp_path / "home"))
    ring = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(ring)
    yield ring
    keyring.set_keyring(previous)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "home")
    p.ensure()
    return p


@pytest.fixture
def conn():
    c = jobs.connect(":memory:")
    yield c
    c.close()


def make_msg(message_id: int, *, text: str = "", media: str | None = "document", mime: str | None = "image/jpeg",
             file_name: str | None = "pic.jpg", grouped_id: int | None = None, chat_id: int = -1001,
             size: int = 1000, has_video_attr: bool = False, via_bot: bool = False,
             sender_is_bot: bool = False, sender_id: int | None = None) -> IncomingMessage:
    return IncomingMessage(chat_id=chat_id, message_id=message_id, posted_at=NOW, text=text,
                           grouped_id=grouped_id, media=media, mime=mime, file_name=file_name,
                           file_size=size, has_video_attr=has_video_attr, via_bot=via_bot,
                           sender_is_bot=sender_is_bot, sender_id=sender_id)
