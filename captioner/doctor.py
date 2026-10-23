"""`captioner doctor`: actionable diagnostics. Optional tools missing is a warning, never a crash."""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import secrets, tools
from .paths import Paths

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def check_config(paths: Paths) -> tuple[Check, config_mod.Config | None]:
    if not paths.config_file.exists():
        return Check("config", WARN, "No config.toml yet", "Run `captioner setup`"), config_mod.Config()
    try:
        cfg = config_mod.load(paths.config_file)
    except config_mod.ConfigError as exc:
        return Check("config", FAIL, str(exc), f"Fix {paths.config_file}"), None
    return Check("config", OK, str(paths.config_file)), cfg


def check_keyring() -> Check:
    try:
        name, secure = secrets.backend_status()
    except Exception as exc:  # noqa: BLE001 - any backend failure is a diagnostic, not a crash
        return Check("keyring", FAIL, f"Keyring error: {exc}", "Install a supported OS credential store")
    if not secure:
        return Check("keyring", FAIL, f"Insecure backend: {name}",
                     "Use Windows Credential Manager, macOS Keychain or Secret Service/KWallet")
    return Check("keyring", OK, name)


def check_secret(key: str, label: str, required: bool) -> Check:
    try:
        present = bool(secrets.get_secret(key))
    except secrets.SecretStoreError as exc:
        return Check(label, FAIL, str(exc))
    if present:
        return Check(label, OK, "stored in OS keyring")
    return Check(label, FAIL if required else WARN, "not stored", "Run `captioner setup`")


def check_database(paths: Paths) -> Check:
    if not paths.database.exists():
        return Check("database", WARN, "Not created yet", "It is created on first `captioner run`")
    try:
        conn = sqlite3.connect(f"file:{paths.database}?mode=ro", uri=True, timeout=5)
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        return Check("database", FAIL, f"Cannot open: {exc}", "See TROUBLESHOOTING.md, database section")
    if result != "ok":
        return Check("database", FAIL, f"Integrity check: {result}", "Restore from a backup copy")
    return Check("database", OK, str(paths.database))


def check_archive(cfg: config_mod.Config, paths: Paths) -> Check:
    root = cfg.archive_root(paths.home)
    probe = root if root.exists() else root.parent
    if not probe.exists():
        return Check("archive", FAIL, f"Folder and its parent do not exist: {root}", "Choose a folder in setup")
    free_mb = shutil.disk_usage(probe).free // (1024 * 1024)
    if free_mb < cfg.archive.min_free_mb:
        return Check("archive", FAIL, f"Only {free_mb} MB free at {probe}", "Free space or move the archive")
    return Check("archive", OK, f"{root} ({free_mb} MB free)")


def check_orphans(cfg: config_mod.Config, paths: Paths) -> Check:
    """Files in the archive that the database does not know, and incomplete copies from a crash.
    Report only: nothing is deleted here."""
    from .archive import TEMP_PREFIX

    originals = cfg.archive_root(paths.home) / "originals"
    if not originals.exists() or not paths.database.exists():
        return Check("archive consistency", OK, "nothing to check yet")
    conn = sqlite3.connect(f"file:{paths.database}?mode=ro", uri=True, timeout=5)
    try:
        known = {Path(r[0]).resolve() for r in conn.execute("SELECT archive_path FROM assets")}
    finally:
        conn.close()
    files = [p for p in originals.rglob("*") if p.is_file() and not p.name.endswith(".json")]
    partial = [p for p in files if p.name.startswith(TEMP_PREFIX)]
    unknown = [p for p in files if not p.name.startswith(TEMP_PREFIX) and p.resolve() not in known]
    if not partial and not unknown:
        return Check("archive consistency", OK, f"{len(files)} original(s), all recorded")
    return Check("archive consistency", WARN,
                 f"{len(unknown)} file(s) not in the database, {len(partial)} incomplete copy(ies)",
                 "Incomplete copies are removed on the next `captioner run`; unrecorded files are kept and "
                 "re-linked if the same item is processed again")


def check_tool(name: str, required: bool, purpose: str, flag: str = "-version") -> Check:
    found = tools.version(name, flag)
    if found:
        return Check(name, OK, found[:90])
    install = {
        "ffmpeg": "winget install Gyan.FFmpeg",
        "ffprobe": "installed together with ffmpeg",
        "exiftool": "winget install OliverBetz.ExifTool",
        "rclone": "winget install Rclone.Rclone",
    }.get(name, "")
    return Check(name, FAIL if required else WARN, f"missing ({purpose})", install)


def check_whisper() -> Check:
    if importlib.util.find_spec("faster_whisper") is None:
        return Check("faster-whisper", WARN, "not installed: video speech will not be used",
                     'uv pip install --python .venv -e ".[whisper]"')
    return Check("faster-whisper", OK, "installed")


def check_ollama(cfg: config_mod.Config) -> list[Check]:
    from .models import ModelError, OllamaClient

    client = OllamaClient(cfg.caption.ollama_url, timeout=5)
    try:
        installed = client.list_models()
    except ModelError as exc:
        return [Check("ollama", WARN, f"not reachable at {cfg.caption.ollama_url}: {exc}",
                      "Start the Ollama app, then run `captioner doctor` again")]
    out = [Check("ollama", OK, f"reachable, {len(installed)} model(s) installed")]
    for role, model in (("vision model", cfg.caption.vision_model), ("text model", cfg.caption.text_model)):
        if model not in installed and f"{model}:latest" not in installed:
            out.append(Check(role, WARN, f"{model} is not pulled", f"ollama pull {model}"))
        else:
            out.append(Check(role, OK, model))
    return out


def check_sessions(paths: Paths) -> Check:
    found = sorted(p.name for p in paths.sessions.glob("*.session")) if paths.sessions.exists() else []
    if not found:
        return Check("telegram sessions", WARN, "none yet", "Run `captioner setup`")
    return Check("telegram sessions", OK, f"{len(found)} session file(s) in {paths.sessions}")


def check_youtube(cfg: config_mod.Config) -> Check:
    if not cfg.youtube.enabled:
        return Check("youtube", WARN, "NOT CONFIGURED (optional)", "Run `captioner setup --step youtube`")
    if not Path(cfg.youtube.client_secret_path).exists():
        return Check("youtube", FAIL, "client secret JSON not found", "Point setup at the downloaded JSON file")
    return check_secret(secrets.YOUTUBE_TOKEN, "youtube token", required=True)


def run_all(paths: Paths, include_network: bool = True) -> list[Check]:
    checks: list[Check] = []
    cfg_check, cfg = check_config(paths)
    checks.append(cfg_check)
    keyring_check = check_keyring()
    checks.append(keyring_check)
    checks.append(check_database(paths))
    checks.append(check_sessions(paths))
    if keyring_check.status == OK:
        checks.append(check_secret(secrets.API_ID, "telegram api id", required=True))
        checks.append(check_secret(secrets.API_HASH, "telegram api hash", required=True))
        checks.append(check_secret(secrets.BOT_TOKEN, "bot token", required=True))
    checks.append(check_tool("ffmpeg", True, "video frames and audio"))
    checks.append(check_tool("ffprobe", True, "video checks"))
    checks.append(check_tool("exiftool", False, "metadata copies of pictures", flag="-ver"))
    checks.append(check_whisper())
    if cfg is not None:
        archive_check = check_archive(cfg, paths)
        if not paths.config_file.exists() and archive_check.status == FAIL:
            archive_check = Check("archive", WARN, "not chosen yet", "Run `captioner setup`")
        checks.append(archive_check)
        checks.append(check_orphans(cfg, paths))
        if include_network:
            checks.extend(check_ollama(cfg))
        if keyring_check.status == OK:
            checks.append(check_youtube(cfg))
        if cfg.backup.enabled and cfg.backup.kind == "rclone":
            checks.append(check_tool("rclone", True, "optional backup", flag="version"))
        if cfg.telegram.inbox_id == 0 or cfg.telegram.vault_id == 0:
            checks.append(Check("channels", WARN, "Inbox/Vault not chosen", "Run `captioner setup`"))
        if not cfg.telegram.owner_dm_ok:
            checks.append(Check("bot chat", WARN, "owner has not pressed /start in the bot chat",
                                "Run `captioner setup --step bot`"))
    return checks
