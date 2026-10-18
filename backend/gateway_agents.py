"""Authenticated loopback client for app-owned meeting agents."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class GatewayUnavailable(RuntimeError):
    """The trusted gateway callback channel is absent or unavailable."""


class GatewayAuthenticationExpired(GatewayUnavailable):
    """The app-scoped gateway session must be authenticated again."""


class GatewayRefused(RuntimeError):
    """The gateway rejected an app-scoped agent operation."""


class GatewayTaskNotFound(GatewayRefused):
    """A previously accepted background task is absent after reconciliation."""


def trusted_gateway_origin(*, app_name: str, crew_home: Path) -> tuple[str, str]:
    """Return a proof-verified loopback origin and app secret."""

    origin = os.environ.get("KIROCREW_GATEWAY_ORIGIN", "").strip()
    proof = os.environ.get("KIROCREW_GATEWAY_ORIGIN_PROOF", "").strip()
    parsed = urllib.parse.urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or not parsed.port
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise GatewayUnavailable("KiroCrew did not publish a trusted loopback gateway origin")
    secret_path = crew_home / "apps" / app_name / ".app_secret"
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GatewayUnavailable("the app callback secret is unavailable") from exc
    expected = hmac.new(secret.encode("utf-8"), origin.encode("utf-8"), hashlib.sha256).hexdigest()
    if not secret or not proof or not hmac.compare_digest(proof, expected):
        raise GatewayUnavailable("the gateway origin proof is missing or invalid")
    return origin.rstrip("/"), secret


class GatewayAgentClient:
    """Use the app's scoped token to start and reconcile background agents."""

    def __init__(self, *, origin: str, app_name: str, secret: str) -> None:
        self.origin = origin
        self.app_name = app_name
        self._secret = secret
        self._token = ""
        self._opener = urllib.request.build_opener()

    @classmethod
    def from_environment(cls, *, app_name: str, crew_home: Path) -> "GatewayAgentClient":
        origin, secret = trusted_gateway_origin(app_name=app_name, crew_home=crew_home)
        return cls(origin=origin, app_name=app_name, secret=secret)

    def authenticate(self) -> None:
        response = self._request(
            "POST",
            f"/api/apps/{urllib.parse.quote(self.app_name, safe='')}/token",
            {},
            headers={"X-App-Secret": self._secret},
        )
        token = str(response.get("token", ""))
        if not token:
            raise GatewayUnavailable("the gateway did not issue an app token")
        self._token = token
        self._secret = ""

    def spawn(self, *, agent: str, prompt: str) -> str:
        result = self._request(
            "POST",
            "/api/spawn",
            {
                "task": prompt,
                "agent": agent,
                "approval_mode": "auto",
                "silent": True,
            },
            timeout=45,
        )
        task_id = str(result.get("id", ""))
        if not task_id:
            raise GatewayUnavailable("the gateway accepted no task identifier")
        return task_id

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/spawn/{urllib.parse.quote(task_id, safe='')}")

    def tasks(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/api/spawn?queued=1")
        rows = result.get("agents", result.get("value", []))
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def find_task(self, dispatch_id: str) -> str:
        marker = f"[WM_DISPATCH:{dispatch_id}]"
        for task in self.tasks():
            if marker in str(task.get("task", "")):
                return str(task.get("id", ""))
        return ""

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = {"Accept": "application/json", **(headers or {})}
        if self._token and "Cookie" not in request_headers:
            port = urllib.parse.urlsplit(self.origin).port
            request_headers["Cookie"] = f"mc_token_{port}={self._token}"
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.origin}{path}",
            data=payload,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1_000]
            if exc.code == 404 and method == "GET" and path.startswith("/api/spawn/"):
                raise GatewayTaskNotFound(
                    f"gateway no longer knows accepted task {path.rsplit('/', 1)[-1]}"
                ) from exc
            if exc.code in {401, 403}:
                raise GatewayAuthenticationExpired(
                    f"gateway returned HTTP {exc.code}: {detail}"
                ) from exc
            raise GatewayRefused(f"gateway returned HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise GatewayUnavailable(f"gateway request failed: {exc}") from exc
        if not raw:
            return {}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayUnavailable("gateway returned invalid JSON") from exc
        if isinstance(result, dict):
            return result
        return {"value": result}
