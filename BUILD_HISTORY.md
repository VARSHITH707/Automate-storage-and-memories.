# Build history

Owner's audit trail. The status table is updated in place; the history below it is append-only.
Never recorded here: secrets, login codes, personal media, private captions, sensitive filenames.

## Status

| Area | Status | Evidence |
|---|---|---|
| Tracking files | VERIFIED | Present and updated after each phase |
| Package, CLI, config, secrets, DB | VERIFIED | `uv pip install -e .[dev]` exit 0; CLI help test; config/secrets/jobs unit tests pass |
| Setup wizard and doctor | IMPLEMENTED | `doctor` VERIFIED on this laptop and in tests; wizard steps for archive, preferences, YouTube-skip VERIFIED by tests; Telegram login/channel/bot steps need a real account (NOT CONFIGURED) |
| Telegram intake, catch-up, albums | VERIFIED (mocked) | Pipeline and adapter tests with fake Telethon clients; live account NOT CONFIGURED |
| Archive, dedupe, Vault | VERIFIED | Byte-equality, dedupe, crash-reuse, Vault-retry tests pass |
| Local captions (video, photo) | VERIFIED (mocked models) | Real ffmpeg frame/audio extraction VERIFIED; Ollama models not pulled (BLOCKED); faster-whisper not installed (BLOCKED) |
| Approval flow and sidecars | VERIFIED | ok / change: / exact replacement / album edits / stale and stranger rejection / rewrite cap tests |
| YouTube private upload | VERIFIED (mocked HTTP) | googleapiclient against recorded HTTP sequences; live account NOT CONFIGURED |
| Optional backup | VERIFIED | Path backup with real files; rclone with a mocked tool (rclone not installed) |
| Health reports and alerts | VERIFIED | Simulated-time tests |
| Offline demo | VERIFIED | `captioner demo` exit 0, Result: PASS |
| CI workflow | IMPLEMENTED | `.github/workflows/ci.yml` written (SHA-pinned actions, pip-audit); not run on GitHub (nothing pushed) |
| Documentation | VERIFIED | All required files exist; CLI options and test selectors in them were executed |
| Code and security review | VERIFIED | 2 reviews, no CRITICAL; all HIGH/MEDIUM fixed with regression tests (one MEDIUM, Telethon reconnect, is not runtime-verified) |
| Live Telegram account test | NOT STARTED | Needs the owner to run `captioner setup` |
| Live YouTube account test | NOT STARTED | Needs the owner's OAuth client and sign-in |

## Project purpose

Free, open-source, local-first Python app: archive the owner's photos and videos from a private Telegram channel, write short memory captions with local AI, get approval through a Telegram bot, and upload approved videos to YouTube as private.

## History

### 2026-10-23 21:26 +05:30 — Phase 0: inspect and research

Environment (all checked by command):

| Tool | Version / state |
|---|---|
| OS | Windows 11 Home 10.0.26300 |
| Python | 3.11.15 (uv-managed), 3.13.5 (system). `python` on PATH points at an unrelated project's venv, so this project uses its own `.venv`. |
| uv | 0.11.24 |
| git | 2.45.2.windows.1 |
| ffmpeg / ffprobe | 8.1 full build (present) |
| Ollama | 0.17.7, server starts, **no models pulled** |
| ExifTool | **missing** |
| rclone | **missing** (optional feature, off by default) |
| GitHub CLI | logged in; target repo `VARSHITH707/Automate-storage-and-memories.` exists, **public and empty** |
| Free disk on Z: | about 466 GB |

Files inspected: the workspace was empty (no files, not a git repo). Nothing to preserve.

Commands:
- `git ls-remote <repo>`: exit 0, no refs (empty repo).
- `git init -b main` and `git remote add origin <repo>`: exit 0. Nothing pushed.

Research: see RESEARCH_NOTES.md. Material findings: Google no longer locks unverified-project uploads to private, so the app must set private itself; uploads use a separate 100/day quota bucket; Telegram API terms forbid training AI on Telegram data (this app only runs inference).

File-change ledger:

| Path | Action | Reason |
|---|---|---|
| .gitignore, .gitattributes | created | Keep secrets, data and models out of Git from the first commit |
| CLAUDE.md, BUILD_HISTORY.md, PROJECT_STATE.md, DECISIONS.md, RESEARCH_NOTES.md | created | Mandatory tracking and recovery files |

### 2026-10-23 22:10 +05:30 — Phase 1 core skeleton, and first pass of all modules

- `uv venv --python 3.11 .venv` and `uv pip install -e ".[dev]"`: exit 0 (telethon 1.45.0, typer 0.27.3, keyring 25.7.0, pillow 13.0.0, google-api-python-client 2.201.0, google-auth-oauthlib 1.5.0, pytest 9.1.1, ruff 0.17.2).
- API check against the installed packages (scratch script, not in repo): Telethon `iter_messages(min_id, reverse)`, `download_media`, `send_file(force_document)`, `start(bot_token=)`, `FloodWaitError(...).seconds == 42`; googleapiclient `HttpRequest.next_chunk` uses public `resumable_uri` / `resumable_progress`; bundled static discovery for youtube v3 exists; keyring backend on this laptop: `WinVaultKeyring`.
- First test run (35 tests): 34 passed, 1 failed (`status` imported a module not written yet). Fix: import moved after the "no database" check.
- Wrote: paths, config, secrets (keyring + redaction), jobs (schema v1, migrations, state machine), tools, doctor, models, router, archive, telegram, bot, approval, photo, video, captions, youtube, backup, health, pipeline, scheduler, recovery, wizard, demo, cli.
- `ruff check captioner tests`: 79 findings, mostly line length. Line length set to 120 (decision), B008 ignored (Typer idiom), S608 false positives removed by moving constant SQL out of f-strings. Result: `All checks passed!`
- `captioner demo`: exit 0, `Result: PASS` (7 synthetic posts; 5 archived, 1 duplicate, 1 vault, 1 unsupported, album rewrite, stale reply ignored, private mock insert).

| Path | Action | Reason |
|---|---|---|
| pyproject.toml | created | Package metadata, pinned ranges, `captioner` entry point, pytest/ruff/mypy settings |
| captioner/*.py | created | Implementation (see list above) |
| tests/conftest.py, tests/unit/test_config_secrets.py, test_jobs.py, test_cli_doctor.py | created | Phase 1 tests with an in-memory keyring |

### 2026-10-23 22:55 +05:30 — Phases 2 to 6: tests, defects found and fixed

Tests written: router/archive, approval/Telegram normalize, models/captions (local fake Ollama HTTP server, real ffmpeg), YouTube (googleapiclient with recorded HTTP), health/backup, recovery/scheduler/wizard, adapters and run loop (fake Telethon clients), pipeline integration (41 tests), demo.

Errors and fixes, in order:

1. `pytest` failed to start: "pyproject.toml: Invalid statement (line 1, column 1)". Root cause: a PowerShell 5.1 `Set-Content -Encoding utf8` wrote a byte-order mark, and the Write tool then kept that encoding. Fix: rewrote `pyproject.toml`, `README.md`, `captioner/__init__.py` without a BOM. Verified: pytest starts; 105 passed, 1 skipped.
2. `test_unreachable_ollama_is_distinct` failed: on Windows a refused local connection surfaced as a connect timeout and was reported as "model too slow". Root cause: urllib wraps connect/send errors in `URLError`; a timeout there means nothing is listening. Fix: `URLError` always maps to `ModelUnreachableError` (retry later); read timeouts stay `ModelTimeoutError`. Verified: caption tests 22/22.
3. `test_lost_temp_file_is_downloaded_again` failed: a job whose temp file vanished went back to `new` but stopped at `downloaded` until the next pass. Root cause: the pass loop allowed each (job, state) only once. Fix: up to `MAX_VISITS = 2` visits per state per pass (still guaranteed to end). Verified: pipeline tests pass.
4. `test_backup_failures_are_queued_and_retried` failed: test compared a `sqlite3.Row` with a tuple (test bug). Fix: `tuple(...)`.
5. `test_single_instance_lock` failed with `PermissionError` when closing the lock file after a refused lock on Windows. Root cause: buffered text-mode handle. Fix: raw `os.open` descriptor with `msvcrt.locking`/`fcntl.flock`, no PID write. Verified.
6. `mypy captioner`: 9 errors, including two real `None` edge cases (album row without a timestamp; a job vanishing between selection and review). Fixed. Verified: "Success: no issues found in 25 source files".

Results (full log: `reports/logs/pytest-full.txt`, ignored by Git):
- `python -m pytest -rs --cov=captioner`: **225 passed, 1 skipped** (ExifTool not installed), total coverage 84%.
- `ruff check .`: All checks passed. `mypy captioner`: no issues.

Integration status: Telegram owner client and bot: mocked only (NOT CONFIGURED). YouTube: mocked HTTP only (NOT CONFIGURED). Ollama: server present, no model pulled (BLOCKED for a real caption smoke test). faster-whisper: not installed (BLOCKED). ffmpeg/ffprobe: real local tests PASSED. ExifTool, rclone: not installed (SKIPPED / mocked).

| Path | Action | Reason |
|---|---|---|
| tests/unit/*.py, tests/integration/*.py | created | Coverage of every acceptance criterion that can run offline |
| captioner/models.py, pipeline.py, scheduler.py, photo.py, config.py, wizard.py | edited | Fixes 2, 3, 5, 6 above |
| requirements.txt | created | `uv pip compile --universal` pinned resolution |
| LICENSE, config.example.toml, .github/workflows/ci.yml | created | Release files |

### 2026-10-23 23:20 +05:30 — Phase 7: docs, clean install, build

- `captioner doctor` on this laptop (fresh app home): 18 checks; OK for keyring (WinVaultKeyring), ffmpeg 8.1, ffprobe 8.1, Ollama reachable (0 models); WARN ExifTool, faster-whisper, models not pulled, YouTube NOT CONFIGURED; FAIL Telegram API id/hash and bot token (not stored). Exit 1, as designed. Changed: archive check is WARN (not FAIL) before setup has created a config.
- Docs written: README, PROJECT_GUIDE, ARCHITECTURE (Mermaid: components, sequence, states, ER, security boundaries, retry, artifacts), SECURITY, PRIVACY, TROUBLESHOOTING, PROFESSOR_DEMO, RELEASE_CHECKLIST.
- Clean environment: new venv in the scratchpad, `uv pip install "<repo>"` (non-editable): exit 0; `captioner --version` = `captioner 0.1.0`; `captioner demo` = `Result: PASS`, exit 0.
- `python -m build`: exit 0, `private_media_captioner-0.1.0-py3-none-any.whl` (69,936 bytes) and `.tar.gz` (63,090 bytes). Log: `reports/logs/build.txt`.
- Test selectors quoted in PROFESSOR_DEMO and RELEASE_CHECKLIST executed: 8, 2, 2 and 44 passed (1 skipped).
- `git status --untracked-files=all`: only source, tests and docs would be tracked. Ignored: `.venv`, caches, `build/`, `dist/`, egg-info, `reports/`, `.coverage`. No session, config, client-secret, database, archive or log file is present in the working tree.

| Path | Action | Reason |
|---|---|---|
| README.md, PROJECT_GUIDE.md, ARCHITECTURE.md, SECURITY.md, PRIVACY.md, TROUBLESHOOTING.md, PROFESSOR_DEMO.md, RELEASE_CHECKLIST.md | created | Required documentation |
| captioner/doctor.py | edited | Archive WARN before setup |

### 2026-10-24 — Code review and security review, fixes

Two independent read-only reviews (code-reviewer and security-reviewer agents) plus my own pass. No CRITICAL findings. Every fix below has a regression test (`tests/unit/test_review_fixes.py` unless noted).

| Finding (severity, source) | Fix | Test |
|---|---|---|
| Scan or connect network error crashed `captioner run` (own pass) | `safe_scan`; guarded live handlers; clean exit code 1 with redacted message | `test_scan_failure_does_not_stop_the_run`, `test_connection_failure_exits_cleanly` |
| Archive files unknown to the DB not diagnosed (own pass, spec) | `doctor` "archive consistency" check, report only | `test_doctor_reports_orphans_without_deleting` |
| Caption with `<`/`>` could never upload (HIGH, code) | `approval.youtube_description` strips them, 5000-byte cap; archive keeps exact text | `test_caption_with_angle_brackets_still_uploads`, `test_youtube_description_is_safe_and_bounded` |
| Ollama traffic followed system/HTTP proxies (MEDIUM, security) | Proxy-less opener, redirects refused, `http` loopback only | `test_ollama_ignores_proxy_settings`, `test_ollama_refuses_redirects_and_other_schemes` |
| Program planted in the current folder could run (MEDIUM, security) | `tools.which` searches PATH only, skips the current folder; whisper worker `python -I` | `test_program_in_current_folder_is_never_used` |
| Owner-only Inbox checked at setup only (MEDIUM, security) | `run` re-checks at start and hourly, stops and tells the owner | `test_run_stops_when_someone_else_can_post_in_the_inbox` |
| Small-file transient error became `upload_unknown` (MEDIUM, code) | Session saved on error; no session = attempt abandoned, safe retry | `test_error_before_any_session_does_not_become_unknown`, `test_session_opened_before_a_network_error_is_kept` |
| httplib2/TransportError treated as permanent (MEDIUM, code) | Mapped to `UploadRetry("network")` | same test |
| Failed model call cached; rewrites from empty evidence (MEDIUM, code) | No cache when a step failed; rewrite guard | `test_failed_vision_is_not_cached`, `test_rewrite_without_evidence_keeps_caption` |
| Non-model exception left a job failed forever (MEDIUM, code) | `_caption` degrades to "needs your caption"; Pillow errors caught broadly | `test_broken_image_decoder_error_means_no_metadata` |
| StaleStateError inside error paths escaped the loop (MEDIUM, code) | Wrapped in `_step` and `dispatch_reviews` | full suite |
| Telethon gave up reconnecting after ~5 tries (MEDIUM, code; not runtime-verified) | `connection_retries=-1` in `run` | none (needs a real network drop) |
| Short replies ("yes", "okk") approved as captions (LOW, both) | Confirm with `1:`; ok-typos asked about | `test_unclear_replies_change_nothing` |
| `</evidence>` in untrusted text; URLs in captions (LOW, security) | Angle brackets neutralised; link captions rejected | `test_evidence_cannot_close_its_own_block`, `test_captions_with_links_are_rejected` |
| Redaction missed tracebacks, library loggers, `GOCSPX-`, bare phone numbers (LOW, security) | `RedactingFormatter` on root handlers; new patterns | `test_log_handlers_redact` |
| Whisper weights could download silently at runtime (LOW, security) | `local_files_only`; download only in setup with consent; model name allowlist | config tests |
| Option-like config values reached command lines (LOW, security) | Validation of model names, whisper size, backup target, rclone remote | `test_values_that_reach_command_lines_are_validated` |
| Pillow bomb warning zone, ffmpeg network protocols, no audio cap, trusted reported size (LOW, security) | 50 MP cap + warning as error; `-nostdin -protocol_whitelist file`; 30 min audio; real size checked after download | full suite |
| Chained keyring with an insecure member counted as secure (LOW, security) | Every member must be secure | `test_chained_keyring_with_insecure_member_is_refused` |
| Group-readable originals on POSIX; reuse path skipped chmod; `tx` COMMIT failure (LOW) | Owner-only read-only; chmod on reuse; rollback if COMMIT fails | full suite |
| `upload_unknown` could not be closed when the video exists (LOW, code) | `retry --uploaded-as VIDEO_ID` | `test_retry_records_a_video_the_owner_found` |
| Retry reused the same seed (LOW, code) | seed 7, then 8 | `test_retry_uses_a_different_seed` |
| .gitignore gaps (LOW, security) | `*client_secret*.json`, `*.session*`, `*.log*`, `logs/`, `tmp/`, `originals/`, `enriched/`, keys, `.claude/` | `git status --ignored` |
| CI: mutable action tags, no timeout, unpinned install, no audit (LOW, security) | SHA-pinned v7.0.1 / v7.1.0, `persist-credentials: false`, 20 min timeout, `-r requirements.txt`, `pip-audit` | `pip-audit -r requirements.txt` locally: "No known vulnerabilities found" |

Accepted, not changed (documented): transcripts under 8 words are not checked for copying; steps run one at a time (a long upload delays reviews); MPEG-TS/FLV/WMV/TIFF are reported as unsupported; `restrict()` relies on the Windows per-user ACL; no minimum ExifTool version; requirements.txt has no hashes; S603/S607 ignored globally because `tools.run` is the single subprocess entry point.

Final verification (2026-10-24):
- `python -m pytest -rs --cov=captioner`: **268 passed, 1 skipped** (ExifTool not installed); coverage 86%. Log: `reports/logs/pytest-full.txt`.
- `ruff check .`: All checks passed. `mypy captioner`: no issues in 25 files.
- `captioner demo`: Result: PASS (repo venv, and a new venv installed from the built wheel).
- `python -m build`: wheel 75,440 bytes, sdist 68,619 bytes.
- CLI options checked against the docs: retry `--confirm-not-uploaded --uploaded-as`; review `--caption --reset-rewrites --show`; run `--once --listen-seconds --verbose`; setup `--step --list`; demo `--output`; doctor `--offline`.
- Nothing committed or pushed (waiting for the owner's go-ahead).
