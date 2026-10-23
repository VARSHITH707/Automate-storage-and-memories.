# Private Media Captioner: instructions for Claude Code sessions

This repository is a local-first Python app. Every new session must:

1. Read `PROJECT_STATE.md` (current phase, last verified checkpoint, next task).
2. Read the latest entries at the bottom of `BUILD_HISTORY.md`.
3. Inspect the real files, `git status` and `git diff` before trusting either file.
4. Find the first incomplete acceptance criterion in `PROJECT_STATE.md`.
5. Continue from there. Do not restart or redo work already marked `VERIFIED`.
6. Run the relevant tests, then update both `BUILD_HISTORY.md` (append-only history, status table updated in place) and `PROJECT_STATE.md`.

## Commands

```powershell
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
.venv\Scripts\captioner --help
.venv\Scripts\captioner demo
```

Resume a stopped session with `claude --continue` (most recent) or `claude --resume` (pick a session).

## Rules that must never be broken

- Never ask the owner to paste secrets, login codes, tokens or private media into chat. Secrets go through `captioner setup` and the OS keyring.
- Never commit `config.toml`, `*.session`, client-secret JSON, archives, databases, model weights or logs.
- The archived original is immutable. Captions go in JSON sidecars; metadata goes in a separate derivative copy.
- YouTube uploads set `status.privacyStatus="private"` in the first insert request.
- Model output is caption text only. It never chooses actions, states or destinations.
- Mark a feature `VERIFIED` only after a command or test proves it. Never fabricate results.
- Do not push, publish, create cloud resources or run live-account tests without the owner's explicit go-ahead.
- Status labels: `NOT STARTED`, `IN PROGRESS`, `IMPLEMENTED`, `VERIFIED`, `BLOCKED`.
