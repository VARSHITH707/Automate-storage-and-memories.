"""`captioner run`: connect, catch up, keep processing, and shut down cleanly on Ctrl+C."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from . import archive, jobs, secrets, tools
from .backup import BackupTarget
from .bot import HelperBot
from .captions import LocalCaptioner
from .config import Config
from .models import ModelError, OllamaClient
from .paths import Paths, restrict
from .pipeline import Pipeline, Services
from .telegram import OwnerTelegram, inbox_problems

log = logging.getLogger("captioner.run")
MB = 1024 * 1024
POLICY_CHECK_INTERVAL = timedelta(hours=1)


class AlreadyRunningError(RuntimeError):
    pass


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    """Only one `captioner run` may use the database and sessions at a time."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunningError("Another `captioner run` is already active") from exc
        yield  # the OS releases the lock when the descriptor is closed, even after a crash
    finally:
        os.close(fd)


def readiness_problems(cfg: Config) -> list[str]:
    problems = []
    t = cfg.telegram
    if not (t.inbox_id and t.vault_id and t.owner_id):
        problems.append("Telegram Inbox, Vault or owner is not set up")
    if not t.owner_dm_ok:
        problems.append("The owner has not pressed /start in the helper bot chat")
    for key, label in ((secrets.API_ID, "Telegram API ID"), (secrets.API_HASH, "Telegram API hash"),
                       (secrets.BOT_TOKEN, "bot token")):
        if not secrets.get_secret(key):
            problems.append(f"{label} is not stored")
    return problems


def _uploader(cfg: Config) -> object | None:
    if not cfg.youtube.enabled:
        return None
    from .youtube import YouTubeUploader, authorized_http, build_service, load_credentials

    creds = load_credentials()
    return YouTubeUploader(lambda: build_service(authorized_http(creds)), lambda: authorized_http(creds),
                           cfg.youtube.chunk_mb * MB)


async def _inbox_unsafe(owner: OwnerTelegram, cfg: Config, pipe: Pipeline) -> bool:
    """Re-check the owner-only Inbox rule on every start and hourly: admins can change after setup.
    Fails closed: if anyone else could post, processing stops and the owner is told why."""
    try:
        facts = await owner.channel_facts(cfg.telegram.inbox_id, cfg.telegram.owner_id)
    except Exception as exc:  # noqa: BLE001 - could not verify means do not proceed
        log.error("Could not verify the Inbox permissions (%s); stopping for safety.", type(exc).__name__)
        return True
    problems = inbox_problems(facts, cfg.telegram.owner_id)
    if not problems:
        return False
    for p in problems:
        log.error("Inbox is not safe: %s", p)
    await pipe.say("Processing stopped for safety: " + " ".join(problems) +
                   " Fix the Inbox, then start `captioner run` again.")
    return True


def _integrations(cfg: Config) -> dict[str, str]:
    try:
        models = OllamaClient(cfg.caption.ollama_url, timeout=5).list_models()
        ollama = f"reachable, {len(models)} model(s)"
    except ModelError:
        ollama = "not reachable"
    return {"Telegram": "connected", "Helper bot": "connected", "Ollama": ollama,
            "YouTube": "configured" if cfg.youtube.enabled else "not configured",
            "Backup": f"{cfg.backup.kind}" if cfg.backup.enabled else "off"}


async def _run(cfg: Config, paths: Paths, once: bool, listen_seconds: int) -> int:
    api_id = int(secrets.get_secret(secrets.API_ID) or 0)
    api_hash = secrets.get_secret(secrets.API_HASH) or ""
    owner = OwnerTelegram(api_id, api_hash, paths.sessions / "owner", cfg.telegram.inbox_id, cfg.telegram.vault_id,
                          persistent=not once)
    bot = HelperBot(api_id, api_hash, paths.sessions / "bot", secrets.get_secret(secrets.BOT_TOKEN) or "",
                    cfg.telegram.owner_id, persistent=not once)
    conn = jobs.connect(paths.database)
    backup = BackupTarget(cfg.backup, cfg.archive_root(paths.home)) if cfg.backup.enabled else None
    services = Services(owner, bot, LocalCaptioner(cfg.caption), _uploader(cfg), backup,  # type: ignore[arg-type]
                        exiftool_ok=tools.which("exiftool") is not None)
    pipe = Pipeline(conn, paths.database, cfg, paths, services)
    wake = asyncio.Event()
    try:
        if await owner.connect() != cfg.telegram.owner_id:
            log.error("The saved Telegram session belongs to a different account. Run `captioner setup`.")
            return 2
        await bot.connect()
        for p in (paths.sessions / "owner.session", paths.sessions / "bot.session"):
            restrict(p)
        for leftover in archive.leftover_temp_files(cfg.archive_root(paths.home)):
            leftover.unlink(missing_ok=True)  # our own incomplete copies from a crash
            log.info("Removed an incomplete archive copy left by an earlier crash")

        async def on_message(msg: jobs.IncomingMessage) -> None:
            try:
                if await pipe.register_live(msg):
                    wake.set()
            except Exception as exc:  # noqa: BLE001 - the periodic rescan registers it instead
                log.warning("Live message not registered (%s); the next scan will pick it up", type(exc).__name__)

        async def on_reply(reply: object) -> None:
            try:
                await pipe.handle_reply(reply)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - the owner can reply again; state is unchanged
                log.warning("Could not handle a bot reply (%s)", type(exc).__name__)
            wake.set()

        if await _inbox_unsafe(owner, cfg, pipe):
            return 2
        owner.on_new_message(on_message)
        bot.on_reply(on_reply)
        last_policy_check = pipe.clock()

        last_beat = jobs.parse_iso(jobs.get_state(conn, "last_heartbeat"))
        caught_up = await pipe.safe_scan()
        if last_beat and pipe.clock() - last_beat > timedelta(minutes=10):
            await pipe.say(f"Back online. The app was not running since {jobs.iso(last_beat)} (UTC); "
                           f"{caught_up} new post(s) were found while catching up.")
        log.info("Catch-up registered %s new item(s)", caught_up)
        integrations = _integrations(cfg)
        while True:
            await pipe.process_ready()
            await pipe.health_tick(integrations)
            jobs.set_state(conn, "last_heartbeat", jobs.iso(pipe.clock()))
            if once:
                await asyncio.sleep(listen_seconds)  # give queued bot replies time to arrive
                await pipe.process_ready()
                break
            try:
                await asyncio.wait_for(wake.wait(), timeout=cfg.run.scan_interval_s)
            except TimeoutError:
                pass
            wake.clear()
            if pipe.clock() - last_policy_check >= POLICY_CHECK_INTERVAL:
                if await _inbox_unsafe(owner, cfg, pipe):
                    return 2
                last_policy_check = pipe.clock()
            await pipe.safe_scan()  # periodic rescan closes any gap left by missed live events
    finally:
        pipe.close()
        await owner.disconnect()
        await bot.disconnect()
        conn.close()
    return 0


def main(cfg: Config, paths: Paths, once: bool, listen_seconds: int, verbose: bool) -> int:
    paths.ensure()
    secrets.setup_logging(paths.logs, verbose)
    try:
        problems = readiness_problems(cfg)
    except secrets.SecretStoreError as exc:
        problems = [str(exc)]
    if problems:
        for p in problems:
            log.error("Not ready: %s", p)
        log.error("Run `captioner setup`, then `captioner doctor`.")
        return 2
    try:
        with single_instance(paths.home / "run.lock"):
            return asyncio.run(_run(cfg, paths, once, listen_seconds))
    except AlreadyRunningError as exc:
        log.error("%s", exc)
        return 3
    except KeyboardInterrupt:
        log.info("Stopped by Ctrl+C. Work in progress resumes on the next run.")
        return 0
    except Exception as exc:  # noqa: BLE001 - report clearly instead of a traceback; state is saved
        log.error("Stopped: %s: %s. Run `captioner doctor`; work resumes on the next run.",
                  type(exc).__name__, secrets.redact(str(exc)))
        return 1
