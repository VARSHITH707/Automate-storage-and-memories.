# Project guide (plain English)

## The problem

Phones get lost, stolen or broken, and the photos and videos on them go too. This project keeps a safe copy of every memory you care about on your own laptop, adds a short description so you remember the moment, and puts videos on your YouTube channel as private so you can watch them on any device.

## The journey of one video

1. On your phone you send a video to your private Telegram channel called the **Inbox**, using *Send as file*.
2. Your laptop is running `captioner run`. It notices the new post (or, if the laptop was off, finds it when it starts again).
3. It downloads the original file into a temporary folder and checks what it really is by reading its first bytes.
4. It calculates the file's **SHA-256** fingerprint. If that fingerprint is already in the archive, it is a duplicate and stops.
5. It copies the file into the **archive**, checks the copy has the same fingerprint, and makes it read-only. Your memory is now safe, before any AI runs.
6. If you wrote `#vault` in the post, it copies the file to your locked **Vault** channel and stops.
7. If you wrote your own text, that text is the caption, exactly as written.
8. Otherwise it takes up to 16 still frames, asks a local **vision model** what it sees, turns the speech into text with **Whisper** (only as background information), and asks a local **text model** to write 2 to 3 short lines.
9. The **helper bot** sends you the caption in a private chat. You reply `ok`, `change: ...`, or your own words.
10. The approved caption is saved in a small JSON file next to the original.
11. The video is uploaded to YouTube as **private**, and the bot sends you the link.

## How the parts fit

| Part | File | What it does |
|---|---|---|
| Command line | `cli.py` | The `captioner` commands |
| Setup wizard | `wizard.py` | Guides you through every account and folder, testing each step |
| Health check | `doctor.py` | Tells you what is missing and how to fix it |
| Run loop | `scheduler.py` | Connects, catches up, keeps working, stops cleanly on Ctrl+C |
| The brain | `pipeline.py` | Moves each item through its steps and records every step |
| Database | `jobs.py` | Remembers every message, file, step, approval and upload |
| Telegram account | `telegram.py` | Reads the Inbox, downloads files, copies to the Vault |
| Helper bot | `bot.py` | Talks to you: reviews, notices, reports |
| Rules | `router.py` | What kind of file it is, whether it is a Vault item, safe file names |
| Archive | `archive.py` | Safe copies, fingerprints, caption files, metadata copies |
| Captions | `captions.py`, `photo.py`, `video.py`, `models.py` | Look at the media and write captions with local AI |
| Approval | `approval.py` | Understands your replies and writes the review messages |
| YouTube | `youtube.py` | Private uploads that can resume after interruptions |
| Backup | `backup.py` | Optional extra copy to a drive or rclone remote |
| Reports | `health.py` | Status, weekly report, "nothing archived for 3 days" alert |
| Recovery | `recovery.py` | `retry` and `review` commands |
| Demo | `demo.py` | A full offline run with pretend services |
| Secrets | `secrets.py` | Uses your computer's password store; hides secrets in logs |

## Why speech is not the caption

People say many things in a video: background chatter, names, private details. A transcript would be long and could expose that. The app uses speech only to understand what is happening, and it rejects any caption that copies 8 words in a row from the transcript.

## Telegram sessions and the bot

The app uses **two** Telegram identities. Your own account (through a *session file*, which is like a saved login) is the only one that can read your private Inbox and download full-size files. The **bot** is a separate helper account; it only chats with you, so its messages can never be mistaken for your posts, and an approval only counts when you reply to its current question.

## Archive, sidecar and metadata copy

- **Archive original**: the untouched file, read-only, named `<name>__<fingerprint>.<ext>`.
- **Sidecar**: `<original>.json` with the caption, where it came from, when you approved it, and which models were used.
- **Metadata copy**: an extra copy of a picture with the caption written inside it (needs ExifTool), stored under `enriched/`. The original is never changed.

## Database and job states

Each media message becomes a **job**. Its state is one of: `new`, `downloaded`, `archived`, `captioning`, `awaiting_approval`, `approved`, `uploading`, `done`, or the special `duplicate`, `vaulted`, `ignored_unsupported`, `failed`, `upload_unknown`. A state only changes if it is still what the step expected, so two things can never move the same job at once.

## Idempotency and restart recovery

**Idempotent** means doing something twice has the same effect as doing it once. Registering a message, archiving a file, sending to the Vault and uploading are all written so a crash and a retry never repeat finished work: messages are unique by id, files by fingerprint, Vault copies are found again by a tag, and uploads resume their saved YouTube session.

## YouTube private uploads

The upload request itself says "private". The app sends the video in chunks, saves the upload session, and after a crash asks YouTube how much it already received. If YouTube cannot answer, the job waits as `upload_unknown` until you check YouTube Studio and run `captioner retry`. It never uploads a second copy on a guess.

## Monitoring and backup

Every week the bot sends counts (no captions). If posts are waiting but nothing has been archived for 3 days, you get an alert. The app only works while the laptop is on; it says so and catches up when it starts. An optional backup copies originals to a drive or rclone remote and checks each copy.

## Privacy promises and limits

Kept: AI runs locally; originals are never changed; uploads are private; location is off unless you turn it on; secrets live in your computer's password store. Limits: Telegram channels are not end-to-end encrypted; small local models can be wrong (that is why you approve); a compressed Telegram photo cannot be restored.

## Glossary

| Term | Meaning |
|---|---|
| Inbox | Your private Telegram channel where you post media |
| Vault | Your second private channel for `#vault` items |
| Session file | A saved Telegram login; treat it like a password |
| Bot | A helper Telegram account the app controls |
| SHA-256 | A fingerprint of a file; any change gives a different fingerprint |
| Sidecar | A small file stored next to another file, holding information about it |
| EXIF | Information a camera stores inside a photo (date, sometimes GPS) |
| Ollama | Free software that runs AI models on your own computer |
| Whisper | A speech-to-text model; here it runs on your CPU |
| OAuth | Google's way of letting an app act for you without seeing your password |
| Resumable upload | An upload that can continue where it stopped |
| Keyring | Your operating system's password store |
| Idempotent | Safe to repeat: twice has the same effect as once |
| Checkpoint | The id of the last Inbox message the app has scanned |

## A traced example

1. 09:00 you post `beach.mp4` (40 MB, as a file) to the Inbox. The laptop is off.
2. 18:00 you start `captioner run`. The scan reads messages after checkpoint 120, finds message 121, records it, and moves the checkpoint to 121 in the same database transaction. The bot says "Back online ... 1 new post(s)".
3. Job 57 downloads the file to `tmp/job-57/download/`. Its first bytes say MP4. Fingerprint `9f2c...` is new.
4. It is copied to `archive/originals/2026/10/beach__9f2c1a7b3e00.mp4`, checked, made read-only. Job 57 is `archived`.
5. No `#vault`, no text: job 57 is `captioning`. Two frames go to the vision model, the speech goes to Whisper, and the text model writes "Waves rolling onto an empty beach.\nA calm evening by the sea." Job 57 is `awaiting_approval`.
6. The bot sends "Review: 1 item ... Proposed caption: ...". You reply `ok`.
7. An approval row stores that exact caption, the sidecar `beach__9f2c1a7b3e00.mp4.json` is written, and job 57 is `uploading`.
8. The upload starts private, the session is saved, and chunks are sent. YouTube returns video id `abc123` with privacy `private`. Job 57 is `done`, the temp folder is deleted, and the bot sends `https://youtu.be/abc123`.
