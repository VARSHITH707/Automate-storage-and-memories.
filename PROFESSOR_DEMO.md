# Demonstration script (8 to 12 minutes)

Everything in parts 1 to 4 is offline and uses mocked Telegram, bot, AI and YouTube. Say so out loud. Show part 5 only if real accounts have been set up with `captioner setup`.

## 0. Before the session (not on screen)

```powershell
cd "<repo>"
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"
```

## 1. The problem and the idea (1 minute)

Speaker notes: phones get lost; this keeps originals safe on the laptop, adds memory captions with AI that runs locally, asks the owner to approve, and uploads videos privately. No server, no cloud AI.

## 2. Commands and health check (2 minutes)

```powershell
.venv\Scripts\captioner --help
.venv\Scripts\captioner doctor --offline
```

Expected: seven commands (setup, doctor, run, status, demo, retry, review). `doctor` lists OK/WARN/FAIL lines. On a fresh machine the Telegram and bot entries are FAIL ("not stored") and the exit code is 1. Notes: missing optional tools are warnings, never crashes; secrets live in the OS keyring.

## 3. Offline demo, the whole flow (3 minutes)

```powershell
.venv\Scripts\captioner demo --output demo-output
```

Expected output (the archive path will differ):

```
Private Media Captioner offline demo (all external services are mocked)
1. Inbox has 7 posts (synthetic). Catch-up scan registers them.
   new jobs: 7  (a second scan finds 0: idempotent)
2. After processing: {'awaiting_approval': 4, 'duplicate': 1, 'ignored_unsupported': 1, 'vaulted': 1}
   Vault copies (mocked Telegram): 1; review messages sent: 3
3. Owner replies (mocked): ok, change 2: ..., a stale ok (ignored), ok
   Final states: {'done': 4, 'duplicate': 1, 'ignored_unsupported': 1, 'vaulted': 1}
4. Archived originals: 5 (hash re-checked: all match); sidecars: 4
   Mock YouTube insert: title='A quiet moment kept in this video.' privacyStatus=private
   Archive folder: ...\demo-output\archive
Result: PASS
```

(Without ffmpeg there is no video: 6 posts and 3 done.) Speaker notes, line by line: the second scan finds nothing (idempotent); the duplicate photo was recognised by its fingerprint; `#vault` stopped before AI; the text file was ignored; the album got one review; "change 2" rewrote only item 2; a reply to the old question was ignored; the upload body says private.

```powershell
$env:CAPTIONER_HOME = "$PWD\demo-output"
.venv\Scripts\captioner status
Get-ChildItem demo-output\archive -Recurse | Select-Object FullName
Get-Content (Get-ChildItem demo-output\archive -Recurse -Filter *.json | Select-Object -First 1).FullName
Remove-Item Env:CAPTIONER_HOME
```

Show: read-only originals named `<name>__<hash>.<ext>`, a JSON sidecar with `schema_version`, `asset_sha256`, `caption`, `caption_source`, and no transcript.

## 4. Failure and recovery, from the tests (2 minutes)

```powershell
.venv\Scripts\python -m pytest tests\integration\test_pipeline.py -k "unknown_upload or lost_temp or flood_wait or current_question or anyone_else or vault_retry" -v
.venv\Scripts\python -m pytest tests\unit\test_youtube.py -v
```

Speaker notes (all mocked): an upload whose result is unknown waits for the owner instead of uploading twice; a lost temp file is downloaded again; Telegram flood waits are respected; stale or stranger replies change nothing; a Vault copy is not sent twice after a crash; the very first YouTube request is private and an interrupted upload resumes the same session.

## 5. Live run (only if accounts are configured, 2 minutes)

```powershell
.venv\Scripts\captioner doctor
.venv\Scripts\captioner run --once
```

Post a photo to the Inbox from a phone first. Expected: a review message in the bot chat. Reply `ok`, run `captioner run --once` again, and show the sidecar. Do not show tokens, phone numbers, account ids or private photos on screen.

## 6. Close (1 minute)

What is verified (the automated tests and the demo; counts in BUILD_HISTORY) and what is not (live Telegram and YouTube until configured; real model quality). Point to `BUILD_HISTORY.md` for the honest record.
