# Troubleshooting

Start with `captioner doctor` and `captioner status`. Logs: `%LOCALAPPDATA%\PrivateMediaCaptioner\logs\captioner.log` (secrets redacted).

## Python and dependencies

- `captioner` not found: use `.venv\Scripts\captioner` or activate the venv with `.venv\Scripts\activate`.
- `python` points to another project: create this project's venv with `uv venv --python 3.11 .venv` or `py -3.11 -m venv .venv`.
- `pyproject.toml: Invalid statement (line 1, column 1)`: the file was saved with a byte-order mark by Windows PowerShell 5.1. Save it as UTF-8 without BOM.

## Telegram login and sessions

- "The Telegram session is not logged in": run `captioner setup --step telegram`.
- "The saved Telegram session belongs to a different account": you logged in with another account. Delete `sessions\owner.session` and run the telegram step again.
- Login code never arrives: Telegram sends it inside the Telegram app first. Never type it anywhere except the wizard.
- Your session was ended (Settings > Devices): run `captioner setup --step telegram` again.

## Bot chat

- Setup waits forever for /start: open the bot link the wizard printed, on the same account you logged in with, and press START.
- Your replies are ignored: you must use **Reply** on the bot's latest review message, in the bot's private chat. Replies from other accounts, or to older questions, are ignored on purpose.
- "That review is no longer active": a newer review replaced it; reply to the newest one.
- A reply sent while the laptop was off may be delivered late or not at all. Reply again after `captioner run` starts.

- The bot answers "To use ... as the caption, reply: 1: ...": one- and two-word replies are confirmed first so "yes" or "thanks" never become a caption. Reply `1: <caption>`.
- The bot answers "Did you mean ok?": reply exactly `ok`.

## "Processing stopped for safety"

`captioner run` found another admin or a bot in the Inbox (it checks at start and hourly). Remove them in the channel's admin settings, then start `captioner run` again. Nothing from the Inbox was processed while it was unsafe.

## Channel permissions

- Setup says other admins or bots can post in the Inbox: remove them, or the app cannot know who posted. The helper bot must not be an admin of the Inbox.
- "The Inbox is public": remove its @username (make the channel private).
- "Your account cannot post in the Vault": make your account an admin of the Vault.

## Catch-up

The checkpoint in `captioner status` is the last scanned message id. Posts after it are processed on the next start. Every scan is safe to repeat; nothing is processed twice.

## FloodWait

Telegram sometimes asks clients to wait. Waits up to 60 s are slept automatically; longer ones are scheduled (`flood_wait` in `status`). Do not restart repeatedly; that makes it worse.

## Compressed files

Review messages say "this looks Telegram-compressed" for photos sent normally. Quality cannot be restored. Send again with *Send as file*; the app will archive the new original (it has a different fingerprint).

## Ollama and models

- "Ollama is not reachable": start the Ollama app, then `captioner doctor`.
- "is not pulled": `ollama pull gemma3:4b` (or set another model in `config.toml`).
- Captions take very long or time out: use a smaller model (`qwen3-vl:2b`), lower `caption.max_frames`, or raise `caption.model_timeout_s`. After 5 failed attempts the item asks for your own caption instead of failing.
- "No caption yet": the models had nothing reliable to say, or the file could not be analysed. Reply with your caption.
- Video speech is not used although faster-whisper is installed: the speech model is downloaded only in setup. Run `captioner setup --step models` and accept the speech-model download.
- Telegram drops after sleep or a Wi-Fi change: `captioner run` keeps reconnecting by itself; `run --once` gives up after 5 attempts.

## CPU and RAM

With 16 GB RAM keep one model for both roles (the default). Close other heavy apps. Whisper runs in a separate process and frees its memory after each video.

## ffmpeg

- doctor shows ffmpeg missing: `winget install Gyan.FFmpeg`, then open a new terminal.
- "Not a readable video": the file is damaged or not a video; the item asks for a manual caption.

## ExifTool

Optional. Missing means no `enriched/` metadata copies; captions are still saved in sidecars. Install: `winget install OliverBetz.ExifTool`. If a metadata copy fails, the bot tells you and the sidecar is kept.

## Keyring

- "No secure OS keyring is available": on Windows, Credential Manager must work; on Linux install and unlock GNOME Keyring or KWallet. The app never stores secrets in plain files.
- To remove stored secrets: Credential Manager > Windows Credentials > entries for `PrivateMediaCaptioner`.

## Disk space

"Not enough disk space": free space or move the archive (`captioner setup --step archive`). Keep at least `archive.min_free_mb` (1024 MB by default) free.

## Database locks

"database is locked": another `captioner run` may be active (the app refuses a second one with "already active"). Close it. `status`, `retry` and `review` can run while `run` is active.

## Google OAuth and YouTube

- "YouTube is not signed in" or "sign-in expired or was revoked": run `captioner setup --step youtube`. Consent screens in Testing may expire the sign-in after about 7 days; publishing the consent screen avoids that.
- HTTP 403 with `quotaExceeded`/`uploadLimitExceeded`: the daily upload quota is used up; the job retries after 6 hours, up to `run.max_attempts`.
- "Access blocked: app not verified": add your account as a test user, or complete Google's verification if others use your project.
- WARNING about privacy not `private`: open YouTube Studio and set the video to Private immediately, then report the issue.

## Interrupted or unknown uploads

- Normal interruptions resume automatically from the saved session.
- `upload_unknown`: the app cannot tell whether YouTube received the video. Run `captioner retry JOB_ID`. It asks YouTube about the saved session first. If it still cannot tell, check YouTube Studio: if the video IS there, run `captioner retry JOB_ID --uploaded-as VIDEO_ID` (the part after `youtu.be/`) and make sure it is Private; if it is NOT there, run `captioner retry JOB_ID --confirm-not-uploaded`.
- A caption with `<` or `>` uploads fine: those two characters are removed from the YouTube description only.

## Failed jobs

`captioner status` lists them with a reason. Fix the cause, then `captioner retry JOB_ID`. It reopens the job at the step that failed and never creates a duplicate upload.

## Rewrite limit

After `caption.max_rewrites` AI rewrites, reply with the exact caption, or run `captioner review JOB_ID` (approve, enter a caption, or `--reset-rewrites`).

## Optional backup

- "Backup folder is not available": connect the drive; queued copies retry with backoff.
- rclone errors: run `rclone config` to fix the remote; the app never sees rclone's credentials.
- After repeated failures the bot tells you; the main archive is never affected.
