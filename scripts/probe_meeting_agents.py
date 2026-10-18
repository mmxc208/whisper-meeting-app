#!/usr/bin/env python3
"""Probe KiroCrew execution modes for Whisper Meeting agents.

This script performs live model calls. It refuses non-loopback gateways and
requires ``--execute`` so importing it or running it accidentally is harmless.
Secrets and tokens are never included in its evidence output.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

APP_NAME = "whisper-meeting"
NOTE_AGENT = "whisper-meeting-note-taker"
SKETCH_AGENT = "whisper-meeting-sketch-artist"
TERMINAL_OUTCOMES = {"completed", "failed", "cancelled", "stopped", "orphaned"}


class ProbeError(RuntimeError):
    """A live platform probe could not establish its contract."""


class GatewayClient:
    """Small app-token client used only by the Phase A live probe."""

    def __init__(self, *, origin: str, app_name: str, secret: str) -> None:
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ProbeError("the probe requires an explicit loopback HTTP gateway origin")
        if not parsed.port:
            raise ProbeError("the gateway origin must include its bound port")
        self.origin = origin.rstrip("/")
        self.app_name = app_name
        self.secret = secret
        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )

    def authenticate(self) -> None:
        response = self.request(
            "POST",
            f"/api/apps/{urllib.parse.quote(self.app_name, safe='')}/token",
            {},
            extra_headers={"X-App-Secret": self.secret},
        )
        token = str(response.get("token", ""))
        if not token:
            raise ProbeError("the gateway did not return an app token")
        parsed = urllib.parse.urlsplit(self.origin)
        cookie = http.cookiejar.Cookie(
            version=0,
            name=f"mc_token_{parsed.port}",
            value=token,
            port=str(parsed.port),
            port_specified=True,
            domain=parsed.hostname or "localhost",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        self._cookie_jar.set_cookie(cookie)
        self.secret = ""

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", **(extra_headers or {})}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.origin}{path}", data=payload, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=45) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProbeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise ProbeError(f"{method} {path} failed: {exc}") from exc
        if not raw:
            return {}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            return {"body_bytes": len(raw)}
        if not isinstance(result, dict):
            return {"value": result}
        return result

    def stream_chat(self, *, slot: str, message: str) -> dict[str, Any]:
        payload = json.dumps({"slot": slot, "message": message}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.origin}/api/chat",
            data=payload,
            headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=600) as response:
                content_type = response.headers.get_content_type()
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProbeError(f"POST /api/chat returned HTTP {exc.code}: {detail}") from exc
        if content_type == "application/json":
            result = json.loads(raw or b"{}")
            return result if isinstance(result, dict) else {"value": result}
        if b"data: [DONE]" not in raw:
            raise ProbeError("the slot turn ended without an SSE completion marker")
        return {"completed": True, "body_bytes": len(raw)}


def _prompt(*, output: Path, kind: str) -> str:
    content = (
        "[finalized speech]\nAlice owns the launch checklist. "
        "团队将在周五复核，李明负责记录风险。"
        if kind == "note"
        else (
            "[finalized speech]\nThe capture pipeline writes PCM to durable storage, then local "
            "Whisper produces transcript segments, then the Note Taker consumes the text. "
            "音频始终留在本机，只有转写文本交给会议助手。"
        )
    )
    return (
        f"OUTPUT_FILE: {output}\n\n"
        "Write your output to exactly OUTPUT_FILE. Read the existing file first if it exists. "
        "This is a Phase A capability probe.\n\n"
        f"{content}"
    )


def _wait_for_task(client: GatewayClient, task_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.request("GET", f"/api/spawn/{urllib.parse.quote(task_id, safe='')}")
        outcome = str(status.get("outcome", ""))
        if status.get("done") is True or outcome in TERMINAL_OUTCOMES:
            return status
        time.sleep(0.5)
    raise ProbeError(f"task {task_id} did not finish within {timeout:.0f}s")


def _probe_spawn(client: GatewayClient, *, output_dir: Path, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    requests = [
        (NOTE_AGENT, _prompt(output=output_dir / "spawn-note.md", kind="note")),
        (SKETCH_AGENT, _prompt(output=output_dir / "spawn-sketch.html", kind="sketch")),
    ]
    task_ids: list[str] = []
    for agent, prompt in requests:
        accepted = client.request(
            "POST",
            "/api/spawn",
            {"task": prompt, "agent": agent, "approval_mode": "auto", "silent": True},
        )
        task_id = str(accepted.get("id", ""))
        if not task_id:
            raise ProbeError(f"spawn for {agent} returned no task id: {accepted}")
        task_ids.append(task_id)
    statuses = [_wait_for_task(client, task_id, timeout) for task_id in task_ids]
    files = [output_dir / "spawn-note.md", output_dir / "spawn-sketch.html"]
    return {
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "tasks": [
            {
                "id": task_id,
                "done": status.get("done"),
                "outcome": status.get("outcome", ""),
                "error": status.get("error", ""),
                "credits": status.get("credits"),
                "made_no_tool_calls": status.get("made_no_tool_calls"),
            }
            for task_id, status in zip(task_ids, statuses, strict=True)
        ],
        "outputs": [
            {
                "path": str(path),
                "exists": path.is_file(),
                "bytes": path.stat().st_size if path.is_file() else 0,
            }
            for path in files
        ],
    }


def _probe_slot(client: GatewayClient, *, output_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    slot = f"wm-probe-{uuid.uuid4().hex[:12]}"
    client.request("POST", "/api/chat/slots", {"name": slot, "agent": NOTE_AGENT})
    try:
        receipt = client.stream_chat(
            slot=slot,
            message=_prompt(output=output_dir / "slot-note.md", kind="note"),
        )
        history = client.request(
            "GET", f"/api/chat/slots/{urllib.parse.quote(slot, safe='')}?limit=20"
        )
    finally:
        client.request("DELETE", f"/api/chat/slots/{urllib.parse.quote(slot, safe='')}")
    output = output_dir / "slot-note.md"
    return {
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "receipt": receipt,
        "history_rows": len(history.get("history", [])),
        "output": {
            "path": str(output),
            "exists": output.is_file(),
            "bytes": output.stat().st_size if output.is_file() else 0,
        },
    }


def run_probe(
    *, origin: str, crew_home: Path, mode: str, timeout: float, execute: bool
) -> dict[str, Any]:
    if not execute:
        raise ProbeError("live dispatch is disabled; pass --execute after reviewing the target")
    secret_path = crew_home / "apps" / APP_NAME / ".app_secret"
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProbeError(f"cannot read the installed app secret at {secret_path}") from exc
    if not secret:
        raise ProbeError("the installed app secret is empty")

    probe_id = f"probe-{uuid.uuid4()}"
    output_dir = crew_home / "apps" / APP_NAME / "data" / "sessions" / probe_id / "agents"
    output_dir.mkdir(parents=True, exist_ok=False)
    client = GatewayClient(origin=origin, app_name=APP_NAME, secret=secret)
    client.authenticate()
    evidence: dict[str, Any] = {
        "schema": 1,
        "app": APP_NAME,
        "probe_id": probe_id,
        "gateway": "loopback",
        "mode": mode,
        "output_dir": str(output_dir),
    }
    if mode in {"spawn", "all"}:
        evidence["stateless_spawn"] = _probe_spawn(client, output_dir=output_dir, timeout=timeout)
    if mode in {"slot", "all"}:
        evidence["durable_slot"] = _probe_slot(client, output_dir=output_dir)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway-origin", default=os.environ.get("KIROCREW_GATEWAY_ORIGIN", ""))
    parser.add_argument(
        "--crew-home",
        type=Path,
        default=Path(os.environ.get("KIROCREW_HOME", Path.home() / ".kiro" / "crew")),
    )
    parser.add_argument("--mode", choices=("spawn", "slot", "all"), default="all")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.gateway_origin:
        raise SystemExit("--gateway-origin is required; do not guess the running gateway port")
    try:
        evidence = run_probe(
            origin=args.gateway_origin,
            crew_home=args.crew_home,
            mode=args.mode,
            timeout=args.timeout,
            execute=args.execute,
        )
    except ProbeError as exc:
        raise SystemExit(str(exc)) from exc
    rendered = json.dumps(evidence, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
