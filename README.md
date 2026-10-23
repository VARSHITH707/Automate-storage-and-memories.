# Private Media Captioner

A free, open-source app that runs on your own laptop. You post photos and videos into a private Telegram channel; the app saves the original files on your laptop, writes a short "memory" caption for each one with AI that runs locally, asks you to approve the caption in a Telegram chat, and uploads approved videos to your YouTube channel as **private**.

There is no server, no website and no cloud AI. Nothing is uploaded anywhere except the transfers listed in [PRIVACY.md](PRIVACY.md).

> Status: version 0.1.0. The offline demo and the automated test suite pass (see BUILD_HISTORY for the latest count). Live Telegram and YouTube use has not yet been tested with real accounts; see [BUILD_HISTORY.md](BUILD_HISTORY.md). This is not production-ready software.

## What it does

1. You post media to your **Inbox** channel (use Telegram's *Send as file* to keep full quality).
2. On start the app **catches up** on everything posted while the laptop was off, then keeps watching.
3. Each original is **archived** byte-for-byte (SHA-256 checked, read-only) before anything else happens. Duplicate files are recognised and skipped.
4. Posts tagged `#vault` (or the alias `#private`) are copied to your **Vault** channel and stop there: no caption, no upload.
5. Your own text is used **exactly** as the caption. Otherwise a local model looks at the photo, or at frames and speech of the video, and writes 2 to 3 short lines. Speech is used only to understand the video, never shown as the caption.
6. A helper **bot** asks you in a private chat: reply `ok`, `change: <what to change>`, or type the caption you want. Albums get one numbered review.
7. The approved caption is saved in a JSON file next to the original. Videos are uploaded to YouTube as private, with the caption as the description. Pictures are never uploaded anywhere.
8. A weekly health report arrives in the bot chat, and you get an alert if something goes wrong.

## Requirements

- Windows 10/11, macOS or Linux; Python 3.11 or newer; about 16 GB RAM recommended (no GPU needed).
- [ffmpeg](https://ffmpeg.org) (required for videos), [Ollama](https://ollama.com) (local AI), and optionally [ExifTool](https://exiftool.org) (metadata copies of pictures) and faster-whisper (video speech).
- A Telegram account, two private channels you create, and a bot you make with @BotFather.
- Optional: a Google account and your own Google OAuth "Desktop app" client for YouTube uploads.

## Install (Windows PowerShell)

```powershell
winget install Gyan.FFmpeg
winget install Ollama.Ollama
winget install OliverBetz.ExifTool        # optional
git clone https://github.com/VARSHITH707/Automate-storage-and-memories..git captioner
cd captioner
uv venv --python 3.11 .venv               # or: py -3.11 -m venv .venv
uv pip install --python .venv -e .        # add ".[whisper]" to use video speech
.venv\Scripts\captioner --help
```

Without uv: `.venv\Scripts\python -m pip install -e .`

## Try it first: offline demo

```powershell
.venv\Scripts\captioner demo
```

The demo generates sample pictures (and a short test video when ffmpeg is installed), runs the real pipeline with **mocked** Telegram, bot, AI and YouTube, and prints `Result: PASS`. It sends nothing anywhere.

## Models and their licenses

The app does not ship any model weights. Defaults (change them in `config.toml`):

| Role | Default | Size | License (check before use) |
|---|---|---|---|
| Vision and text | `gemma3:4b` (Ollama) | about 3.4 GB | Gemma Terms of Use |
| Low-RAM alternative | `qwen3-vl:2b` (Ollama) | about 1.9 GB | see `ollama show qwen3-vl:2b --license` |
| Speech (optional) | faster-whisper `small`, CPU int8 | about 0.5 GB | see the Systran model card on Hugging Face |

Download a model: `ollama pull gemma3:4b`. The MIT license of this app does not change the license of any model.

## Set up

```powershell
.venv\Scripts\captioner setup
```

The wizard runs these steps and tests each one; rerun one with `captioner setup --step <name>`:

| Step | What you do |
|---|---|
| `archive` | Choose the folder for originals |
| `telegram` | Enter **your own** api_id and api_hash from https://my.telegram.org, then log in with the code Telegram sends you |
| `channels` | Pick your private Inbox and Vault. Setup stops if anyone else can post in the Inbox |
| `bot` | Paste the bot token from @BotFather, then press START in the bot chat |
| `models` | Checks Ollama, offers to download missing models, and (if installed) the speech model |
| `tools` | Checks ffmpeg, ffprobe and ExifTool |
| `youtube` | Optional: your OAuth client JSON and a browser sign-in |
| `backup` | Optional: a folder (external drive) or an rclone remote |
| `preferences` | Caption language, location (off by default), rewrite limit, report interval |

**Never paste keys, tokens or login codes into any chat, including AI assistants.** You type them only into the setup wizard. They are stored in Windows Credential Manager, macOS Keychain or the Linux Secret Service, never in a file. If no secure keyring exists, setup stops.

### YouTube (optional)

1. In https://console.cloud.google.com create a project and enable **YouTube Data API v3**.
2. Set up the OAuth consent screen (External) and add your own Google account as a test user.
3. Create an OAuth client ID of type **Desktop app** and download the JSON.
4. Run `captioner setup --step youtube` and give the path to that JSON. A browser opens for sign-in.

The app asks only for the `youtube.upload` scope, and every upload is created private in the first request. While your consent screen is in Testing, Google may expire the sign-in after about 7 days. If other people use your Google project, Google may require app verification. Uploads have their own daily quota (100 per project, per Google's docs as checked on 2026-10-23).

## Daily use

```powershell
.venv\Scripts\captioner doctor            # check everything
.venv\Scripts\captioner run               # catch up, then keep watching (Ctrl+C to stop)
.venv\Scripts\captioner run --once        # catch up, process, listen 20 s for replies, exit
.venv\Scripts\captioner status            # counts, pending reviews, failures (no captions shown)
.venv\Scripts\captioner retry 12          # reopen a failed job safely
.venv\Scripts\captioner review 12         # approve or set a caption from the terminal
```

Replies in the bot chat (always use **Reply** on the review message):

| Single item | Album |
|---|---|
| `ok` | `ok` (approves every caption shown) |
| `change: make it shorter` | `change 2: mention the rain` |
| any other text of 3+ words or several lines = your exact caption | `2: Our dog, asleep again.` |
| `1: Beach!` for a one- or two-word caption | |

Short replies such as "yes", "thanks" or "okk" are never taken as a caption; the bot asks you to confirm with `1: ...`. After 5 AI rewrites of an item, the bot asks for your exact words instead; `captioner review JOB_ID --reset-rewrites` resets the counter.

If an upload's result is unknown, check YouTube Studio, then either `captioner retry JOB_ID --uploaded-as VIDEO_ID` (it is there) or `captioner retry JOB_ID --confirm-not-uploaded` (it is not).

To start automatically, add a Windows Task Scheduler task that runs `<repo>\.venv\Scripts\captioner.exe run` at log-on. The app does not create one for you.

## Where things are

| What | Where (Windows) |
|---|---|
| Settings | `%LOCALAPPDATA%\PrivateMediaCaptioner\config.toml` (example: [config.example.toml](config.example.toml)) |
| Database | `%LOCALAPPDATA%\PrivateMediaCaptioner\captioner.db` |
| Telegram sessions | `%LOCALAPPDATA%\PrivateMediaCaptioner\sessions\` (treat like passwords) |
| Logs | `%LOCALAPPDATA%\PrivateMediaCaptioner\logs\captioner.log` (secrets redacted, no captions) |
| Originals | `<archive>\originals\YYYY\MM\<name>__<hash>.<ext>` (read-only) |
| Captions | `<original>.json` next to each original |
| Metadata copies | `<archive>\enriched\YYYY\MM\...` (pictures, needs ExifTool) |

macOS uses `~/Library/Application Support/PrivateMediaCaptioner`, Linux `~/.local/share/PrivateMediaCaptioner`. Set `CAPTIONER_HOME` to use another folder.

Every `captioner run` re-checks, at start and hourly, that only you can post in the Inbox. If an admin or bot was added, processing stops and the bot tells you why.

## Limitations

- Telegram private channels are invite-only but **not end-to-end encrypted**. Do not post extremely sensitive material.
- The app works only while the laptop is on; it catches up on start.
- Telegram-compressed photos and videos cannot be restored to full quality. Use *Send as file*.
- Small local models write plain captions and are slow on a CPU (minutes per video). The rules forbid invented names and places, but a model can still be wrong; that is why you approve every caption.
- HEIC pictures that Pillow cannot read get a metadata-only or manual caption.
- Instagram is not supported.

More: [PROJECT_GUIDE.md](PROJECT_GUIDE.md) (plain-English explanation), [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md), [PRIVACY.md](PRIVACY.md), [TROUBLESHOOTING.md](TROUBLESHOOTING.md), [PROFESSOR_DEMO.md](PROFESSOR_DEMO.md).

## Development

```powershell
uv pip install --python .venv -e ".[dev]"
.venv\Scripts\python -m pytest -rs
.venv\Scripts\ruff check .
.venv\Scripts\mypy captioner
```

This app uses the Telegram API (via Telethon); it is not an official Telegram product.

License: MIT ([LICENSE](LICENSE)).
