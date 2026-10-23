"""Command-line interface (Typer). Entry point: `captioner`."""

from __future__ import annotations

from pathlib import Path

import typer

from . import __version__
from . import config as config_mod
from .paths import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Private Media Captioner: archive and caption your Telegram photos and videos locally.")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"captioner {__version__}")
        raise typer.Exit()


@app.callback()
def main(_version_flag: bool = typer.Option(False, "--version", callback=_version, is_eager=True,
                                            help="Show the version and exit.")) -> None:
    """Private Media Captioner."""


def _load_config() -> config_mod.Config:
    paths = get_paths()
    try:
        return config_mod.load(paths.config_file)
    except config_mod.ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc


@app.command()
def setup(step: str = typer.Option(None, help="Run only this step again (see --list)."),
          list_steps: bool = typer.Option(False, "--list", help="List the setup steps and exit.")) -> None:
    """Guided setup. Secrets are typed here, on your machine, and stored in the OS keyring."""
    from . import wizard

    raise typer.Exit(wizard.run_wizard(only=step, list_only=list_steps))


@app.command()
def doctor(offline: bool = typer.Option(False, help="Skip checks that contact local services (Ollama)."))\
        -> None:
    """Check dependencies, storage, credentials and integrations."""
    from . import doctor as doctor_mod

    checks = doctor_mod.run_all(get_paths(), include_network=not offline)
    colors = {doctor_mod.OK: "green", doctor_mod.WARN: "yellow", doctor_mod.FAIL: "red"}
    for c in checks:
        typer.secho(f"[{c.status:4}] {c.name}: {c.detail}", fg=colors[c.status])
        if c.fix and c.status != doctor_mod.OK:
            typer.echo(f"        fix: {c.fix}")
    failed = [c for c in checks if c.status == doctor_mod.FAIL]
    typer.echo(f"\n{len(checks)} checks, {len(failed)} failed.")
    raise typer.Exit(1 if failed else 0)


@app.command()
def run(once: bool = typer.Option(False, help="Catch up, process available work, then exit."),
        listen_seconds: int = typer.Option(20, help="With --once: seconds to wait for bot replies."),
        verbose: bool = typer.Option(False, help="Debug logging.")) -> None:
    """Start processing: catch up on the Inbox, then keep watching until Ctrl+C."""
    from . import scheduler

    raise typer.Exit(scheduler.main(_load_config(), get_paths(), once=once, listen_seconds=listen_seconds,
                                    verbose=verbose))


@app.command()
def status() -> None:
    """Show job counts, pending reviews and failures. Never shows captions or file names."""
    paths = get_paths()
    if not paths.database.exists():
        typer.echo("No database yet. Run `captioner run` after setup, or `captioner demo`.")
        raise typer.Exit(0)
    from . import health, jobs

    conn = jobs.connect(paths.database)
    try:
        typer.echo(health.status_text(conn))
    finally:
        conn.close()


@app.command()
def demo(output: Path = typer.Option(None, help="Folder for demo data (default: a new temp folder)."))\
        -> None:
    """Offline demo with generated media. Sends nothing to Telegram or YouTube."""
    from . import demo as demo_mod

    raise typer.Exit(demo_mod.main(output))


@app.command()
def retry(job_id: int,
          confirm_not_uploaded: bool = typer.Option(
              False, help="For upload_unknown jobs: you checked YouTube Studio and the video is NOT there."),
          uploaded_as: str = typer.Option(
              None, help="For upload_unknown jobs: the video IS in YouTube Studio; give its id to record it.")) \
        -> None:
    """Show why a job failed and reopen it safely (never causes a duplicate upload)."""
    from . import recovery

    raise typer.Exit(recovery.retry(get_paths(), _load_config(), job_id, confirm_not_uploaded, uploaded_as))


@app.command()
def review(job_id: int,
           caption: str = typer.Option(None, help="Exact caption to set and approve."),
           reset_rewrites: bool = typer.Option(False, help="Reset the AI rewrite counter and re-ask in Telegram."),
           show: bool = typer.Option(False, help="Only show the job and its current caption.")) -> None:
    """Manual review for a job, for example after the rewrite limit was reached."""
    from . import recovery

    raise typer.Exit(recovery.review(get_paths(), _load_config(), job_id, caption, reset_rewrites, show))


if __name__ == "__main__":
    app()
