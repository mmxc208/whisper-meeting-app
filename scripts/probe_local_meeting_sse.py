#!/usr/bin/env python3
"""Observe 30-second heartbeats through Kiro Crew's real App Kit HTTP proxy."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Sequence

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from kiro_crew.apps import routes

HEARTBEAT_SECONDS = 30.0
HEARTBEAT_COUNT = 3


async def _backend() -> web.Application:
    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for index in range(HEARTBEAT_COUNT):
            if index:
                await asyncio.sleep(HEARTBEAT_SECONDS)
            await response.write(f": heartbeat {index}\n\n".encode())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/api/events", events)
    return app


async def run_probe() -> dict[str, object]:
    """Run a backend and the production proxy handler on loopback."""

    backend = TestServer(await _backend())
    await backend.start_server()
    base = f"http://127.0.0.1:{backend.port}"
    routes.is_app_enabled = lambda name: True
    routes._resolve_app_backend_url = lambda name: base
    routes._get_app_secret = lambda name: "phase0-probe-secret"

    gateway_app = web.Application()
    gateway_app.router.add_route("*", "/apps/{name}/api/{path:.*}", routes.handle_app_api_proxy)
    gateway = TestClient(TestServer(gateway_app))
    await gateway.start_server()
    received: list[dict[str, float | str]] = []
    started = asyncio.get_running_loop().time()
    try:
        response = await gateway.get("/apps/local-meeting/api/events")
        if response.status != 200:
            raise RuntimeError(f"proxy returned HTTP {response.status}: {await response.text()}")
        if not routes._is_event_stream(response.headers.get("Content-Type", "")):
            raise RuntimeError("proxy did not preserve the event-stream content type")
        async with asyncio.timeout(HEARTBEAT_SECONDS * (HEARTBEAT_COUNT + 1)):
            async for raw_line in response.content:
                line = raw_line.decode().strip()
                if line.startswith(": heartbeat"):
                    received.append(
                        {
                            "comment": line,
                            "elapsed_seconds": asyncio.get_running_loop().time() - started,
                        }
                    )
    finally:
        await gateway.close()
        await backend.close()

    if len(received) != HEARTBEAT_COUNT:
        raise RuntimeError(f"received {len(received)} heartbeats, expected {HEARTBEAT_COUNT}")
    elapsed = float(received[-1]["elapsed_seconds"])
    if elapsed <= routes._PROXY_IDLE_TIMEOUT:
        raise RuntimeError(
            f"stream lasted {elapsed:.2f}s, not beyond the {routes._PROXY_IDLE_TIMEOUT}s idle bound"
        )
    return {
        "content_type": "text/event-stream",
        "ordinary_proxy_timeout_seconds": routes._PROXY_TIMEOUT,
        "event_stream_idle_timeout_seconds": routes._PROXY_IDLE_TIMEOUT,
        "heartbeat_interval_seconds": HEARTBEAT_SECONDS,
        "heartbeat_count": HEARTBEAT_COUNT,
        "elapsed_seconds": elapsed,
        "heartbeats": received,
        "survived_idle_timeout_boundary": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = {
        "schema_version": 1,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "result": asyncio.run(run_probe()),
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
