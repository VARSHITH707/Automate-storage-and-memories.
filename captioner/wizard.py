"""`captioner setup`: guided, resumable, tested one step at a time.

Every secret is typed here, in the owner's own terminal, with hidden input, and goes straight to the OS
keyring. Completed steps are remembered in config.toml so a failed step can simply be run again.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from . import config as config_mod
from . import secrets, tools
from .config import Config
from .paths import Paths, get_paths, restrict

if TYPE_CHECKING:
    from .telegram import OwnerTelegram

Step = Callable[[Config, Paths], bool]


def _say(text: str) -> None:
    typer.echo(text)


def _confirm(question: str, default: bool = False) -> bool:
    return typer.confirm(question, default=default)


def _owner_client(cfg: Config, paths: Paths) -> OwnerTelegram:
    from .telegram import OwnerTelegram

    api_id = int(secrets.get_secret(secrets.API_ID) or 0)
    api_hash = secrets.get_secret(secrets.API_HASH) or ""
    if not api_id or not api_hash:
        raise RuntimeError("Run the 'telegram' step first")
    return OwnerTelegram(api_id, api_hash, paths.sessions / "owner", cfg.telegram.inbox_id, cfg.telegram.vault_id)


# ---------------------------------------------------------------- steps
def step_archive(cfg: Config, paths: Paths) -> bool:
    default = cfg.archive.root or str(paths.default_archive)
    root = Path(typer.prompt("Folder for the archive of originals", default=default)).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    probe = root / ".write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    free_mb = shutil.disk_usage(root).free // (1024 * 1024)
    _say(f"Archive folder OK: {root} ({free_mb} MB free)")
    if free_mb < cfg.archive.min_free_mb:
        _say("Not enough free space for the archive.")
        return False
    cfg.archive.root = str(root)
    return True


def step_telegram(cfg: Config, paths: Paths) -> bool:
    _say("Telegram API keys: open https://my.telegram.org, log in, choose 'API development tools' and create an "
         "app. Use YOUR OWN keys; they are stored in the OS keyring, never in a file.")
    api_id = typer.prompt("api_id (a number)", type=int)
    api_hash = typer.prompt("api_hash", hide_input=True).strip()
    if len(api_hash) != 32:
        _say("That api_hash does not look right (it should be 32 characters).")
        return False
    secrets.set_secret(secrets.API_ID, str(api_id))
    secrets.set_secret(secrets.API_HASH, api_hash)
    _say("Now log in to your Telegram account. Telegram sends you a login code; type it here (not in any chat).")

    async def login() -> int:
        client = _owner_client(cfg, paths)
        try:
            return await client.connect(interactive=True)
        finally:
            await client.disconnect()

    cfg.telegram.owner_id = asyncio.run(login())
    restrict(paths.sessions / "owner.session")
    _say("Logged in. The login is saved in a session file in the app-data folder; treat it like a password.")
    return True


def _pick(options: list[tuple[int, str]], label: str) -> int:
    for i, (_, name) in enumerate(options, start=1):
        _say(f"  {i}. {name}")
    choice = typer.prompt(f"Number of the {label} channel", type=int)
    if not 1 <= choice <= len(options):
        raise typer.BadParameter("Not in the list")
    return options[choice - 1][0]


def step_channels(cfg: Config, paths: Paths) -> bool:
    from .telegram import inbox_problems, vault_problems

    _say("Create two PRIVATE Telegram channels first, with only you in them: an Inbox (where you post photos and "
         "videos) and a Vault (for #vault items). Then continue.")

    async def choose() -> bool:
        client = _owner_client(cfg, paths)
        try:
            owner_id = await client.connect()
            channels = await client.list_own_channels()
            if len(channels) < 2:
                _say("You need at least two channels where you are an admin.")
                return False
            _say("Your channels:")
            inbox = _pick(channels, "Inbox")
            vault = _pick(channels, "Vault")
            if inbox == vault:
                _say("The Inbox and the Vault must be different channels.")
                return False
            inbox_facts = await client.channel_facts(inbox, owner_id)
            vault_facts = await client.channel_facts(vault, owner_id)
        finally:
            await client.disconnect()
        problems = inbox_problems(inbox_facts, owner_id) + vault_problems(vault_facts)
        if problems:
            _say("Setup stopped for safety:")
            for p in problems:
                _say(f"  - {p}")
            return False
        cfg.telegram.inbox_id, cfg.telegram.vault_id = inbox, vault
        cfg.telegram.inbox_kind = "channel" if inbox_facts.is_broadcast else "group"
        if not inbox_facts.is_broadcast:
            _say("The Inbox is a group: only messages sent by your account will be processed.")
        _say("Inbox and Vault checked: private, and only you can post in the Inbox.")
        return True

    return asyncio.run(choose())


def step_bot(cfg: Config, paths: Paths) -> bool:
    from .bot import HelperBot

    _say("Helper bot: in Telegram, message @BotFather, send /newbot and follow the steps. It gives you a token. "
         "Do NOT add the bot to your Inbox; it only talks to you in its own private chat.")
    token = typer.prompt("Bot token", hide_input=True).strip()
    if ":" not in token:
        _say("That does not look like a bot token.")
        return False
    secrets.set_secret(secrets.BOT_TOKEN, token)
    api_id = int(secrets.get_secret(secrets.API_ID) or 0)
    api_hash = secrets.get_secret(secrets.API_HASH) or ""

    async def connect_and_wait() -> bool:
        bot = HelperBot(api_id, api_hash, paths.sessions / "bot", token, cfg.telegram.owner_id)
        try:
            cfg.telegram.bot_id = await bot.connect()
            me = await bot.client.get_me()
            _say(f"Now open https://t.me/{me.username} on your phone and press START. Waiting up to 5 minutes...")
            if not await bot.wait_for_start(300):
                _say("No /start from your account arrived. Run `captioner setup --step bot` to try again.")
                return False
            await bot.send("Setup check: this chat is connected. Review requests will arrive here.")
            return True
        finally:
            await bot.disconnect()

    ok = asyncio.run(connect_and_wait())
    restrict(paths.sessions / "bot.session")
    cfg.telegram.owner_dm_ok = ok
    return ok


def step_models(cfg: Config, paths: Paths) -> bool:
    from .models import ModelError, OllamaClient

    client = OllamaClient(cfg.caption.ollama_url, timeout=10)
    try:
        installed = client.list_models()
    except ModelError as exc:
        _say(f"Ollama is not reachable ({exc}). Install it from https://ollama.com, start it, and rerun this step.")
        return False
    for model in sorted({cfg.caption.vision_model, cfg.caption.text_model}):
        if model not in installed:
            _say(f"Model {model} is not downloaded. Each model has its own license (see README, Models).")
            if not _confirm(f"Download {model} now with `ollama pull`? (several GB)"):
                _say(f"Skipped. Later run: ollama pull {model}")
                continue
            tools.run(["ollama", "pull", model], timeout=4 * 3600)
    try:
        if "vision" not in client.capabilities(cfg.caption.vision_model):
            _say(f"Warning: {cfg.caption.vision_model} does not report image support.")
    except ModelError:
        pass
    import importlib.util

    if importlib.util.find_spec("faster_whisper") is None:
        _say('Video speech is off: faster-whisper is not installed (optional: uv pip install -e ".[whisper]").')
    elif _confirm(f"Download the speech model '{cfg.caption.whisper_model}' now (about 0.5 GB for small)? "
                  "Normal runs never download it.", default=False):
        from .models import download_whisper

        download_whisper(cfg.caption.whisper_model)
        _say("Speech model downloaded.")
    _say("Ollama checked. Run `captioner doctor` any time to re-check.")
    return True


def step_tools(cfg: Config, paths: Paths) -> bool:
    ok = True
    for name, flag, required in (("ffmpeg", "-version", True), ("ffprobe", "-version", True),
                                 ("exiftool", "-ver", False)):
        found = tools.version(name, flag)
        _say(f"{name}: {found or 'missing'}{'' if found or required else ' (optional: metadata copies)'}")
        ok = ok and (bool(found) or not required)
    return ok


def step_youtube(cfg: Config, paths: Paths) -> bool:
    if not _confirm("Enable private YouTube uploads of approved videos?", default=False):
        cfg.youtube.enabled = False
        return True
    _say("You need your own Google OAuth client:\n"
         "  1. console.cloud.google.com: create a project and enable 'YouTube Data API v3'.\n"
         "  2. OAuth consent screen: External; add your own Google account as a test user.\n"
         "  3. Credentials: create an OAuth client ID of type 'Desktop app' and download its JSON.\n"
         "While the consent screen is in Testing, Google may expire the sign-in after about 7 days;\n"
         "publishing the app (without verification it shows a warning) avoids that for personal use.")
    path = Path(typer.prompt("Path to the downloaded client JSON")).expanduser()
    if not path.exists():
        _say("File not found.")
        return False
    dest = paths.home / "youtube_client_secret.json"
    shutil.copyfile(path, dest)
    restrict(dest)
    from .youtube import authorize

    _say("A browser window opens for Google sign-in. Approve 'Manage your YouTube videos' (upload only).")
    authorize(dest)
    cfg.youtube.enabled, cfg.youtube.client_secret_path = True, str(dest)
    _say("YouTube connected. Uploads are always created as Private.")
    return True


def step_backup(cfg: Config, paths: Paths) -> bool:
    if not _confirm("Enable an extra backup copy (external drive folder or rclone remote)?", default=False):
        cfg.backup.enabled = False
        return True
    kind = typer.prompt("Backup type: path or rclone", default="path").strip().lower()
    target = typer.prompt("Folder path (for example E:\\MemoriesBackup) or rclone remote (for example gdrive:memories)")
    if kind == "path":
        Path(target).mkdir(parents=True, exist_ok=True)
    elif kind == "rclone":
        if tools.which("rclone") is None:
            _say("rclone is not installed (winget install Rclone.Rclone).")
            return False
        tools.run(["rclone", "lsf", "--max-depth", "1", target.split(":")[0] + ":"], timeout=60)
    else:
        _say("Choose 'path' or 'rclone'.")
        return False
    cfg.backup.enabled, cfg.backup.kind, cfg.backup.target = True, kind, target
    return True


def step_preferences(cfg: Config, paths: Paths) -> bool:
    cfg.caption.language = typer.prompt("Caption language", default=cfg.caption.language)
    cfg.caption.include_location = _confirm("Include GPS coordinates from photo metadata in captions?",
                                            default=cfg.caption.include_location)
    cfg.caption.max_rewrites = typer.prompt("Maximum AI rewrites per item", default=cfg.caption.max_rewrites, type=int)
    cfg.run.report_interval_days = typer.prompt("Days between health reports", default=cfg.run.report_interval_days,
                                                type=int)
    return True


STEPS: list[tuple[str, Step]] = [
    ("archive", step_archive), ("telegram", step_telegram), ("channels", step_channels), ("bot", step_bot),
    ("models", step_models), ("tools", step_tools), ("youtube", step_youtube), ("backup", step_backup),
    ("preferences", step_preferences),
]


def run_wizard(only: str | None = None, list_only: bool = False) -> int:
    names = [n for n, _ in STEPS]
    if list_only:
        _say("Setup steps: " + ", ".join(names))
        return 0
    if only and only not in names:
        _say(f"Unknown step {only!r}. Steps: {', '.join(names)}")
        return 2
    paths = get_paths()
    paths.ensure()
    name, secure = secrets.backend_status()
    if not secure:
        _say(f"No secure OS keyring is available ({name}). Setup cannot store secrets safely; stopping.")
        return 2
    try:
        cfg = config_mod.load(paths.config_file)
    except config_mod.ConfigError as exc:
        _say(f"Config error: {exc}")
        return 2
    for step_name, step in STEPS:
        if only and step_name != only:
            continue
        if not only and step_name in cfg.setup_completed:
            _say(f"[done] {step_name}")
            continue
        _say(f"\n== {step_name} ==")
        try:
            ok = step(cfg, paths)
        except (KeyboardInterrupt, typer.Abort):
            config_mod.save(cfg, paths.config_file)
            _say("\nStopped. Completed steps are saved; run `captioner setup` to continue.")
            return 130
        except Exception as exc:  # noqa: BLE001 - show the reason, keep progress, never crash
            ok = False
            _say(f"Step '{step_name}' failed: {secrets.redact(str(exc))}")
        if ok and step_name not in cfg.setup_completed:
            cfg.setup_completed.append(step_name)
        config_mod.save(cfg, paths.config_file)
        if not ok:
            _say(f"Fix the problem and run `captioner setup --step {step_name}` (finished steps are kept).")
            return 1
    _say("\nSetup complete. Next: `captioner doctor`, then `captioner run`.")
    return 0
