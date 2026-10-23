"""Load, validate and save `config.toml`. Secrets never live here; they go to the OS keyring."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass
class TelegramConfig:
    inbox_id: int = 0  # 0 = not set
    inbox_kind: str = "channel"  # "channel" (broadcast, owner is the only admin) or "group"
    vault_id: int = 0
    owner_id: int = 0
    bot_id: int = 0
    owner_dm_ok: bool = False  # owner pressed /start in the bot chat


@dataclass
class ArchiveConfig:
    root: str = ""  # empty = <app home>/archive
    max_file_mb: int = 4096
    min_free_mb: int = 1024


@dataclass
class VaultConfig:
    marker: str = "#vault"
    aliases: list[str] = field(default_factory=lambda: ["#private"])


@dataclass
class CaptionConfig:
    language: str = "English"
    include_location: bool = False
    vision_model: str = "gemma3:4b"
    text_model: str = "gemma3:4b"
    whisper_model: str = "small"
    ollama_url: str = "http://127.0.0.1:11434"
    model_timeout_s: int = 300
    max_frames: int = 16
    frame_interval_s: int = 10
    max_rewrites: int = 5
    album_settle_seconds: int = 30
    write_metadata_copy: bool = True


@dataclass
class YouTubeConfig:
    enabled: bool = False
    client_secret_path: str = ""
    chunk_mb: int = 8


@dataclass
class BackupConfig:
    enabled: bool = False
    kind: str = "path"  # "path" or "rclone"
    target: str = ""


@dataclass
class RunConfig:
    scan_interval_s: int = 60
    max_attempts: int = 5
    report_interval_days: int = 7
    inactivity_days: int = 3


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)
    caption: CaptionConfig = field(default_factory=CaptionConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    run: RunConfig = field(default_factory=RunConfig)
    setup_completed: list[str] = field(default_factory=list)

    def archive_root(self, home: Path) -> Path:
        return Path(self.archive.root) if self.archive.root else home / "archive"

    @property
    def markers(self) -> list[str]:
        return [self.vault.marker, *self.vault.aliases]


SECTIONS = {f.name: f.default_factory for f in fields(Config) if f.name != "setup_completed"}


def _load_section(name: str, cls: type, data: Any) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"[{name}] must be a table")
    obj = cls()
    known = {f.name for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"Unknown setting {name}.{key}")
        expected = getattr(obj, key)
        if isinstance(expected, bool):
            ok = isinstance(value, bool)
        elif isinstance(expected, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(expected, list):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = isinstance(value, type(expected))
        if not ok:
            raise ConfigError(f"{name}.{key} must be {type(expected).__name__}, got {type(value).__name__}")
        setattr(obj, key, value)
    return obj


def load(path: Path) -> Config:
    """Read config.toml. A missing file gives the defaults."""
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc
    cfg = Config()
    for key, value in raw.items():
        if key == "setup_completed":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError("setup_completed must be a list of strings")
            cfg.setup_completed = value
        elif key in SECTIONS:
            section_cls = type(getattr(cfg, key))
            setattr(cfg, key, _load_section(key, section_cls, value))
        else:
            raise ConfigError(f"Unknown section [{key}]")
    problems = validate(cfg)
    if problems:
        raise ConfigError("; ".join(problems))
    return cfg


def validate(cfg: Config) -> list[str]:
    problems = []
    if cfg.telegram.inbox_kind not in ("channel", "group"):
        problems.append("telegram.inbox_kind must be 'channel' or 'group'")
    if cfg.backup.kind not in ("path", "rclone"):
        problems.append("backup.kind must be 'path' or 'rclone'")
    if cfg.backup.enabled and not cfg.backup.target:
        problems.append("backup.target is required when backup.enabled = true")
    if not cfg.vault.marker.startswith("#"):
        problems.append("vault.marker must start with '#'")
    if any(not a.startswith("#") for a in cfg.vault.aliases):
        problems.append("vault.aliases must start with '#'")
    if not 1 <= cfg.caption.max_frames <= 64:
        problems.append("caption.max_frames must be between 1 and 64")
    if cfg.caption.frame_interval_s < 1:
        problems.append("caption.frame_interval_s must be at least 1")
    if not 0 <= cfg.caption.max_rewrites <= 20:
        problems.append("caption.max_rewrites must be between 0 and 20")
    if cfg.run.max_attempts < 1:
        problems.append("run.max_attempts must be at least 1")
    if cfg.youtube.chunk_mb < 1:
        problems.append("youtube.chunk_mb must be at least 1")
    if cfg.youtube.enabled and not cfg.youtube.client_secret_path:
        problems.append("youtube.client_secret_path is required when youtube.enabled = true")
    # Values that reach command lines must never look like options.
    for key in ("vision_model", "text_model"):
        if not _MODEL_NAME.match(getattr(cfg.caption, key)):
            problems.append(f"caption.{key} must be an Ollama model name such as gemma3:4b")
    from .models import WHISPER_SIZES

    if cfg.caption.whisper_model not in WHISPER_SIZES:
        problems.append(f"caption.whisper_model must be one of: {', '.join(sorted(WHISPER_SIZES))}")
    if cfg.backup.target.startswith("-"):
        problems.append("backup.target must not start with '-'")
    if cfg.backup.enabled and cfg.backup.kind == "rclone" and not _RCLONE_REMOTE.match(cfg.backup.target):
        problems.append("backup.target must be an rclone remote such as gdrive:memories")
    return problems


_MODEL_NAME = re.compile(r"^[A-Za-z0-9][\w.\-/]*(:[\w.\-]+)?$")
_RCLONE_REMOTE = re.compile(r"^[\w.][\w.\- ]*:")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON string escapes are valid TOML basic strings
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise ConfigError(f"Cannot write {type(value).__name__} to TOML")


def dumps(cfg: Config) -> str:
    lines = ["# Private Media Captioner settings. No secrets belong in this file.", ""]
    lines.append(f"setup_completed = {_toml_value(cfg.setup_completed)}")
    for name in SECTIONS:
        lines += ["", f"[{name}]"]
        for key, value in asdict(getattr(cfg, name)).items():
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def save(cfg: Config, path: Path) -> None:
    """Write atomically so a crash never leaves a half-written config."""
    problems = validate(cfg)
    if problems:
        raise ConfigError("; ".join(problems))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(dumps(cfg))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
