"""OS keyring storage for credentials, plus log redaction. Never falls back to plaintext."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

import keyring
from keyring.errors import KeyringError

SERVICE = "PrivateMediaCaptioner"
MAX_SECRET_CHARS = 1200  # Windows Credential Manager holds about 1280 UTF-16 characters

# Names of the values the app keeps in the keyring.
API_ID = "telegram_api_id"
API_HASH = "telegram_api_hash"
BOT_TOKEN = "telegram_bot_token"  # noqa: S105 - keyring entry name, not a secret
YOUTUBE_TOKEN = "youtube_refresh_token"  # noqa: S105 - keyring entry name, not a secret


class SecretStoreError(RuntimeError):
    pass


def _backend_names(backend: object) -> list[str]:
    inner = getattr(backend, "backends", None)  # ChainerBackend wraps several
    if inner:
        return [n for b in inner for n in _backend_names(b)]
    return [f"{type(backend).__module__}.{type(backend).__name__}"]


def _is_insecure(name: str) -> bool:
    return (
        name.startswith(("keyring.backends.fail", "keyring.backends.null", "keyrings.alt"))
        or "Plaintext" in name
    )


def backend_status() -> tuple[str, bool]:
    """Return (backend description, is_secure). A chain counts as secure only if every member is,
    because a chained keyring may read a value back from any of its members."""
    names = _backend_names(keyring.get_keyring())
    return ", ".join(names), bool(names) and not any(_is_insecure(n) for n in names)


def _require_secure() -> None:
    name, secure = backend_status()
    if not secure:
        raise SecretStoreError(
            f"No secure OS keyring is available (backend: {name}). "
            "Install or unlock your system credential store; plaintext storage is not allowed."
        )


def set_secret(key: str, value: str) -> None:
    _require_secure()
    if not value:
        raise SecretStoreError(f"Refusing to store an empty value for {key}")
    if len(value) > MAX_SECRET_CHARS:
        raise SecretStoreError(f"{key} is too long for the OS credential store")
    try:
        keyring.set_password(SERVICE, key, value)
    except KeyringError as exc:
        raise SecretStoreError(f"Could not save {key} to the keyring: {exc}") from exc


def get_secret(key: str) -> str | None:
    _require_secure()
    try:
        return keyring.get_password(SERVICE, key)
    except KeyringError as exc:
        raise SecretStoreError(f"Could not read {key} from the keyring: {exc}") from exc


def delete_secret(key: str) -> None:
    try:
        keyring.delete_password(SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass


_PATTERNS = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "[bot-token]"),
    (re.compile(r"ya29\.[\w.-]+"), "[google-access-token]"),
    (re.compile(r"1//[\w-]{20,}"), "[google-refresh-token]"),
    (re.compile(r"GOCSPX-[\w-]+"), "[google-client-secret]"),
    (re.compile(r"\b[a-f0-9]{32}\b"), "[hex-secret]"),
    (re.compile(r"\+\d{7,15}\b"), "[phone]"),
    (re.compile(r"(?<![\w-])\d{10,15}(?![\w-])"), "[long-number]"),  # phone numbers, account ids
    (re.compile(r"(?i)\b(api_hash|token|secret|password|passwd|code)(\s*[=:]\s*)\S+"), r"\1\2[redacted]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Redacts the whole formatted line, including any traceback text."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def setup_logging(log_dir: Path | None, verbose: bool = False) -> None:
    """Console plus a rotating file in app-data. Every handler redacts, for our logs and for
    third-party libraries (Telethon, Google) alike; the libraries only log warnings and errors."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    logging.getLogger("captioner").setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_dir / "captioner.log", maxBytes=2_000_000, backupCount=3,
                                            encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
