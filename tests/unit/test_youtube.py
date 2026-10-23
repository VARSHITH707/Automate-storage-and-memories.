from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from captioner import youtube
from captioner.youtube import UploadDone, UploadFailed, UploadRetry, UploadUnknown

CHUNK = 256 * 1024
SIZE = 600_000
SESSION = "https://upload.test/upload/youtube/v3/videos?upload_id=abc"
DONE_BODY = json.dumps({"id": "VID123", "status": {"privacyStatus": "private"}})


class Recorder(HttpMockSequence):
    def __init__(self, responses):
        super().__init__(responses)
        self.calls: list[tuple[str, str, object, dict]] = []

    def request(self, uri, method="GET", body=None, headers=None, redirections=1, connection_type=None):
        self.calls.append((uri, method, body, dict(headers or {})))
        return super().request(uri, method, body, headers, redirections, connection_type)


class MemoryStore:
    def __init__(self, latest: dict | None = None):
        self.rows: list[dict] = [latest] if latest else []
        self.events: list[tuple] = []

    def latest(self, job_id):
        return self.rows[-1] if self.rows else None

    def start_attempt(self, job_id, total):
        row = {"id": len(self.rows) + 1, "state": "in_progress", "session_uri": None, "final_chunk_sent": 0,
               "video_id": None, "privacy_status": None}
        self.rows.append(row)
        self.events.append(("start", total))
        return row["id"]

    def _row(self, upload_id):
        return next(r for r in self.rows if r["id"] == upload_id)

    def save_session(self, upload_id, uri):
        self._row(upload_id)["session_uri"] = uri
        self.events.append(("session", uri))

    def save_progress(self, upload_id, sent, final):
        self._row(upload_id)["final_chunk_sent"] |= int(final)
        self.events.append(("progress", sent, final))

    def finish(self, upload_id, video_id, privacy):
        self._row(upload_id).update(state="done", video_id=video_id, privacy_status=privacy)
        self.events.append(("finish", video_id, privacy))

    def mark(self, upload_id, state, error=""):
        self._row(upload_id)["state"] = state
        self.events.append(("mark", state))


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "v.mp4"
    path.write_bytes(b"\x00" * SIZE)
    return path


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda s: None)  # googleapiclient backoff between retries


def uploader(service_http: Recorder | None, query_http: Recorder | None = None) -> youtube.YouTubeUploader:
    def service_factory():
        if service_http is None:
            raise AssertionError("no insert must be made")
        return build("youtube", "v3", http=service_http, static_discovery=True)

    return youtube.YouTubeUploader(service_factory, lambda: query_http, CHUNK)


def test_insert_body_is_private() -> None:
    body = youtube.insert_body("Title", "Desc")
    assert body["status"]["privacyStatus"] == "private"
    assert body["snippet"] == {"title": "Title", "description": "Desc", "categoryId": "22"}


def test_upload_in_chunks_first_request_is_private(video: Path) -> None:
    http = Recorder([
        ({"status": "200", "location": SESSION}, b""),
        ({"status": "308", "range": "bytes=0-262143"}, b""),
        ({"status": "308", "range": "bytes=0-524287"}, b""),
        ({"status": "200"}, DONE_BODY),
    ])
    store = MemoryStore()
    outcome = uploader(http).upload(1, video, "My title", "Line one\nLine two", store)
    assert outcome == UploadDone("VID123", "private")
    first_uri, method, body, headers = http.calls[0]
    assert method == "POST" and "uploadType=resumable" in first_uri
    sent = json.loads(body)
    assert sent["status"]["privacyStatus"] == "private"
    assert sent["snippet"]["description"] == "Line one\nLine two"
    assert headers["X-Upload-Content-Length"] == str(SIZE)
    assert ("session", SESSION) in store.events
    assert store.rows[-1]["final_chunk_sent"] == 1
    assert [c[1] for c in http.calls] == ["POST", "PUT", "PUT", "PUT"]


def test_resume_after_restart_continues_same_session(video: Path) -> None:
    latest = {"id": 1, "state": "in_progress", "session_uri": SESSION, "final_chunk_sent": 0,
              "video_id": None, "privacy_status": None}
    query = Recorder([({"status": "308", "range": "bytes=0-262143"}, b"")])
    service = Recorder([({"status": "308", "range": "bytes=0-524287"}, b""), ({"status": "200"}, DONE_BODY)])
    store = MemoryStore(latest)
    outcome = uploader(service, query).upload(1, video, "t", "d", store)
    assert outcome == UploadDone("VID123", "private")
    assert query.calls[0][3]["Content-Range"] == f"bytes */{SIZE}"
    assert all(method == "PUT" for _, method, _, _ in service.calls)  # no new insert
    assert service.calls[0][3]["Content-Range"] == f"bytes 262144-524287/{SIZE}"
    assert not any(e[0] == "start" for e in store.events)


def test_completed_session_is_recognised_without_reupload(video: Path) -> None:
    latest = {"id": 1, "state": "in_progress", "session_uri": SESSION, "final_chunk_sent": 1}
    query = Recorder([({"status": "201"}, DONE_BODY)])
    store = MemoryStore(latest)
    assert uploader(None, query).upload(1, video, "t", "d", store) == UploadDone("VID123", "private")
    assert ("finish", "VID123", "private") in store.events


def test_expired_session_after_final_bytes_is_unknown(video: Path) -> None:
    latest = {"id": 1, "state": "in_progress", "session_uri": SESSION, "final_chunk_sent": 1}
    outcome = uploader(None, Recorder([({"status": "404"}, b"")])).upload(1, video, "t", "d", MemoryStore(latest))
    assert isinstance(outcome, UploadUnknown)


def test_expired_session_before_final_bytes_starts_fresh(video: Path) -> None:
    latest = {"id": 1, "state": "in_progress", "session_uri": SESSION, "final_chunk_sent": 0}
    service = Recorder([({"status": "200", "location": SESSION + "2"}, b""),
                        ({"status": "308", "range": "bytes=0-262143"}, b""),
                        ({"status": "308", "range": "bytes=0-524287"}, b""),
                        ({"status": "200"}, DONE_BODY)])
    store = MemoryStore(latest)
    outcome = uploader(service, Recorder([({"status": "404"}, b"")])).upload(1, video, "t", "d", store)
    assert outcome == UploadDone("VID123", "private")
    assert ("mark", "abandoned") in store.events and ("start", SIZE) in store.events


def test_no_saved_session_for_single_chunk_file_is_unknown(tmp_path: Path) -> None:
    small = tmp_path / "s.mp4"
    small.write_bytes(b"\x00" * 1000)
    latest = {"id": 1, "state": "in_progress", "session_uri": None, "final_chunk_sent": 0}
    assert isinstance(uploader(None).upload(1, small, "t", "d", MemoryStore(latest)), UploadUnknown)


def test_status_check_failure_retries_later(video: Path) -> None:
    latest = {"id": 1, "state": "in_progress", "session_uri": SESSION, "final_chunk_sent": 1}
    outcome = uploader(None, None).upload(1, video, "t", "d", MemoryStore(latest))  # http factory returns None
    assert isinstance(outcome, UploadRetry) and outcome.category == "network"


def test_already_done_returns_stored_result(video: Path) -> None:
    latest = {"id": 1, "state": "done", "video_id": "V9", "privacy_status": "private"}
    assert uploader(None).upload(1, video, "t", "d", MemoryStore(latest)) == UploadDone("V9", "private")


@pytest.mark.parametrize("status, content, kind, category", [
    ("403", b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}', UploadRetry, "quota"),
    ("401", b'{"error": {"message": "Invalid Credentials"}}', UploadFailed, "auth"),
    ("400", b'{"error": {"message": "bad"}}', UploadFailed, "rejected"),
    ("503", b"unavailable", UploadRetry, "network"),
])
def test_http_errors_map_to_clear_outcomes(video: Path, status, content, kind, category) -> None:
    responses = [({"status": status}, content)] * 4  # retries of 5xx consume more responses
    outcome = uploader(Recorder(responses)).upload(1, video, "t", "d", MemoryStore())
    assert isinstance(outcome, kind) and outcome.category == category
    if category == "quota":
        assert outcome.delay_s == 6 * 3600


def test_credentials_round_trip_through_keyring() -> None:
    from captioner import secrets

    secrets.set_secret(secrets.YOUTUBE_TOKEN, json.dumps({"refresh_token": "r", "client_id": "c",
                                                          "client_secret": "s", "token_uri": youtube.TOKEN_URI}))
    creds = youtube.load_credentials()
    assert creds.refresh_token == "r" and creds.scopes == youtube.SCOPES


def test_missing_credentials_message() -> None:
    with pytest.raises(youtube.YouTubeAuthError, match="captioner setup --step youtube"):
        youtube.load_credentials()
