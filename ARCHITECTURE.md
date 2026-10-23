# Architecture

One Python process (`captioner run`) on the owner's laptop. Adapters talk to the outside world; `pipeline.py` moves each job through an explicit state machine stored in SQLite. Every name below is a real module or state in the code.

## Components

```mermaid
flowchart LR
    phone[Owner's phone] -->|posts media| inbox[(Telegram Inbox channel)]
    subgraph laptop[Owner's laptop]
        tg[telegram.py<br/>owner user client] --> pipe[pipeline.py<br/>state machine]
        bot[bot.py<br/>helper bot] <--> pipe
        pipe --> router[router.py<br/>classify, markers, names]
        pipe --> arc[archive.py<br/>originals, sidecars, copies]
        pipe --> cap[captions.py]
        cap --> photo[photo.py<br/>EXIF, thumbnails]
        cap --> video[video.py<br/>ffprobe, ffmpeg]
        cap --> models[models.py<br/>Ollama + whisper worker]
        pipe --> yt[youtube.py<br/>resumable private upload]
        pipe --> bk[backup.py<br/>optional copy]
        pipe --> health[health.py]
        pipe --> db[(jobs.py<br/>SQLite)]
        sched[scheduler.py<br/>run loop, lock] --> pipe
        cli[cli.py] --> sched
        cli --> wiz[wizard.py]
        cli --> doc[doctor.py]
        cli --> rec[recovery.py]
        sec[secrets.py<br/>OS keyring] --- wiz
        sec --- sched
    end
    inbox --> tg
    tg -->|#vault copies| vault[(Telegram Vault channel)]
    bot <-->|private chat| owner[Owner]
    models <-->|127.0.0.1 only| ollama[Ollama server]
    yt --> youtube[(YouTube, private)]
    bk --> drive[(Folder or rclone remote)]
```

## Data flow for one item

```mermaid
sequenceDiagram
    participant I as Inbox
    participant P as pipeline
    participant A as archive
    participant M as local models
    participant B as bot (owner chat)
    participant Y as YouTube
    I->>P: scan from checkpoint / live NewMessage
    P->>P: register_message (idempotent, one transaction)
    P->>I: download original to tmp/job-N/download
    P->>A: sniff type, SHA-256, dedupe, copy + verify + atomic rename
    alt #vault or #private
        P->>I: send_to_vault (tagged "#pmc <hash>")
    else owner wrote text
        P->>P: caption = owner text, exactly
    else no text
        P->>M: frames/photo -> vision; audio -> whisper (context only); text model -> 2-3 lines
    end
    P->>B: one review per item or album
    B->>P: reply (owner only, to the current question)
    P->>A: approvals row, then JSON sidecar (+ metadata copy for pictures)
    P->>Y: videos only: insert with privacyStatus=private, resumable
    P->>B: link or error
```

## Job states

Transitions are checked centrally in `jobs.TRANSITIONS` and applied with compare-and-set (`UPDATE ... WHERE state = ?`).

```mermaid
stateDiagram-v2
    [*] --> new
    new --> downloaded
    new --> ignored_unsupported
    downloaded --> archived
    downloaded --> duplicate
    downloaded --> ignored_unsupported
    downloaded --> new: temp file lost
    archived --> vaulted
    archived --> captioning
    archived --> awaiting_approval: owner text
    captioning --> awaiting_approval
    awaiting_approval --> captioning: change request
    awaiting_approval --> approved
    approved --> uploading: video + YouTube on
    approved --> done: picture, or YouTube off
    uploading --> done
    uploading --> upload_unknown
    upload_unknown --> done: session confirms, or owner records the video id
    upload_unknown --> uploading: retry (resume or confirmed re-upload)
    new --> failed
    downloaded --> failed
    archived --> failed
    captioning --> failed
    approved --> failed
    uploading --> failed
    awaiting_approval --> failed
    failed --> new: captioner retry
    failed --> archived: captioner retry
    failed --> captioning: captioner retry
    failed --> awaiting_approval: captioner retry
    failed --> approved: captioner retry
    failed --> uploading: captioner retry
    vaulted --> [*]
    duplicate --> [*]
    ignored_unsupported --> [*]
    done --> [*]
```

`captioner retry` reopens a failed job at the step that failed (`recovery.RESUME_FROM`); a failure while archiving restarts from a fresh download.

## Tables

```mermaid
erDiagram
    albums ||--o{ inbox_messages : groups
    albums ||--o{ jobs : groups
    assets ||--o{ inbox_messages : "same file"
    inbox_messages ||--|| jobs : "one job per media message"
    assets ||--o{ jobs : archived_as
    jobs ||--o{ approvals : snapshot
    jobs ||--o{ uploads : attempts
    assets ||--o{ backup_jobs : copies
    albums { int id PK; int chat_id; int grouped_id; text last_item_at }
    assets { int id PK; text sha256 UK; text archive_path UK; text kind; int size; text archived_at }
    inbox_messages { int id PK; int chat_id; int message_id; int grouped_id; text text; int asset_id FK }
    jobs { int id PK; int inbox_message_id UK; text state; text caption; int rewrite_count; int retry_count; int question_id; int item_no }
    approvals { int id PK; int job_id FK; text caption; int owner_id; int question_id; text approved_at }
    uploads { int id PK; int job_id FK; int attempt; text state; text session_uri; int final_chunk_sent; text video_id; text privacy_status }
    backup_jobs { int id PK; int asset_id FK; text destination; text state; int checksum_ok; int retry_count }
    app_state { text key PK; text value }
```

`inbox_messages` is unique on `(chat_id, message_id)`; `assets` is unique on `sha256`. A repeated file arrives as a new message and job, which ends in `duplicate` linked to the existing asset.

## Security boundaries

```mermaid
flowchart TB
    subgraph trusted[Trusted: owner's laptop]
        keyring[(OS keyring:<br/>api id/hash, bot token,<br/>YouTube refresh token)]
        appdata[(App data: config, DB,<br/>sessions, logs)]
        code[captioner code]
    end
    subgraph untrusted[Untrusted input: treated as data only]
        media[Media files and their text]
        transcript[Speech transcripts]
        modelout[Model output]
        replies[Bot chat messages]
    end
    media --> code
    transcript -->|inside &lt;evidence&gt;| code
    modelout -->|validated caption text only| code
    replies -->|owner id + private chat + current question| code
    code -->|owner Inbox read, Vault write| telegram[Telegram]
    code -->|youtube.upload scope, private| google[YouTube]
    code -->|loopback only| ollama[Ollama]
```

## Retries and backoff

```mermaid
flowchart LR
    step[step raises] --> kind{classify_error}
    kind -->|flood_wait| wait[wait Telegram's seconds]
    kind -->|network, io, model_unavailable, quota| backoff[retry_count+1,<br/>wait = min 30 s x 2^n, 1 h, times jitter 0.5-1.5]
    kind -->|archive, missing_source, internal| failed[failed + owner notified]
    wait --> cap{retry_count >= max_attempts?}
    backoff --> cap
    cap -->|yes| failed
    cap -->|no| later[runs again when due]
```

Upload quota errors wait 6 hours. Model unavailability falls back to "needs your caption" after the last attempt instead of failing. Backups retry separately in `backup_jobs` and never affect the job.

## Persistent artifacts

| Artifact | Written by | Atomic how |
|---|---|---|
| `archive/originals/YYYY/MM/<stem>__<hash12>.<ext>` | `archive.archive_original` | temp file in the same folder, fsync, hash check, `os.replace`, then read-only |
| `<original>.json` sidecar | `archive.write_sidecar` | temp file, fsync, `os.replace` |
| `archive/enriched/...` metadata copy | `archive.write_metadata_copy` | copy, ExifTool, read back, `os.replace`; original hash re-checked |
| `config.toml` | `config.save` | temp file, fsync, `os.replace` |
| `captioner.db` (WAL) | `jobs` | `BEGIN IMMEDIATE` transactions |
| `sessions/*.session` | Telethon | owner-only permissions where the OS supports them |
| `logs/captioner.log` | `secrets.setup_logging` | rotating, redacted |
