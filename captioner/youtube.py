"""YouTube private uploads (google-api-python-client), resumable and safe against duplicate inserts.

Rules: the first insert request sets status.privacyStatus = "private"; the resumable session URI and
progress are saved durably; an outcome that cannot be determined becomes `upload_unknown`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import secrets

log = logging.getLogger("captioner.youtube")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint, not a secret
CATEGORY_PEOPLE_BLOGS = "22"


class YouTubeAuthError(RuntimeError):
    pass


def insert_body(title: str, description: str) -> dict:
    return {
        "snippet": {"title": title, "description": description, "categoryId": CATEGORY_PEOPLE_BLOGS},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }


def authorize(client_secret_path: Path) -> None:
    """Browser sign-in (loopback redirect + PKCE via google-auth-oauthlib). Stores only the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    if not creds.refresh_token:
        raise YouTubeAuthError("Google did not return a refresh token; remove the app's access and try again")
    secrets.set_secret(secrets.YOUTUBE_TOKEN, json.dumps({
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "token_uri": creds.token_uri or TOKEN_URI,
        "scopes": SCOPES,
    }))


def load_credentials() -> Any:
    from google.oauth2.credentials import Credentials

    raw = secrets.get_secret(secrets.YOUTUBE_TOKEN)
    if not raw:
        raise YouTubeAuthError("YouTube is not signed in. Run `captioner setup --step youtube`.")
    data = json.loads(raw)
    return Credentials(token=None, refresh_token=data["refresh_token"], client_id=data["client_id"],
                       client_secret=data.get("client_secret"), token_uri=data.get("token_uri", TOKEN_URI),
                       scopes=data.get("scopes", SCOPES))


def authorized_http(creds: Any, timeout: float = 120) -> Any:
    import google_auth_httplib2
    import httplib2

    return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))


def build_service(http: Any) -> Any:
    from googleapiclient.discovery import build

    return build("youtube", "v3", http=http, cache_discovery=False, static_discovery=True)


# -- outcomes -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class UploadDone:
    video_id: str
    privacy_status: str | None


@dataclass(frozen=True)
class UploadUnknown:
    reason: str


@dataclass(frozen=True)
class UploadRetry:
    category: str
    message: str
    delay_s: float | None = None


@dataclass(frozen=True)
class UploadFailed:
    category: str
    message: str


UploadOutcome = UploadDone | UploadUnknown | UploadRetry | UploadFailed


class UploadStore(Protocol):
    def latest(self, job_id: int) -> dict | None: ...
    def start_attempt(self, job_id: int, total: int) -> int: ...
    def save_session(self, upload_id: int, uri: str) -> None: ...
    def save_progress(self, upload_id: int, sent: int, final_chunk_sent: bool) -> None: ...
    def finish(self, upload_id: int, video_id: str, privacy: str | None) -> None: ...
    def mark(self, upload_id: int, state: str, error: str = "") -> None: ...


def _http_error_outcome(exc: Any) -> UploadOutcome:
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    text = getattr(exc, "content", b"")
    text = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
    if any(r in text for r in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded")):
        return UploadRetry("quota", "YouTube upload quota reached; it resets daily. Will retry later.",
                           delay_s=6 * 3600)
    if status in (401, 403):
        return UploadFailed("auth", f"YouTube refused the request (HTTP {status}). Check the account and "
                                    "run `captioner setup --step youtube`.")
    if status >= 500 or status == 0:
        return UploadRetry("network", f"YouTube server error (HTTP {status})")
    return UploadFailed("rejected", f"YouTube rejected the upload (HTTP {status})")


class YouTubeUploader:
    def __init__(self, service_factory: Any, http_factory: Any, chunk_bytes: int):
        self.service_factory = service_factory  # () -> youtube service
        self.http_factory = http_factory  # () -> authorized http object
        self.chunk = chunk_bytes

    def query_session(self, uri: str, size: int) -> tuple[str, Any]:
        """Ask YouTube what happened to an earlier session: complete / incomplete / expired / unknown."""
        try:
            resp, content = self.http_factory().request(
                uri, "PUT", headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"})
        except Exception as exc:  # noqa: BLE001 - transport failures mean "could not check"
            return "unknown", f"status check failed: {type(exc).__name__}"
        status = int(resp.status)
        if status in (200, 201):
            body = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
            return "complete", body
        if status == 308:
            rng = resp.get("range")
            return "incomplete", int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
        if status in (404, 410):
            return "expired", None
        return "unknown", f"status check returned HTTP {status}"

    def upload(self, job_id: int, path: Path, title: str, description: str, store: UploadStore) \
            -> UploadOutcome:
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        size = path.stat().st_size
        resume_uri, offset, upload_id = None, 0, None
        latest = store.latest(job_id)
        if latest and latest["state"] == "done":
            return UploadDone(latest["video_id"], latest["privacy_status"])
        if latest and latest["state"] == "in_progress":
            if latest["session_uri"]:
                state, value = self.query_session(latest["session_uri"], size)
                if state == "complete":
                    privacy = (value.get("status") or {}).get("privacyStatus")
                    store.finish(latest["id"], value["id"], privacy)
                    return UploadDone(value["id"], privacy)
                if state == "incomplete":
                    resume_uri, offset, upload_id = latest["session_uri"], value, latest["id"]
                elif state == "expired" and latest["final_chunk_sent"]:
                    return UploadUnknown("The upload session expired after the last bytes were sent.")
                elif state == "expired":
                    store.mark(latest["id"], "abandoned", "session expired before completion")
                else:
                    return UploadRetry("network", str(value))
            elif latest["final_chunk_sent"] or size <= self.chunk:
                return UploadUnknown("An earlier attempt may have completed, and no session was saved to check.")
            else:
                store.mark(latest["id"], "abandoned", "no session saved; nothing could have completed")

        import httplib2
        from google.auth.exceptions import TransportError

        request: Any = None
        saved_uri = resume_uri

        def after_error() -> None:
            """Keep the session if one was opened; if none was, nothing can have been uploaded."""
            nonlocal saved_uri
            if request is None or upload_id is None:
                return
            if request.resumable_uri and request.resumable_uri != saved_uri:
                store.save_session(upload_id, request.resumable_uri)
                saved_uri = request.resumable_uri
            elif not request.resumable_uri:
                store.mark(upload_id, "abandoned", "failed before an upload session existed")

        try:
            service = self.service_factory()
            media = MediaFileUpload(str(path), mimetype="application/octet-stream", chunksize=self.chunk,
                                    resumable=True)
            request = service.videos().insert(part="snippet,status", body=insert_body(title, description),
                                              media_body=media)
            if upload_id is None:
                upload_id = store.start_attempt(job_id, size)
            if resume_uri:
                request.resumable_uri, request.resumable_progress = resume_uri, offset
            response = None
            while response is None:
                if size - request.resumable_progress <= self.chunk:
                    store.save_progress(upload_id, request.resumable_progress, True)
                _status, response = request.next_chunk(num_retries=3)
                if request.resumable_uri and request.resumable_uri != saved_uri:
                    store.save_session(upload_id, request.resumable_uri)
                    saved_uri = request.resumable_uri
                if response is None:
                    store.save_progress(upload_id, request.resumable_progress, False)
        except RefreshError:
            after_error()
            if upload_id is not None:
                store.mark(upload_id, "failed", "auth")
            return UploadFailed("auth", "YouTube sign-in expired or was revoked. Run `captioner setup --step youtube`.")
        except HttpError as exc:
            after_error()
            outcome = _http_error_outcome(exc)
            if upload_id is not None and isinstance(outcome, UploadFailed):
                store.mark(upload_id, "failed", outcome.category)
            return outcome
        except (OSError, TimeoutError, httplib2.HttpLib2Error, TransportError) as exc:
            after_error()
            return UploadRetry("network", f"Connection problem during upload: {type(exc).__name__}")
        video_id = response["id"]
        privacy = (response.get("status") or {}).get("privacyStatus")
        store.finish(upload_id, video_id, privacy)
        return UploadDone(video_id, privacy)
