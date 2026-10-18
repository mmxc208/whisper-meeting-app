import hashlib
import hmac
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from backend.gateway_agents import (
    GatewayAgentClient,
    GatewayAuthenticationExpired,
    GatewayTaskNotFound,
    GatewayUnavailable,
    trusted_gateway_origin,
)


def _secret(crew_home: Path, value: str = "probe-secret") -> str:
    path = crew_home / "apps" / "whisper-meeting" / ".app_secret"
    path.parent.mkdir(parents=True)
    path.write_text(value, encoding="utf-8")
    return value


def test_gateway_origin_requires_valid_app_keyed_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = "http://127.0.0.1:5476"
    secret = _secret(tmp_path)
    monkeypatch.setenv("KIROCREW_GATEWAY_ORIGIN", origin)
    monkeypatch.setenv(
        "KIROCREW_GATEWAY_ORIGIN_PROOF",
        hmac.new(secret.encode(), origin.encode(), hashlib.sha256).hexdigest(),
    )

    assert trusted_gateway_origin(app_name="whisper-meeting", crew_home=tmp_path) == (
        origin,
        secret,
    )


def test_gateway_origin_refuses_remote_or_unproved_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secret(tmp_path)
    monkeypatch.setenv("KIROCREW_GATEWAY_ORIGIN", "https://provider.example")
    monkeypatch.setenv("KIROCREW_GATEWAY_ORIGIN_PROOF", "invalid")

    with pytest.raises(GatewayUnavailable, match="loopback"):
        trusted_gateway_origin(app_name="whisper-meeting", crew_home=tmp_path)


class _TaskClient(GatewayAgentClient):
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self._tasks = tasks

    def tasks(self) -> list[dict[str, Any]]:
        return self._tasks


def test_indeterminate_spawn_reconciles_by_durable_dispatch_marker() -> None:
    client = _TaskClient(
        [
            {"id": "other", "task": "unrelated"},
            {"id": "task-2", "task": "[WM_DISPATCH:dispatch-2]\nOUTPUT_FILE: /tmp/note"},
        ]
    )

    assert client.find_task("dispatch-2") == "task-2"
    assert client.find_task("missing") == ""


class _MissingTaskOpener:
    def open(self, request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "not found",
            Message(),
            io.BytesIO(b'{"error":"not found"}'),
        )


class _ExpiredTokenOpener:
    def open(self, request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "forbidden",
            Message(),
            io.BytesIO(b'{"error":"Forbidden","code":"forbidden"}'),
        )


def test_forbidden_gateway_session_requests_fresh_authentication() -> None:
    client = GatewayAgentClient(
        origin="http://localhost:5476",
        app_name="whisper-meeting",
        secret="secret",
    )
    client._opener = _ExpiredTokenOpener()  # type: ignore[assignment]

    with pytest.raises(GatewayAuthenticationExpired, match="HTTP 403"):
        client.tasks()


def test_task_status_maps_generation_scoped_404_to_missing_task() -> None:
    client = GatewayAgentClient(
        origin="http://127.0.0.1:5476",
        app_name="whisper-meeting",
        secret="secret",
    )
    client._opener = _MissingTaskOpener()  # type: ignore[assignment]

    with pytest.raises(GatewayTaskNotFound, match="task-1"):
        client.task("task-1")


class _SpawnResponse:
    def __enter__(self) -> "_SpawnResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"id":"task-1"}'


class _JsonResponse(_SpawnResponse):
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body


class _LocalhostAuthOpener:
    def __init__(self) -> None:
        self.cookies: list[str | None] = []

    def open(self, request: Any, timeout: float) -> _JsonResponse:
        self.cookies.append(request.get_header("Cookie"))
        if request.full_url.endswith("/api/apps/whisper-meeting/token"):
            return _JsonResponse(b'{"token":"app-token"}')
        return _JsonResponse(b'{"agents":[]}')


def test_authentication_sends_app_token_to_a_localhost_gateway() -> None:
    client = GatewayAgentClient(
        origin="http://localhost:5476",
        app_name="whisper-meeting",
        secret="secret",
    )
    opener = _LocalhostAuthOpener()
    client._opener = opener  # type: ignore[assignment]

    client.authenticate()
    assert client.tasks() == []

    assert opener.cookies == [None, "mc_token_5476=app-token"]


class _CaptureSpawnOpener:
    def __init__(self) -> None:
        self.body: dict[str, Any] = {}

    def open(self, request: Any, timeout: float) -> _SpawnResponse:
        self.body = json.loads(request.data)
        return _SpawnResponse()


def test_spawn_uses_the_qualified_unattended_approval_mode() -> None:
    client = GatewayAgentClient(
        origin="http://127.0.0.1:5476",
        app_name="whisper-meeting",
        secret="secret",
    )
    opener = _CaptureSpawnOpener()
    client._opener = opener  # type: ignore[assignment]

    assert client.spawn(agent="note", prompt="meeting text") == "task-1"
    assert opener.body == {
        "task": "meeting text",
        "agent": "note",
        "approval_mode": "auto",
        "silent": True,
    }
