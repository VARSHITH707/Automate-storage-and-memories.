from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

from captioner import config, jobs, recovery, scheduler, secrets, wizard
from captioner.paths import Paths
from tests.conftest import NOW, make_msg


@pytest.fixture
def db(paths: Paths):
    conn = jobs.connect(paths.database)
    yield conn
    conn.close()


def new_job(conn, state: str, **fields) -> int:
    with jobs.tx(conn):
        job_id = jobs.register_message(conn, make_msg(len(fields) + 100 + conn.total_changes), "video", False, NOW)
        sets = ", ".join(f"{k} = ?" for k in ["state", *fields])
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (state, *fields.values(), job_id))  # noqa: S608
    return job_id


def test_retry_reopens_failed_job_at_safe_step(paths, db) -> None:
    job_id = new_job(db, "failed", failed_from="downloaded", error_category="archive", error_message="disk full",
                     retry_count=4)
    assert recovery.retry(paths, config.Config(), job_id, False) == 0
    job = jobs.get_job(db, job_id)
    assert (job["state"], job["retry_count"], job["error_message"]) == ("new", 0, None)


def test_retry_unknown_upload_requires_confirmation(paths, db) -> None:
    job_id = new_job(db, "upload_unknown")
    db.execute("INSERT INTO uploads(job_id, attempt, state, started_at, updated_at) VALUES (?, 1, 'in_progress', "
               "'x', 'x')", (job_id,))
    assert recovery.retry(paths, config.Config(), job_id, False) == 1
    assert jobs.get_job(db, job_id)["state"] == "upload_unknown"
    assert recovery.retry(paths, config.Config(), job_id, True) == 0
    assert jobs.get_job(db, job_id)["state"] == "uploading"
    assert db.execute("SELECT state FROM uploads").fetchone()[0] == "abandoned"


def test_retry_records_a_video_the_owner_found(paths, db) -> None:
    job_id = new_job(db, "upload_unknown")
    assert recovery.retry(paths, config.Config(), job_id, False, uploaded_as="bad id!") == 1
    assert recovery.retry(paths, config.Config(), job_id, False, uploaded_as="dQw4w9WgXcQ") == 0
    assert jobs.get_job(db, job_id)["state"] == "done"
    assert db.execute("SELECT video_id, state FROM uploads WHERE job_id = ?", (job_id,)).fetchone()[0] == "dQw4w9WgXcQ"


def test_retry_unknown_job_and_nothing_to_do(paths, db) -> None:
    assert recovery.retry(paths, config.Config(), 999, False) == 1
    job_id = new_job(db, "done")
    assert recovery.retry(paths, config.Config(), job_id, False) == 0
    assert jobs.get_job(db, job_id)["state"] == "done"


def test_review_sets_exact_caption_and_approves(paths, db) -> None:
    cfg = config.Config()
    cfg.telegram.owner_id = 42
    job_id = new_job(db, "awaiting_approval", caption="old", rewrite_count=5, question_id=7)
    assert recovery.review(paths, cfg, job_id, "New words.\nExactly.", False, False) == 0
    job = jobs.get_job(db, job_id)
    assert (job["state"], job["caption"], job["caption_source"]) == ("approved", "New words.\nExactly.", "owner_cli")
    approval = db.execute("SELECT owner_id, caption FROM approvals WHERE job_id = ?", (job_id,)).fetchone()
    assert tuple(approval) == (42, "New words.\nExactly.")


def test_review_reset_rewrites_reasks(paths, db) -> None:
    job_id = new_job(db, "awaiting_approval", caption="c", rewrite_count=5, question_id=7)
    assert recovery.review(paths, config.Config(), job_id, None, True, False) == 0
    job = jobs.get_job(db, job_id)
    assert (job["state"], job["rewrite_count"], job["question_id"]) == ("awaiting_approval", 0, None)


def test_review_refuses_wrong_state(paths, db) -> None:
    job_id = new_job(db, "done")
    assert recovery.review(paths, config.Config(), job_id, "x", False, False) == 1


def test_single_instance_lock(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    with scheduler.single_instance(lock):
        with pytest.raises(scheduler.AlreadyRunningError):
            with scheduler.single_instance(lock):
                pass
    with scheduler.single_instance(lock):  # released after the first run ends
        pass


def test_run_refuses_when_not_set_up(paths) -> None:
    assert scheduler.main(config.Config(), paths, once=True, listen_seconds=0, verbose=False) == 2


def test_readiness_lists_every_missing_piece() -> None:
    problems = scheduler.readiness_problems(config.Config())
    assert len(problems) == 5
    secrets.set_secret(secrets.API_ID, "1")
    assert len(scheduler.readiness_problems(config.Config())) == 4


def test_wizard_lists_and_validates_steps(paths) -> None:
    assert wizard.run_wizard(list_only=True) == 0
    assert wizard.run_wizard(only="nonsense") == 2


def test_wizard_stops_without_secure_keyring() -> None:
    import keyring
    from keyring.backends import fail

    keyring.set_keyring(fail.Keyring())
    assert wizard.run_wizard() == 2


def test_wizard_archive_step_saves_progress(paths, monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "my-archive"
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: str(target))
    assert wizard.run_wizard(only="archive") == 0
    cfg = config.load(paths.config_file)
    assert cfg.archive.root == str(target) and "archive" in cfg.setup_completed


def test_wizard_failed_step_keeps_earlier_progress(paths, monkeypatch) -> None:
    cfg = config.Config()
    cfg.setup_completed = ["archive"]
    config.save(cfg, paths.config_file)
    answers = iter(["12345", "short-hash"])
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(answers))
    assert wizard.run_wizard(only="telegram") == 1
    assert config.load(paths.config_file).setup_completed == ["archive"]
    assert secrets.get_secret(secrets.API_HASH) is None  # nothing stored from a bad entry


def test_wizard_preferences_keep_location_off_by_default(paths, monkeypatch) -> None:
    monkeypatch.setattr(typer, "prompt", lambda text, default=None, **k: default)
    monkeypatch.setattr(typer, "confirm", lambda text, default=False: default)
    assert wizard.run_wizard(only="preferences") == 0
    cfg = config.load(paths.config_file)
    assert cfg.caption.include_location is False and cfg.caption.language == "English"


def test_wizard_youtube_step_can_be_skipped(paths, monkeypatch) -> None:
    monkeypatch.setattr(typer, "confirm", lambda text, default=False: False)
    assert wizard.run_wizard(only="youtube") == 0
    assert config.load(paths.config_file).youtube.enabled is False


def test_config_written_by_wizard_holds_no_secrets(paths, monkeypatch, tmp_path: Path) -> None:
    secrets.set_secret(secrets.BOT_TOKEN, "123456:SECRETTOKENVALUE")
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: str(tmp_path / "a"))
    wizard.run_wizard(only="archive")
    assert "SECRETTOKENVALUE" not in paths.config_file.read_text(encoding="utf-8")
    assert json.dumps(config.load(paths.config_file).__dict__, default=str).count("SECRET") == 0
