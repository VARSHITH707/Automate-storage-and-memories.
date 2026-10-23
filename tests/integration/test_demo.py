from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from captioner import demo
from captioner.cli import app


def test_demo_runs_offline_and_passes(tmp_path: Path) -> None:
    assert demo.main(tmp_path / "out") == 0


def test_demo_refuses_non_empty_folder(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep.txt").write_text("x", encoding="utf-8")
    assert demo.main(tmp_path / "out") == 2


def test_demo_and_status_through_cli(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "demo-home"
    result = CliRunner().invoke(app, ["demo", "--output", str(out)])
    assert result.exit_code == 0 and "Result: PASS" in result.output
    monkeypatch.setenv("CAPTIONER_HOME", str(out))
    status = CliRunner().invoke(app, ["status"])
    assert status.exit_code == 0 and "done" in status.output and "Archived originals" in status.output
