# Privacy

## What stays on your laptop

- Original files, captions (sidecars), metadata copies, the database, logs and transcripts.
- All AI work: Ollama runs locally (the app refuses any non-local Ollama address, ignores proxy settings for it, and refuses redirects) and faster-whisper runs on your CPU.
- YouTube receives a copy of the caption with `<` and `>` removed (YouTube rejects them); the archive keeps your exact words.
- Speech transcripts exist only inside the job's temporary folder while a caption is made. They are not saved in sidecars and are deleted with the job's temp files.

## What leaves your laptop

| Destination | What | When |
|---|---|---|
| Telegram (your account) | Reads your Inbox; copies `#vault` items to your Vault | Always, while running |
| Telegram (your bot's private chat with you) | Review questions with file names, message ids, dates and proposed captions; notices; weekly reports (counts only, no captions) | Always, while running |
| YouTube (your channel) | Approved videos with their caption as the description, created as private | Only if you enable YouTube |
| Your backup folder or rclone remote | Copies of originals | Only if you enable backup |
| Ollama / Hugging Face | Model downloads | Only when you confirm a download in `captioner setup` (normal runs load the speech model with `local_files_only`, so they never download) |

Nothing is sent to the app's author or any other service.

## Location

`include_location` is off by default. When on, only GPS coordinates already inside a photo's own EXIF data are used, and they are written as coordinates. The app never looks up addresses online.

## Telegram limits

Private channels are invite-only, but they are stored on Telegram's servers and are not end-to-end encrypted like Secret Chats. Do not post extremely sensitive material. Telegram's API terms forbid using Telegram data to train AI models; this app only runs local inference and never trains or fine-tunes on your media.

## Retention and deletion

- Originals and sidecars stay until you delete them; the app never deletes archived files.
- Temporary files (downloads, frames, audio, thumbnails) are deleted when a job finishes or is vaulted.
- To remove everything: delete the archive folder, the app-data folder (`%LOCALAPPDATA%\PrivateMediaCaptioner` on Windows), and the keyring entries named `PrivateMediaCaptioner` in Credential Manager. Revoke the app's Google access at https://myaccount.google.com/permissions and end the Telegram session under Settings > Devices.

## Session files

The `sessions` folder holds your Telegram login. Anyone with those files can use your Telegram account. Keep them out of Git, cloud sync folders and backups you share.
