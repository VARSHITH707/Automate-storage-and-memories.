from __future__ import annotations

from typer.testing import CliRunner

from captioner import doctor
from captioner.cli import app

runner = CliRunner()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("setup", "doctor", "run", "status", "demo", "retry", "review"):
        assert command in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and "captioner 0.1.0" in result.output


def test_doctor_on_empty_environment_reports_without_crashing(paths) -> None:
    checks = {c.name: c for c in doctor.run_all(paths, include_network=False)}
    assert checks["config"].status == doctor.WARN
    assert checks["keyring"].status == doctor.OK
    assert checks["telegram api hash"].status == doctor.FAIL
    assert checks["bot token"].status == doctor.FAIL
    assert checks["youtube"].detail.startswith("NOT CONFIGURED")
    assert checks["database"].status == doctor.WARN


def test_doctor_missing_optional_tool_is_warning(monkeypatch, paths) -> None:
    monkeypatch.setattr(doctor.tools, "version", lambda name, flag="-version": None)
    checks = {c.name: c for c in doctor.run_all(paths, include_network=False)}
    assert checks["exiftool"].status == doctor.WARN
    assert checks["ffmpeg"].status == doctor.FAIL
    assert "winget install" in checks["exiftool"].fix


def test_doctor_reports_orphans_without_deleting(paths) -> None:
    from captioner import config, jobs

    cfg = config.Config()
    originals = cfg.archive_root(paths.home) / "originals" / "2026" / "01"
    originals.mkdir(parents=True)
    known = originals / "a__111.jpg"
    known.write_bytes(b"a")
    stray = originals / "b__222.jpg"
    stray.write_bytes(b"b")
    partial = originals / ".incoming-xyz"
    partial.write_bytes(b"c")
    conn = jobs.connect(paths.database)
    conn.execute("INSERT INTO assets(sha256, archive_path, kind, size, archived_at) VALUES (?, ?, 'picture', 1, 'x')",
                 ("1" * 64, str(known)))
    conn.close()
    check = doctor.check_orphans(cfg, paths)
    assert check.status == doctor.WARN and "1 file(s) not in the database, 1 incomplete" in check.detail
    assert stray.exists() and partial.exists()


def test_doctor_cli_exit_code_reflects_failures() -> None:
    result = runner.invoke(app, ["doctor", "--offline"])
    assert result.exit_code == 1
    assert "checks," in result.output


def test_status_without_database() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0 and "No database yet" in result.output
