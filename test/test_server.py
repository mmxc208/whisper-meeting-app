import hashlib
import hmac
import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kiro_crew.apps.proxy_auth import PRINCIPAL_OWNER_SESSION, sign_proxy_principal_claim

from backend.agent_ledger import NOTE_AGENT, SKETCH_AGENT, AgentLedger
from backend.auth import ProxyAuthenticator
from backend.server import SSE_HEARTBEAT_SECONDS, WhisperMeetingServer
from backend.store import SCHEMA_VERSION, MeetingStore

SECRET = "phase-one-test-secret"
NOW = 1_797_801_600.0
MEETING_ID = "11111111-1111-4111-8111-111111111111"


class _Coordinator:
    def __init__(self) -> None:
        self.notifications = 0

    def notify(self) -> None:
        self.notifications += 1


def _signed_headers(
    method: str,
    target: str,
    body: bytes,
    *,
    request_id: str,
) -> dict[str, str]:
    timestamp = str(int(NOW))
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{timestamp}:{method}:{target}:{body_hash}"
    proxy_signature = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    principal = sign_proxy_principal_claim(
        kind=PRINCIPAL_OWNER_SESSION,
        owner_id="owner-a",
        method=method,
        target=target,
        body=body,
        secret=SECRET,
        now=NOW,
        request_id=request_id,
    )
    return {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json",
        "X-KiroCrew-Proxy": f"{timestamp}:{proxy_signature}",
        "X-KiroCrew-Principal": principal,
    }


@contextmanager
def _running_server(tmp_path: Path) -> Iterator[WhisperMeetingServer]:
    store = MeetingStore(
        tmp_path / "data",
        clock=lambda: "2026-10-17T12:00:00.000+00:00",
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
    )
    store.initialize()
    coordinator = _Coordinator()
    server = WhisperMeetingServer(
        ("127.0.0.1", 0),
        store=store,
        authenticator=ProxyAuthenticator(clock=lambda: NOW),
        agent_ledger=AgentLedger(
            store.data_dir,
            clock=lambda: "2026-10-17T12:00:00.000+00:00",
            id_factory=lambda: "context-1",
        ),
        agent_coordinator=coordinator,
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        assert not thread.is_alive()


def _request(
    server: WhisperMeetingServer,
    method: str,
    target: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2.0)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _raw_request(
    server: WhisperMeetingServer,
    method: str,
    target: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2.0)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_health_is_unsigned_and_reports_schema(tmp_path: Path) -> None:
    with _running_server(tmp_path) as server:
        status, payload = _request(server, "GET", "/health")

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["schemaVersion"] == SCHEMA_VERSION
    assert SSE_HEARTBEAT_SECONDS == 30.0
    assert SSE_HEARTBEAT_SECONDS < 60.0


def test_meeting_routes_require_proxy_authentication(tmp_path: Path) -> None:
    with _running_server(tmp_path) as server:
        status, payload = _request(server, "GET", "/api/meetings")

    assert status == 401
    assert payload["code"] == "proxy_auth_required"


def test_create_and_list_meeting_through_signed_routes(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    create_body = json.dumps({"title": "中英 weekly"}).encode()
    with _running_server(tmp_path) as server:
        create_status, create_payload = _request(
            server,
            "POST",
            "/api/meetings",
            body=create_body,
            headers=_signed_headers(
                "POST",
                "/api/meetings",
                create_body,
                request_id="0" * 32,
            ),
        )
        list_status, list_payload = _request(
            server,
            "GET",
            "/api/meetings",
            headers=_signed_headers(
                "GET",
                "/api/meetings",
                b"",
                request_id="1" * 32,
            ),
        )

    assert create_status == 201
    assert create_payload["meeting"]["id"] == MEETING_ID
    assert list_status == 200
    assert list_payload["meetings"] == [create_payload["meeting"]]


def test_owner_claim_cannot_be_replayed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    headers = _signed_headers(
        "GET",
        "/api/meetings",
        b"",
        request_id="2" * 32,
    )
    with _running_server(tmp_path) as server:
        first_status, _ = _request(server, "GET", "/api/meetings", headers=headers)
        replay_status, replay_payload = _request(server, "GET", "/api/meetings", headers=headers)

    assert first_status == 200
    assert replay_status == 401
    assert replay_payload["code"] == "proxy_auth_required"


def test_meeting_context_is_durable_and_visible_to_both_agents(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    create_body = json.dumps({"title": "Agent workspace"}).encode()
    with _running_server(tmp_path) as server:
        _request(
            server,
            "POST",
            "/api/meetings",
            body=create_body,
            headers=_signed_headers("POST", "/api/meetings", create_body, request_id="f" * 32),
        )
        start_target = f"/api/meetings/{MEETING_ID}/start"
        _request(
            server,
            "POST",
            start_target,
            headers=_signed_headers("POST", start_target, b"", request_id="0f" * 16),
        )
        context_target = f"/api/meetings/{MEETING_ID}/context"
        context_body = json.dumps({"text": "The agenda is launch readiness."}).encode()
        context_status, context_payload = _request(
            server,
            "POST",
            context_target,
            body=context_body,
            headers=_signed_headers("POST", context_target, context_body, request_id="1f" * 16),
        )
        agents_target = f"/api/meetings/{MEETING_ID}/agents"
        agents_status, agents_payload = _request(
            server,
            "GET",
            agents_target,
            headers=_signed_headers("GET", agents_target, b"", request_id="2f" * 16),
        )

    assert context_status == 201
    assert context_payload["context"]["text"] == "The agenda is launch readiness."
    assert agents_status == 200
    assert agents_payload["context"] == [context_payload["context"]]
    assert {row["agent_id"] for row in agents_payload["agents"]} == {
        NOTE_AGENT,
        SKETCH_AGENT,
    }
    assert agents_payload["outputs"][NOTE_AGENT].startswith("# Meeting notes")
    assert "Sketch pending" in agents_payload["outputs"][SKETCH_AGENT]
    assert agents_payload["dispatch_counts"] == []
    assert server.agent_coordinator is not None
    assert server.agent_coordinator.notifications == 1


def test_failed_agent_work_can_be_retried_through_signed_route(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    create_body = json.dumps({"title": "Agent retry"}).encode()
    with _running_server(tmp_path) as server:
        _request(
            server,
            "POST",
            "/api/meetings",
            body=create_body,
            headers=_signed_headers("POST", "/api/meetings", create_body, request_id="3f" * 16),
        )
        start_target = f"/api/meetings/{MEETING_ID}/start"
        _request(
            server,
            "POST",
            start_target,
            headers=_signed_headers("POST", start_target, b"", request_id="4f" * 16),
        )
        context_target = f"/api/meetings/{MEETING_ID}/context"
        context_body = json.dumps({"text": "Agenda: retry handling."}).encode()
        _request(
            server,
            "POST",
            context_target,
            body=context_body,
            headers=_signed_headers("POST", context_target, context_body, request_id="5f" * 16),
        )
        assert server.agent_ledger is not None
        queued = server.agent_ledger.queue_update(
            meeting_id=MEETING_ID,
            owner_id="owner-a",
            agent_id=NOTE_AGENT,
        )
        assert queued is not None
        dispatch = server.agent_ledger.claim_next(agent_id=NOTE_AGENT)
        assert dispatch is not None
        server.agent_ledger.fail(dispatch_id=dispatch["id"], error="provider unavailable")
        retry_target = f"/api/meetings/{MEETING_ID}/agents/{NOTE_AGENT}/retry"
        retry_status, retry_payload = _request(
            server,
            "POST",
            retry_target,
            headers=_signed_headers("POST", retry_target, b"", request_id="6f" * 16),
        )

    assert retry_status == 202
    assert retry_payload == {"retried": 1}
    assert server.agent_coordinator is not None
    assert server.agent_coordinator.notifications == 2


def test_start_and_upload_are_durable_and_idempotent(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    with _running_server(tmp_path) as server:
        create_body = b"{}"
        _request(
            server,
            "POST",
            "/api/meetings",
            body=create_body,
            headers=_signed_headers("POST", "/api/meetings", create_body, request_id="3" * 32),
        )
        start_target = f"/api/meetings/{MEETING_ID}/start"
        start_status, start_payload = _request(
            server,
            "POST",
            start_target,
            headers=_signed_headers("POST", start_target, b"", request_id="4" * 32),
        )
        audio = bytes([1, 0]) * 8000
        digest = hashlib.sha256(audio).hexdigest()
        audio_target = f"/api/meetings/{MEETING_ID}/audio/1/0"
        audio_headers = {
            **_signed_headers("PUT", audio_target, audio, request_id="5" * 32),
            "Content-Type": "application/octet-stream",
            "X-Chunk-SHA256": digest,
            "X-Sample-Start": "0",
            "X-Sample-Count": "8000",
            "X-Sample-Rate": "16000",
            "X-Channels": "1",
            "X-Encoding": "pcm_s16le",
        }
        first_status, first_payload = _request(
            server, "PUT", audio_target, body=audio, headers=audio_headers
        )
        duplicate_headers = {
            **audio_headers,
            **_signed_headers("PUT", audio_target, audio, request_id="6" * 32),
        }
        duplicate_status, duplicate_payload = _request(
            server, "PUT", audio_target, body=audio, headers=duplicate_headers
        )
        pause_target = f"/api/meetings/{MEETING_ID}/pause"
        pause_body = json.dumps({"epoch": 1, "cut_sequence": 0, "cut_sample": 8000}).encode()
        pause_status, pause_payload = _request(
            server,
            "POST",
            pause_target,
            body=pause_body,
            headers=_signed_headers("POST", pause_target, pause_body, request_id="7" * 32),
        )
        detail_target = f"/api/meetings/{MEETING_ID}"
        detail_status, detail_payload = _request(
            server,
            "GET",
            detail_target,
            headers=_signed_headers("GET", detail_target, b"", request_id="8" * 32),
        )

    assert start_status == 201
    assert start_payload["capture"]["epoch"] == 1
    assert first_status == 201
    assert first_payload["ack"]["durable"] is True
    assert duplicate_status == 200
    assert duplicate_payload["ack"]["duplicate"] is True
    assert pause_status == 202
    assert pause_payload["operation"]["state"] == "transcribing"
    assert detail_status == 200
    assert detail_payload["meeting"]["state"] == "draining"
    assert detail_payload["capture"] == {
        "epoch": 1,
        "active": False,
        "cut_sequence": 0,
        "cut_sample": 8000,
    }


def test_event_stream_replays_after_cursor(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    with _running_server(tmp_path) as server:
        create_body = b"{}"
        _request(
            server,
            "POST",
            "/api/meetings",
            body=create_body,
            headers=_signed_headers("POST", "/api/meetings", create_body, request_id="9" * 32),
        )
        first_cursor = server.store.events_after(
            meeting_id=MEETING_ID, owner_id="owner-a", cursor=0
        )[-1]["cursor"]
        start_target = f"/api/meetings/{MEETING_ID}/start"
        _request(
            server,
            "POST",
            start_target,
            headers=_signed_headers("POST", start_target, b"", request_id="a" * 32),
        )
        events_target = f"/api/meetings/{MEETING_ID}/events?cursor={first_cursor}"
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2.0)
        try:
            connection.request(
                "GET",
                events_target,
                headers=_signed_headers("GET", events_target, b"", request_id="b" * 32),
            )
            response = connection.getresponse()
            event_id = response.readline().decode().strip()
            data = response.readline().decode().removeprefix("data: ")
        finally:
            connection.close()

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert event_id.startswith("id: ")
    assert json.loads(data)["kind"] == "capture.started"


def test_export_and_confirmed_delete_routes(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    create_body = json.dumps({"title": "中英 review"}).encode()
    with _running_server(tmp_path) as server:
        _request(
            server,
            "POST",
            "/api/meetings",
            body=create_body,
            headers=_signed_headers("POST", "/api/meetings", create_body, request_id="c" * 32),
        )
        export_target = f"/api/meetings/{MEETING_ID}/export?format=markdown"
        export_status, export_headers, export_body = _raw_request(
            server,
            "GET",
            export_target,
            headers=_signed_headers("GET", export_target, b"", request_id="d" * 32),
        )
        sketch_target = f"/api/meetings/{MEETING_ID}/export?format=sketch"
        sketch_status, sketch_headers, sketch_body = _raw_request(
            server,
            "GET",
            sketch_target,
            headers=_signed_headers("GET", sketch_target, b"", request_id="df" * 16),
        )
        delete_target = f"/api/meetings/{MEETING_ID}"
        delete_status, delete_payload = _request(
            server,
            "DELETE",
            delete_target,
            headers=_signed_headers("DELETE", delete_target, b"", request_id="e" * 32),
        )

    assert export_status == 200
    assert export_headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert "attachment; filename=" in export_headers["Content-Disposition"]
    assert "# 中英 review" in export_body.decode()
    assert sketch_status == 200
    assert sketch_headers["Content-Type"] == "text/html; charset=utf-8"
    assert "Content-Security-Policy" in sketch_body.decode()
    assert delete_status == 200
    assert delete_payload == {"deleted": MEETING_ID}
