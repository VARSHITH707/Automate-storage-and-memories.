# Project state

- **Current phase:** all 7 build phases complete; waiting for the owner (commit/push decision, live-account setup)
- **Last verified checkpoint (2026-10-24):** 268 passed, 1 skipped; coverage 86%; ruff and mypy clean; `captioner demo` PASS from the repo venv and from a fresh venv using the built wheel; pip-audit clean
- **Next tasks (need the owner):**
  1. Decide whether to commit and push to `https://github.com/VARSHITH707/Automate-storage-and-memories.` (nothing is committed yet).
  2. Run `captioner setup` with real Telegram accounts, then the live checks in RELEASE_CHECKLIST items 12 to 15.
  3. Optional: `ollama pull gemma3:4b`, `winget install OliverBetz.ExifTool`, `uv pip install -e ".[whisper]"` for real local captions.

## Resume

```powershell
cd "Z:\Project online free server"
git status
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"
.venv\Scripts\python -m pytest -rs
.venv\Scripts\captioner demo
```

## Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Package installs and CLI works | VERIFIED | clean venv from wheel; `captioner --version` |
| `doctor` reports missing and configured dependencies | VERIFIED | `test_cli_doctor.py`; real run on this laptop |
| Offline demo runs without accounts | VERIFIED | `captioner demo` PASS |
| Catch-up and live events are idempotent | VERIFIED | `test_catch_up_and_live_events_share_one_idempotent_path` |
| Album grouping correct; per-item captions | VERIFIED | album tests |
| Unsupported files reported and ignored | VERIFIED | `test_unsupported_files_are_reported_once_and_ignored` |
| `#vault` / alias routing stops captioning and upload | VERIFIED | vault tests |
| SHA-256 dedupe prevents duplicate uploads | VERIFIED | `test_duplicate_hash_is_linked_and_never_uploaded_twice` |
| Originals byte-identical and immutable | VERIFIED | archive tests; demo hash re-check |
| Owner captions preserved exactly | VERIFIED | `test_owner_caption_is_preserved_exactly` |
| Generated captions local, grounded, short, not transcripts | VERIFIED (mocked models) | caption tests; real model run BLOCKED (no model pulled) |
| Location off by default; no online geocoding | VERIFIED | location tests |
| Approval authenticated and tied to current question | VERIFIED | stranger/bot/stale/no-reply tests |
| Rewrite limit and manual recovery | VERIFIED | rewrite-limit and review tests |
| Sidecars durable; derivative never mutates original | VERIFIED (mocked ExifTool) | archive tests; real ExifTool SKIPPED |
| First YouTube insert sets private | VERIFIED (mocked HTTP) | `test_upload_in_chunks_first_request_is_private` |
| Interrupted/ambiguous uploads recoverable without duplicates | VERIFIED (mocked HTTP) | resume/expired/unknown tests; retry tests |
| Pictures never uploaded | VERIFIED | album approval test |
| Bounded retries; failures inspectable | VERIFIED | backoff, flood-wait, quota, failure tests |
| Optional backup copies and verifies | VERIFIED (path real, rclone mocked) | backup tests |
| Weekly report and inactivity alert tested | VERIFIED | health tests |
| No secrets or media tracked or logged | VERIFIED | redaction tests; `git status --ignored` |
| Tests run; CI configured and checked | IMPLEMENTED | tests run locally; CI not run on GitHub (not pushed) |
| Docs exist and match implementation | VERIFIED | CLI options and doc test selectors executed |
| Unexercised live integrations marked NOT CONFIGURED/BLOCKED | VERIFIED | BUILD_HISTORY status table |
