"""HTTP backend for the Whisper Meeting App Kit app."""

from __future__ import annotations

import json
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.auth import ProxyAuthenticator, RequestIdentity
from backend.chunks import ChunkMetadata
from backend.exporter import SUPPORTED_EXPORTS, export_transcript
from backend.model import ModelManager
from backend.recognition import RecognitionScheduler
from backend.store import (
    ChunkConflict,
    InvalidMeeting,
    MeetingNotFound,
    MeetingStore,
    StorageLimit,
    app_data_dir,
)

APP_NAME = os.environ.get("KIROCREW_APP_NAME", "whisper-meeting")
MAX_JSON_BODY = 64 * 1024
MAX_AUDIO_BODY = 1024 * 1024
SSE_HEARTBEAT_SECONDS = 30.0
_START_ROUTE = re.compile(r"^/api/meetings/([^/]+)/start$")
_DRAIN_ROUTE = re.compile(r"^/api/meetings/([^/]+)/(pause|end)$")
_RESUME_ROUTE = re.compile(r"^/api/meetings/([^/]+)/resume$")
_EVENTS_ROUTE = re.compile(r"^/api/meetings/([^/]+)/events$")
_EXPORT_ROUTE = re.compile(r"^/api/meetings/([^/]+)/export$")
_MEETING_ROUTE = re.compile(r"^/api/meetings/([^/]+)$")
_RETRY_SEGMENT_ROUTE = re.compile(r"^/api/meetings/([^/]+)/segments/([0-9]+)/retry$")
_AUDIO_ROUTE = re.compile(r"^/api/meetings/([^/]+)/audio/([0-9]+)/([0-9]+)$")


class WhisperMeetingServer(ThreadingHTTPServer):
    """Threaded loopback server carrying app services."""

    allow_reuse_address = not sys.platform.startswith("win")
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: MeetingStore,
        authenticator: ProxyAuthenticator,
        models: ModelManager | None = None,
        scheduler: RecognitionScheduler | None = None,
    ) -> None:
        self.store = store
        self.authenticator = authenticator
        self.models = models
        self.scheduler = scheduler
        super().__init__(server_address, WhisperMeetingHandler)


class WhisperMeetingHandler(BaseHTTPRequestHandler):
    server: WhisperMeetingServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "app": APP_NAME,
                    "schemaVersion": self.server.store.schema_version(),
                },
            )
            return

        body = b""
        identity = self._authorize(body=body, require_owner=True)
        if identity is None:
            return
        route = urlsplit(self.path).path
        if route == "/api/model":
            if self.server.models is None:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE, "model_unavailable", "model manager unavailable"
                )
                return
            self._json(HTTPStatus.OK, {"model": self.server.models.status()})
            return
        if route == "/api/meetings":
            self._json(
                HTTPStatus.OK,
                {"meetings": self.server.store.list_meetings(owner_id=identity.owner_id)},
            )
            return
        events_match = _EVENTS_ROUTE.fullmatch(route)
        if events_match is not None:
            meeting_id = unquote(events_match.group(1))
            self._event_stream(meeting_id=meeting_id, identity=identity)
            return
        export_match = _EXPORT_ROUTE.fullmatch(route)
        if export_match is not None:
            meeting_id = unquote(export_match.group(1))
            format_name = parse_qs(urlsplit(self.path).query).get("format", ["markdown"])[0]
            if format_name not in SUPPORTED_EXPORTS:
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_export_format",
                    "export format must be markdown, text or json",
                )
                return
            try:
                meeting = self.server.store.get_meeting(
                    meeting_id=meeting_id, owner_id=identity.owner_id
                )
                document = export_transcript(
                    meeting=meeting,
                    segments=self.server.store.transcript(
                        meeting_id=meeting_id, owner_id=identity.owner_id
                    ),
                    format_name=format_name,
                )
            except MeetingNotFound:
                self._error(HTTPStatus.NOT_FOUND, "meeting_not_found", "meeting not found")
                return
            self._download(document.body, document.content_type, document.filename)
            return
        prefix = "/api/meetings/"
        if route.startswith(prefix):
            suffix = route[len(prefix) :]
            transcript_suffix = "/transcript"
            wants_transcript = suffix.endswith(transcript_suffix)
            raw_meeting_id = suffix[: -len(transcript_suffix)] if wants_transcript else suffix
            meeting_id = unquote(raw_meeting_id)
            if not meeting_id or "/" in meeting_id:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found")
                return
            try:
                meeting = self.server.store.get_meeting(
                    meeting_id=meeting_id,
                    owner_id=identity.owner_id,
                )
            except MeetingNotFound:
                self._error(HTTPStatus.NOT_FOUND, "meeting_not_found", "meeting not found")
                return
            if wants_transcript:
                self._json(
                    HTTPStatus.OK,
                    {
                        "meeting": meeting,
                        "segments": self.server.store.transcript(
                            meeting_id=meeting_id, owner_id=identity.owner_id
                        ),
                        "filtered_segments": self.server.store.filtered_transcript(
                            meeting_id=meeting_id, owner_id=identity.owner_id
                        ),
                    },
                )
            else:
                self._json(
                    HTTPStatus.OK,
                    self.server.store.meeting_progress(
                        meeting_id=meeting_id, owner_id=identity.owner_id
                    ),
                )
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found")

    def do_POST(self) -> None:
        body = self._read_body(max_bytes=MAX_JSON_BODY)
        if body is None:
            return
        identity = self._authorize(body=body, require_owner=True)
        if identity is None:
            return
        route = urlsplit(self.path).path
        if route == "/api/model/install":
            if self.server.models is None:
                self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE, "model_unavailable", "model manager unavailable"
                )
                return
            self._json(HTTPStatus.ACCEPTED, {"model": self.server.models.start_install()})
            return
        retry_match = _RETRY_SEGMENT_ROUTE.fullmatch(route)
        if retry_match is not None:
            meeting_id = unquote(retry_match.group(1))
            try:
                retry = self.server.store.retry_coverage(
                    meeting_id=meeting_id,
                    segment_id=int(retry_match.group(2)),
                    owner_id=identity.owner_id,
                )
            except MeetingNotFound:
                self._error(HTTPStatus.NOT_FOUND, "meeting_not_found", "meeting not found")
                return
            except InvalidMeeting as exc:
                self._error(HTTPStatus.CONFLICT, "coverage_not_retryable", str(exc))
                return
            if self.server.scheduler is not None:
                self.server.scheduler.notify()
            self._json(HTTPStatus.ACCEPTED, {"retry": retry})
            return
        start_match = _START_ROUTE.fullmatch(route)
        if start_match is not None:
            if self.server.models is not None and not self.server.models.ready():
                self._error(
                    HTTPStatus.CONFLICT,
                    "model_not_ready",
                    "Install and verify the local Metal model before recording.",
                )
                return
            meeting_id = unquote(start_match.group(1))
            try:
                capture = self.server.store.start_capture(
                    meeting_id=meeting_id,
                    owner_id=identity.owner_id,
                )
            except MeetingNotFound:
                self._error(HTTPStatus.NOT_FOUND, "meeting_not_found", "meeting not found")
                return
            except InvalidMeeting as exc:
                self._error(HTTPStatus.CONFLICT, "capture_not_startable", str(exc))
                return
            self._json(HTTPStatus.CREATED, {"capture": capture})
            return
        resume_match = _RESUME_ROUTE.fullmatch(route)
        if resume_match is not None:
            if self.server.models is not None and not self.server.models.ready():
                self._error(
                    HTTPStatus.CONFLICT,
                    "model_not_ready",
                    "Install and verify the local Metal model before recording.",
                )
                return
            meeting_id = unquote(resume_match.group(1))
            try:
                capture = self.server.store.resume_capture(
                    meeting_id=meeting_id, owner_id=identity.owner_id
                )
            except MeetingNotFound:
                self._error(HTTPStatus.NOT_FOUND, "meeting_not_found", "meeting not found")
                return
            except InvalidMeeting as exc:
                self._error(HTTPStatus.CONFLICT, "capture_not_resumable", str(exc))
                return
            self._json(HTTPStatus.CREATED, {"capture": capture})
            return
        drain_match = _DRAIN_ROUTE.fullmatch(route)
        if drain_match is not None:
            meeting_id = unquote(drain_match.group(1))
            try:
                payload = json.loads(body or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be an object")
                operation = self.server.store.request_drain(
                    meeting_id=meeting_id,
                    owner_id=identity.owner_id,
                    kind=drain_match.group(2),
                    epoch=int(payload["epoch"]),
                    cut_sequence=int(payload["cut_sequence"]),
                    cut_sample=int(payload["cut_sample"]),
                )
            except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError):
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_drain",
                    "epoch, cut_sequence and cut_sample must be integers",
                )
                return
            except MeetingNotFound:
                self._error(HTTPStatus.NOT_FOUND, "meeting_not_found", "meeting not found")
                return
            except InvalidMeeting as exc:
                self._error(HTTPStatus.CONFLICT, "meeting_not_drainable", str(exc))
                return
            if self.server.scheduler is not None:
                self.server.scheduler.notify()
            status = HTTPStatus.OK if operation["state"] == "complete" else HTTPStatus.ACCEPTED
            self._json(status, {"operation": operation})
            return
        if route != "/api/meetings":
            self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found")
            return
        try:
            payload = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "body must be valid JSON")
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("title", ""), str):
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid_meeting",
                "title must be a string",
            )
            return
        try:
            meeting = self.server.store.create_meeting(
                title=payload.get("title", ""),
                owner_id=identity.owner_id,
            )
        except InvalidMeeting as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_meeting", str(exc))
            return
        self._json(HTTPStatus.CREATED, {"meeting": meeting})

    def do_PUT(self) -> None:
        body = self._read_body(max_bytes=MAX_AUDIO_BODY)
        if body is None:
            return
        identity = self._authorize(body=body, require_owner=True)
        if identity is None:
            return
        route = urlsplit(self.path).path
        match = _AUDIO_ROUTE.fullmatch(route)
        if match is None:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found")
            return
        try:
            metadata = ChunkMetadata(
                meeting_id=unquote(match.group(1)),
                epoch=int(match.group(2)),
                sequence=int(match.group(3)),
                sample_start=self._integer_header("X-Sample-Start"),
                sample_count=self._integer_header("X-Sample-Count"),
                sample_rate=self._integer_header("X-Sample-Rate"),
                channels=self._integer_header("X-Channels"),
                encoding=self.headers.get("X-Encoding", ""),
                sha256=self.headers.get("X-Chunk-SHA256", ""),
            )
            ack = self.server.store.store_chunk(
                metadata=metadata,
                body=body,
                owner_id=identity.owner_id,
            )
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_audio_chunk", str(exc))
            return
        except MeetingNotFound:
            self._error(HTTPStatus.NOT_FOUND, "meeting_not_found", "meeting not found")
            return
        except InvalidMeeting as exc:
            self._error(HTTPStatus.CONFLICT, "capture_stopped", str(exc))
            return
        except ChunkConflict as exc:
            self._error(HTTPStatus.CONFLICT, "chunk_conflict", str(exc))
            return
        except StorageLimit as exc:
            self._error(HTTPStatus.INSUFFICIENT_STORAGE, "storage_limit", str(exc))
            return
        if self.server.scheduler is not None:
            self.server.scheduler.notify()
        self._json(HTTPStatus.OK if ack["duplicate"] else HTTPStatus.CREATED, {"ack": ack})

    def do_DELETE(self) -> None:
        body = self._read_body(max_bytes=0)
        if body is None:
            return
        identity = self._authorize(body=body, require_owner=True)
        if identity is None:
            return
        match = _MEETING_ROUTE.fullmatch(urlsplit(self.path).path)
        if match is None:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "route not found")
            return
        meeting_id = unquote(match.group(1))
        try:
            result = self.server.store.delete_meeting(
                meeting_id=meeting_id, owner_id=identity.owner_id
            )
        except MeetingNotFound:
            self._error(HTTPStatus.NOT_FOUND, "meeting_not_found", "meeting not found")
            return
        except InvalidMeeting as exc:
            self._error(HTTPStatus.CONFLICT, "meeting_not_deletable", str(exc))
            return
        self._json(HTTPStatus.OK, result)

    def _event_stream(self, *, meeting_id: str, identity: RequestIdentity) -> None:
        split = urlsplit(self.path)
        raw_cursor = self.headers.get("Last-Event-ID")
        if raw_cursor is None:
            raw_cursor = parse_qs(split.query).get("cursor", ["0"])[0]
        try:
            cursor = int(raw_cursor)
            if cursor < 0:
                raise ValueError
            self.server.store.get_meeting(meeting_id=meeting_id, owner_id=identity.owner_id)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_cursor", "event cursor is invalid")
            return
        except MeetingNotFound:
            self._error(HTTPStatus.NOT_FOUND, "meeting_not_found", "meeting not found")
            return

        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                events = self.server.store.wait_for_events(
                    meeting_id=meeting_id,
                    owner_id=identity.owner_id,
                    cursor=cursor,
                    timeout=SSE_HEARTBEAT_SECONDS,
                )
                if not events:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    cursor = int(event["cursor"])
                    data = json.dumps(
                        {
                            "kind": event["kind"],
                            "payload": json.loads(event["payload_json"]),
                            "created_at": event["created_at"],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.wfile.write(f"id: {cursor}\n".encode("ascii"))
                    self.wfile.write(b"data: " + data + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _authorize(self, *, body: bytes, require_owner: bool) -> RequestIdentity | None:
        identity = self.server.authenticator.verify(
            method=self.command,
            target=self.path,
            body=body,
            headers=dict(self.headers.items()),
            require_owner=require_owner,
        )
        if identity is None:
            self._error(
                HTTPStatus.UNAUTHORIZED,
                "proxy_auth_required",
                "request authentication failed",
            )
        return identity

    def _integer_header(self, name: str) -> int:
        value = self.headers.get(name)
        if value is None:
            raise ValueError(f"{name} header is required")
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"{name} header must be an integer") from exc

    def _read_body(self, *, max_bytes: int) -> bytes | None:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_content_length", "invalid body length")
            return None
        if length < 0 or length > max_bytes:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "body is too large")
            return None
        return self.rfile.read(length)

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"code": code, "error": message})

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _download(self, body: bytes, content_type: str, filename: str) -> None:
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(os.environ["PORT"])
    store = MeetingStore(app_data_dir())
    store.initialize()
    store.reconcile_spool()
    models = ModelManager(store.data_dir, store)
    models.reconcile()
    scheduler = RecognitionScheduler(store, models)
    models.set_ready_callback(scheduler.notify)
    scheduler.start()
    server = WhisperMeetingServer(
        ("127.0.0.1", port),
        store=store,
        authenticator=ProxyAuthenticator(),
        models=models,
        scheduler=scheduler,
    )
    print(f"{APP_NAME} backend listening on 127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        scheduler.close()


if __name__ == "__main__":
    main()
