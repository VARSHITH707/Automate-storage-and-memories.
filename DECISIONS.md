# Decisions

Defaults chosen where the brief was silent or contradictory. Each one is the simplest safe option.

| ID | Decision | Reason |
|---|---|---|
| D-01 | The repository root is the workspace folder itself, with the package in `captioner/`. | The GitHub repo is empty; a nested `private-media-captioner/` folder would add nothing. |
| D-02 | `#vault` is the canonical marker; `#private` is an alias enabled by default. Set `vault.aliases = []` in `config.toml` to disable it. Matching is case-insensitive on whole words. | Required by the prompt; the original workflow image used `#private`. |
| D-03 | Approvals go through the owner's private chat with the helper bot. A linked-discussion-group mode is not implemented. | Broadcast channels do not support reliable replies; the prompt makes the discussion mode optional. |
| D-04 | Telegram access uses two Telethon clients with separate session files: the owner account (read Inbox, download, copy to Vault) and the bot (all messages to the owner). | Required by the brief. |
| D-05 | Owner-only Inbox: setup fails closed unless the Inbox is a broadcast channel whose only admin is the owner, or a group where every accepted message's `sender_id` equals the owner. At runtime, messages via bots or from bot senders are ignored. | Broadcast posts do not reliably carry the human author. |
| D-06 | Only catch-up scans move the message checkpoint. Live events register messages through the same idempotent function but never advance the checkpoint; a periodic rescan closes any gap. | Prevents losing messages when a live event outruns an unfinished scan. |
| D-07 | An album is ready for review only when all of its known items are ready and no new item for that `grouped_id` arrived for `album_settle_seconds` (default 30). | Albums arrive as several messages; this handles the checkpoint boundary without guessing the album size. |
| D-08 | Captions are written in `caption_language` (default `English`). | Most videos have no owner text to detect a language from. |
| D-09 | If YouTube is disabled (`youtube.enabled = false`, the default until setup), approved videos finish as `done` after the sidecar is written. | YouTube stays optional until configured. |
| D-10 | The owner supplies their own Google OAuth Desktop client JSON. No shared client ID ships. | Shipping one requires the author to create a Google Cloud project, which is a cloud resource that needs explicit authorization. |
| D-11 | Uploads use `google-api-python-client` resumable media. The session URI is saved before bytes are sent. Resuming after a restart sets the saved URI on the request and asks the server for the received range. If the outcome cannot be determined, the job goes to `upload_unknown`; `captioner retry` checks the session first and re-uploads only with `--confirm-not-uploaded`. | No blind duplicate inserts; `youtube.upload` scope only. |
| D-12 | Privacy is verified from the `status.privacyStatus` returned by the insert response. A separate `videos.list` check is not done, because it needs a broader scope. | Minimum scope. |
| D-13 | The title is the first non-empty caption line, trimmed to 100 characters. Fallback: `Memory YYYY-MM-DD` from the file date, or the archive date. | Deterministic and documented. |
| D-14 | Caption validation: 2 or 3 non-empty lines, each at most 120 characters, with no run of 8 consecutive words copied from the transcript. Failing output gets one retry, then the item waits for a manual caption. | Grounded and short; prevents transcript leakage. Name invention is controlled by the prompt only and is a documented limitation. |
| D-15 | Images sent to the vision model are re-encoded as JPEG thumbnails (longest side 1024 px) in the temp folder. HEIC files that Pillow cannot open skip vision and use metadata or a manual caption. | Smaller and faster on CPU; avoids format gaps. |
| D-16 | `pipeline.py` (orchestration) and `demo.py` (offline demo) are added beyond the suggested layout. | Keeps orchestration out of the adapters and the demo out of the CLI. |
| D-17 | A failed job keeps its error category and message; `retry` resets it to the last safe state: before archive means re-download, after archive means the step that failed. | Failures stay inspectable and safely retryable. |
| D-19 | Extra modules beyond the suggested layout: `tools.py` (one safe subprocess helper), `doctor.py`, `wizard.py`, `recovery.py`, `captions.py` (caption orchestration over photo/video/models). | Keeps each file focused and under the size limit. |
| D-20 | Telegram stickers, voice notes, round videos, polls and locations are "unsupported"; link previews count as text. | Only real photos and videos are archived. |
| D-21 | `#vault` on any message of an album routes the whole album to the Vault (Telegram puts the album caption on one item). Vault copies carry a `#pmc <hash prefix>` caption so a retry finds an earlier copy instead of sending it twice. | Avoids captioning or uploading part of a private album; avoids duplicate Vault posts. |
| D-22 | Only one `captioner run` at a time (lock file in app-data). | Two processes would share sessions and the database. |
| D-23 | Line length 120 for ruff. | Readable without wrapping SQL. |
| D-24 | Single-item replies of one or two words on one line are not taken as captions; the owner confirms with `1: <caption>`. Near-misses of `ok` ("okk", "k") are asked about. | Security/code review: "yes" or "okk" must never be approved and uploaded as a caption. |
| D-25 | The uploaded YouTube description is the approved caption with `<`/`>` removed and cut to 5000 bytes; the archive and approval keep the exact caption. | YouTube rejects those characters; otherwise the job could never finish. |
| D-26 | `captioner run` re-checks the owner-only Inbox rule at start and hourly and stops if it fails. | Admins can change after setup (security review M3). |
| D-27 | Programs are resolved from PATH only, never the current folder; the whisper worker runs with `python -I`. | Windows `shutil.which` searches the current folder first (security review M2). |
| D-28 | Ollama requests bypass proxies and refuse redirects; only `http://` loopback URLs are accepted. | A system proxy would otherwise receive frames and transcripts (security review M1). |
| D-29 | Speech-model weights download only when the owner confirms it in setup; runs use `local_files_only`. | PRIVACY.md promise (security review L6). |
| D-30 | Telethon clients in `captioner run` retry connections forever (`connection_retries=-1`); `run --once` and setup keep the default 5. | Survive laptop sleep and Wi-Fi drops (code review M7). |
| D-18 | Default models: `gemma3:4b` for both vision and text through Ollama; `small` for faster-whisper, CPU int8. All configurable. | Fits a 16 GB laptop; one download covers both roles. |
