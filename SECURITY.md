# Security

## Threat model

Protects against: accidental public uploads; strangers or bots influencing approvals; secrets leaking into Git, logs or chats; malicious filenames or media; instructions hidden in media steering the AI; duplicate or lost uploads after crashes.

Does not protect against: someone with full control of the owner's logged-in laptop account (they can read the keyring and session files); Telegram or Google themselves; a compromised Ollama install.

## Credentials

| Secret | Storage |
|---|---|
| Telegram api_id and api_hash (the owner's own) | OS keyring (`secrets.set_secret`) |
| Helper bot token | OS keyring |
| YouTube refresh token | OS keyring (JSON with client id, scope) |
| Telegram session files | app-data `sessions/`, owner-only permissions on macOS/Linux; per-user `%LOCALAPPDATA%` ACL on Windows |

- `secrets.backend_status()` refuses the fail, null, `keyrings.alt` and plaintext backends. Setup and `run` stop instead of falling back to plaintext.
- Secrets are typed with hidden input in `captioner setup`, never passed on the command line, never asked for in chat.
- A session file equals full access to the Telegram account. Never share or commit it.
- No developer credentials ship with the app. Each owner uses their own Telegram API keys and Google OAuth client.

## Input validation

- File types come from magic bytes (`router.sniff`), not names or MIME types. Anything else is reported and ignored.
- Archive names come from `router.safe_stem` (ASCII letters, digits, `-`, `_`, at most 60 characters, Windows reserved names replaced) plus the hash. Paths are checked to stay inside the archive root.
- File size is capped (`archive.max_file_mb`), and free disk space is checked before download and archive.
- Pillow's decompression-bomb protection stays on; unreadable images fall back to metadata or a manual caption.
- ffprobe validates videos before frames are taken; every tool call has a timeout.

## Safe subprocesses

`tools.run` takes an argument list, resolves the program from PATH only (never from the current folder, unlike `shutil.which` on Windows), never uses a shell, and always sets a timeout. ffmpeg and ffprobe run with `-nostdin -protocol_whitelist file`, so a crafted playlist cannot make them fetch network URLs; audio extraction stops at 30 minutes. Values that reach command lines (model names, rclone remotes, backup paths) are validated so they cannot look like options. Caption text is passed to ExifTool through a temporary UTF-8 file (`-TAG<=file`), never as a command-line value. Whisper runs as `python -I -m captioner.models whisper <audio> <model>` in an isolated child process (nothing is imported from the current folder), so a timeout can stop it; it loads only already-downloaded weights.

## Immutable archive

Originals are copied, hash-verified, renamed atomically and set read-only. Nothing in the app writes to an original again. Captions go to sidecars; metadata goes to a separate copy whose creation re-checks the original's hash. Cleanup deletes only one job's folder inside the app's temp root (`archive.remove_job_temp` refuses anything else), and on start it removes only the app's own `.incoming-*` temp files.

## Access control

- Intake: only the configured Inbox; messages via bots or from bot senders are ignored. A broadcast Inbox must have the owner as its only admin (setup fails closed otherwise), and `captioner run` re-checks this at start and every hour, stopping if anyone else could post; a group Inbox accepts only the owner's own messages.
- Approvals: only replies in the bot's private chat, from the owner's user id, that reply to a currently stored review question. Stale, unrelated or ambiguous replies change nothing.
- The owner client writes only to the Vault.

## AI boundaries

Model input is wrapped in `<evidence>` with instructions to ignore any commands inside it; angle brackets in untrusted text are neutralised so it cannot close that block. Captions containing links are rejected. Short single-item replies ("yes", "okk") are never approved as captions without an explicit `1:`. Model output is validated as 2 to 3 short lines and stored only as caption text; it never chooses a state, destination or action. Captions that copy 8 consecutive words of the transcript are rejected. The Ollama URL must be a loopback address. The owner approves every caption before it leaves the laptop.

## YouTube

Scope `youtube.upload` only. `status.privacyStatus = "private"` is set in the first insert request (`youtube.insert_body`) and the returned privacy is checked; anything else triggers an alert. Resumable sessions are saved before completion, and an uncertain result becomes `upload_unknown` instead of a second upload.

## Logging

All handlers, including those receiving Telethon and Google library warnings, use `secrets.RedactingFormatter`, which redacts the whole formatted line including tracebacks (bot tokens, Google access/refresh tokens and `GOCSPX-` client secrets, 32-hex secrets, phone numbers and other 10-15 digit numbers, `token=`/`password=`/`code=` values). Logs record job ids and states, not captions, filenames or media.

The YouTube resumable session URI is stored in the local database (in the per-user app-data folder) so an upload can resume; it only grants access to that one unfinished upload.

## Non-goals

End-to-end encryption of Telegram content; protection from a compromised OS account; multi-user access; any hosted service.

## Reporting a problem

Open a GitHub issue without secrets or personal media, or contact the repository owner privately for anything sensitive.
