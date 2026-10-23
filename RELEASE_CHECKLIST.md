# Release checklist

Tick an item only when its command was run and its result was seen. Documentation existing is not evidence.

| # | Criterion | Verification command or evidence | Status (0.1.0) |
|---|---|---|---|
| 1 | Clean install works | New venv: `uv pip install --python <venv> dist\<wheel>` then `<venv>\Scripts\captioner --help` | VERIFIED 2026-10-24 |
| 2 | Tests pass | `python -m pytest -rs` | VERIFIED: 268 passed, 1 skipped |
| 3 | Lint and types | `ruff check .` and `mypy captioner` | VERIFIED |
| 4 | Offline demo | `captioner demo --output <empty folder>` prints `Result: PASS` | VERIFIED |
| 5 | Package builds | `python -m build` creates a wheel and sdist in `dist/` | VERIFIED |
| 6 | Docs match CLI | Compare `captioner --help` and each `captioner <cmd> --help` with README | VERIFIED |
| 7 | No secrets or media tracked | `git status --ignored`; `git ls-files` lists no `*.session`, `config.toml`, `client_secret*.json`, `*.db`, archive or logs | VERIFIED (working tree; nothing committed yet) |
| 8 | Private upload enforced | `pytest tests/unit/test_youtube.py -k private` | VERIFIED (mocked HTTP) |
| 9 | Owner-only approvals | `pytest tests/integration/test_pipeline.py -k "strangers or current_question"` | VERIFIED |
| 10 | Immutable originals | `pytest tests/unit/test_router_archive.py -k archive` | VERIFIED |
| 11 | CI green | GitHub Actions run on the pushed commit | NOT VERIFIED (not pushed) |
| 12 | Live Telegram | Owner runs `captioner setup`, posts a photo, approves it | NOT CONFIGURED |
| 13 | Live YouTube | Owner enables YouTube, approves a short test video, confirms Private in YouTube Studio | NOT CONFIGURED |
| 14 | Real local captions | `ollama pull gemma3:4b`, then a run with a real photo and video; record time per item | BLOCKED (no model pulled) |
| 15 | Real ExifTool copy | Install ExifTool, `pytest -m local_tools -rs` | SKIPPED (not installed) |
| 16 | Security review | Review findings recorded and fixed in BUILD_HISTORY | VERIFIED (all HIGH/MEDIUM fixed; Telethon reconnect fix not runtime-tested) |
| 17 | Dependency audit | `pip-audit -r requirements.txt` | VERIFIED: no known vulnerabilities (2026-10-24) |

Outstanding risks before calling this production-ready: no live-account testing yet; Telegram bot replies sent while the app is offline may be missed; model quality on real media is unmeasured; Google verification needed if others share one OAuth project.
