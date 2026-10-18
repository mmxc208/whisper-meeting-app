"""Background allocation and execution for meeting agents."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from backend.agent_ledger import AGENT_IDS, AgentLedger
from backend.gateway_agents import (
    GatewayAgentClient,
    GatewayRefused,
    GatewayTaskNotFound,
    GatewayUnavailable,
)

TERMINAL_FAILURES = {"failed", "cancelled", "stopped", "orphaned"}
logger = logging.getLogger("whisper-meeting.agents")


class AgentClient(Protocol):
    def authenticate(self) -> None: ...

    def spawn(self, *, agent: str, prompt: str) -> str: ...

    def task(self, task_id: str) -> dict[str, Any]: ...

    def find_task(self, dispatch_id: str) -> str: ...


class AgentCoordinator:
    """Keep agent work downstream, durable, and independent of recording."""

    def __init__(
        self,
        ledger: AgentLedger,
        *,
        app_name: str,
        crew_home: Path,
        client_factory: Callable[[], AgentClient] | None = None,
        batch_interval: float = 30.0,
        poll_interval: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ledger = ledger
        self.app_name = app_name
        self.crew_home = crew_home
        self.batch_interval = batch_interval
        self.poll_interval = poll_interval
        self._monotonic = monotonic
        self._client_factory = client_factory or (
            lambda: GatewayAgentClient.from_environment(
                app_name=self.app_name, crew_home=self.crew_home
            )
        )
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_batch: dict[str, float] = {}

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(
                target=self._allocator_loop, name="meeting-agent-allocator", daemon=True
            ),
            *(
                threading.Thread(
                    target=self._worker_loop,
                    args=(agent_id,),
                    name=f"meeting-agent-{agent_id}",
                    daemon=True,
                )
                for agent_id in AGENT_IDS
            ),
        ]
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads.clear()

    def notify(self) -> None:
        self._wake.set()

    def allocate_once(self) -> bool:
        """Allocate newly visible work. Safe to call repeatedly."""

        changed = False
        now = self._monotonic()
        for meeting in self.ledger.meetings_for_allocation():
            meeting_id = str(meeting["id"])
            owner_id = str(meeting["owner_id"])
            self.ledger.initialize_outputs(meeting_id=meeting_id, owner_id=owner_id)
            state = str(meeting["state"])
            last = self._last_batch.get(meeting_id, 0.0)
            due = state != "recording" or now - last >= self.batch_interval
            if due:
                queued = self.ledger.queue_speech(meeting_id=meeting_id, owner_id=owner_id)
                if queued:
                    changed = True
                self._last_batch[meeting_id] = now
            if state == "ended":
                # queue_speech is bounded. Finalize only after every visible speech
                # segment has been allocated, so it cannot overtake a later batch.
                remaining = self.ledger.queue_speech(meeting_id=meeting_id, owner_id=owner_id)
                if remaining:
                    changed = True
                    continue
                self.ledger.queue_finalize(meeting_id=meeting_id, owner_id=owner_id)
                changed = True
        return changed

    def work_once(self, *, agent_id: str, client: AgentClient) -> bool:
        """Advance at most one dispatch for one agent."""

        dispatch = self.ledger.inflight(agent_id=agent_id)
        newly_claimed = dispatch is None
        if dispatch is None:
            dispatch = self.ledger.claim_next(agent_id=agent_id)
            if dispatch is None:
                return False

        task_id = str(dispatch.get("execution_ref", ""))
        if not task_id:
            task_id = client.find_task(str(dispatch["id"]))
            if task_id:
                self.ledger.mark_accepted(dispatch_id=str(dispatch["id"]), execution_ref=task_id)
            elif not newly_claimed:
                # The prior POST has no receipt. An authoritative app-owned task
                # listing found no matching marker, so it is safe to retry later.
                self.ledger.reconcile_missing_task(dispatch_id=str(dispatch["id"]))
                return True
            else:
                prompt = self._prompt(dispatch)
                try:
                    task_id = client.spawn(agent=agent_id, prompt=prompt)
                except GatewayRefused as exc:
                    self.ledger.fail(dispatch_id=str(dispatch["id"]), error=str(exc))
                    return True
                self.ledger.mark_accepted(dispatch_id=str(dispatch["id"]), execution_ref=task_id)
        try:
            status = client.task(task_id)
        except GatewayTaskNotFound:
            # Task status is gateway-generation scoped. A restart can remove the
            # accepted task while leaving our durable receipt intact. Requeue the
            # same dispatch id; its prompt tells the agent to read the current
            # output first, so a task that wrote before shutdown stays idempotent.
            self.ledger.reconcile_missing_task(dispatch_id=str(dispatch["id"]))
            return True
        if status.get("done") is not True:
            return True
        outcome = str(status.get("outcome", ""))
        error = str(status.get("error", ""))
        if outcome in TERMINAL_FAILURES or error:
            self.ledger.fail(
                dispatch_id=str(dispatch["id"]),
                error=error or f"agent task ended as {outcome or 'failed'}",
            )
        else:
            self.ledger.complete(dispatch_id=str(dispatch["id"]))
        return True

    def _allocator_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.allocate_once()
            except Exception:
                # The workers and capture path remain alive. The next bounded
                # pass retries from the durable transcript and dispatch ledger.
                pass
            self._wake.wait(timeout=self.poll_interval)
            self._wake.clear()

    def _worker_loop(self, agent_id: str) -> None:
        client: AgentClient | None = None
        while not self._stop.is_set():
            try:
                if client is None:
                    client = self._client_factory()
                    client.authenticate()
                did_work = self.work_once(agent_id=agent_id, client=client)
                if not did_work:
                    self._wake.wait(timeout=self.poll_interval)
                    self._wake.clear()
                    continue
                self._stop.wait(self.poll_interval)
            except GatewayUnavailable as exc:
                self.ledger.set_unavailable(agent_id=agent_id, error=str(exc))
                client = None
                self._stop.wait(self.poll_interval)
            except Exception as exc:
                logger.exception("meeting agent worker failed for %s", agent_id)
                self.ledger.set_unavailable(agent_id=agent_id, error=str(exc))
                self._stop.wait(self.poll_interval)

    def _prompt(self, dispatch: dict[str, Any]) -> str:
        output = self.ledger.output_path(
            meeting_id=str(dispatch["meeting_id"]), agent_id=str(dispatch["agent_id"])
        )
        return (
            f"[WM_DISPATCH:{dispatch['id']}]\n"
            f"OUTPUT_FILE: {output}\n\n"
            "Write your output to the exact path in OUTPUT_FILE. Read the current output "
            "before updating it. The dispatch marker is an idempotency key; if this input "
            "was already applied, leave the output coherent rather than duplicating content.\n\n"
            f"{dispatch['payload_text']}"
        )
