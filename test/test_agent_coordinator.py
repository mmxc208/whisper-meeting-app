from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.agent_coordinator import AgentCoordinator
from backend.agent_ledger import NOTE_AGENT, SKETCH_AGENT, AgentLedger
from backend.gateway_agents import GatewayAuthenticationExpired, GatewayTaskNotFound
from backend.store import MeetingStore

MEETING_ID = "00000000-0000-0000-0000-000000000456"
NOW = "2026-10-17T21:00:00.000+00:00"
NOW_EPOCH = datetime.fromisoformat(NOW).timestamp()


class _Client:
    def __init__(self) -> None:
        self.spawned: list[tuple[str, str]] = []
        self.found = ""
        self.status: dict[str, Any] = {"done": True, "outcome": "completed", "error": ""}

    def authenticate(self) -> None:
        return

    def spawn(self, *, agent: str, prompt: str) -> str:
        self.spawned.append((agent, prompt))
        return "task-1"

    def task(self, task_id: str) -> dict[str, Any]:
        assert task_id == "task-1"
        return self.status

    def find_task(self, dispatch_id: str) -> str:
        return self.found


class _LostAcceptedClient(_Client):
    def task(self, task_id: str) -> dict[str, Any]:
        raise GatewayTaskNotFound(f"missing {task_id}")


class _ExpiredClient(_Client):
    def find_task(self, dispatch_id: str) -> str:
        raise GatewayAuthenticationExpired("gateway returned HTTP 403")


def _coordinator(tmp_path: Path) -> tuple[MeetingStore, AgentLedger, AgentCoordinator]:
    data_dir = tmp_path / "data"
    store = MeetingStore(
        data_dir,
        clock=lambda: NOW,
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
    )
    store.initialize()
    store.create_meeting(title="Coordinator", owner_id="owner")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner")
    ledger = AgentLedger(data_dir, clock=lambda: NOW)
    ledger.initialize_outputs(meeting_id=MEETING_ID, owner_id="owner")
    coordinator = AgentCoordinator(
        ledger,
        app_name="whisper-meeting",
        crew_home=tmp_path,
        initial_update_seconds={NOTE_AGENT: 30, SKETCH_AGENT: 30},
        repeat_update_seconds={NOTE_AGENT: 300, SKETCH_AGENT: 600},
        wall_clock=lambda: NOW_EPOCH + 31,
    )
    return store, ledger, coordinator


def test_allocator_does_not_backfill_a_meeting_without_agent_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = MeetingStore(
        data_dir,
        clock=lambda: NOW,
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
    )
    store.initialize()
    store.create_meeting(title="Legacy", owner_id="owner")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner")
    _speech(store)
    ledger = AgentLedger(data_dir, clock=lambda: NOW)
    coordinator = AgentCoordinator(
        ledger,
        app_name="whisper-meeting",
        crew_home=tmp_path,
        wall_clock=lambda: NOW_EPOCH + 31,
    )

    assert coordinator.allocate_once() is False
    assert ledger.meetings_for_allocation() == []
    assert not (data_dir / "sessions" / MEETING_ID / "agents").exists()


def _speech(store: MeetingStore) -> None:
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO transcript_segments (
                meeting_id, epoch, audio_start_sample, audio_end_sample,
                language, text, coverage_kind, committed_at
            ) VALUES (?, 1, 0, 16000, 'en', 'Ship Friday.', 'speech', ?)
            """,
            (MEETING_ID, NOW),
        )
        connection.commit()


def test_coordinator_allocates_and_completes_one_agent_dispatch(tmp_path: Path) -> None:
    store, ledger, coordinator = _coordinator(tmp_path)
    _speech(store)
    client = _Client()

    assert coordinator.allocate_once() is True
    assert coordinator.work_once(agent_id=NOTE_AGENT, client=client) is True

    assert len(client.spawned) == 1
    prompt = client.spawned[0][1]
    assert "[WM_DISPATCH:" in prompt
    assert "OUTPUT_FILE:" in prompt
    input_path = Path(
        next(line for line in prompt.splitlines() if line.startswith("INPUT_FILE: "))[12:]
    )
    assert "Ship Friday." in input_path.read_text(encoding="utf-8")
    assert "Ship Friday." not in prompt
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    note = next(row for row in snapshot["agents"] if row["agent_id"] == NOTE_AGENT)
    assert note["state"] == "caught_up"
    assert note["delivered_through_segment_id"] == 1


def test_long_batch_uses_a_bounded_prompt_and_lossless_input_file(tmp_path: Path) -> None:
    store, ledger, coordinator = _coordinator(tmp_path)
    transcript = "architecture relationship workflow " * 300
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO transcript_segments (
                meeting_id, epoch, audio_start_sample, audio_end_sample,
                language, text, coverage_kind, committed_at
            ) VALUES (?, 1, 0, 16000, 'en', ?, 'speech', ?)
            """,
            (MEETING_ID, transcript, NOW),
        )
        connection.commit()
    client = _Client()

    assert coordinator.allocate_once() is True
    assert coordinator.work_once(agent_id=SKETCH_AGENT, client=client) is True

    assert len(client.spawned) == 1
    prompt = client.spawned[0][1]
    assert len(prompt) < 5_000
    assert transcript not in prompt
    input_path = Path(
        next(line for line in prompt.splitlines() if line.startswith("INPUT_FILE: "))[12:]
    )
    materialized = input_path.read_text(encoding="utf-8")
    assert len(materialized) > 5_000
    assert transcript.strip() in materialized


def test_indeterminate_preaccept_dispatch_reconciles_before_retry(tmp_path: Path) -> None:
    store, ledger, coordinator = _coordinator(tmp_path)
    _speech(store)
    coordinator.allocate_once()
    dispatch = ledger.claim_next(agent_id=NOTE_AGENT)
    assert dispatch is not None
    client = _Client()

    assert coordinator.work_once(agent_id=NOTE_AGENT, client=client) is True

    assert client.spawned == []
    retried = ledger.claim_next(agent_id=NOTE_AGENT)
    assert retried is not None
    assert retried["id"] == dispatch["id"]
    assert retried["attempt_count"] == 2


def test_indeterminate_dispatch_adopts_matching_gateway_task(tmp_path: Path) -> None:
    store, ledger, coordinator = _coordinator(tmp_path)
    _speech(store)
    coordinator.allocate_once()
    dispatch = ledger.claim_next(agent_id=NOTE_AGENT)
    assert dispatch is not None
    client = _Client()
    client.found = "task-1"

    assert coordinator.work_once(agent_id=NOTE_AGENT, client=client) is True

    assert client.spawned == []
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    note = next(row for row in snapshot["agents"] if row["agent_id"] == NOTE_AGENT)
    assert note["state"] == "caught_up"


def test_coordinator_restart_resumes_an_accepted_task_without_respawn(tmp_path: Path) -> None:
    store, ledger, coordinator = _coordinator(tmp_path)
    _speech(store)
    coordinator.allocate_once()
    dispatch = ledger.claim_next(agent_id=NOTE_AGENT)
    assert dispatch is not None
    ledger.mark_accepted(dispatch_id=dispatch["id"], execution_ref="task-1")
    restarted = AgentCoordinator(
        ledger,
        app_name="whisper-meeting",
        crew_home=tmp_path,
        wall_clock=lambda: NOW_EPOCH + 31,
    )
    client = _Client()

    assert restarted.work_once(agent_id=NOTE_AGENT, client=client) is True

    assert client.spawned == []
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    note = next(row for row in snapshot["agents"] if row["agent_id"] == NOTE_AGENT)
    assert note["state"] == "caught_up"


def test_coordinator_requeues_an_accepted_task_lost_with_gateway_generation(
    tmp_path: Path,
) -> None:
    store, ledger, coordinator = _coordinator(tmp_path)
    _speech(store)
    coordinator.allocate_once()
    dispatch = ledger.claim_next(agent_id=NOTE_AGENT)
    assert dispatch is not None
    ledger.mark_accepted(dispatch_id=dispatch["id"], execution_ref="old-task")

    assert coordinator.work_once(agent_id=NOTE_AGENT, client=_LostAcceptedClient()) is True
    replacement = _Client()
    assert coordinator.work_once(agent_id=NOTE_AGENT, client=replacement) is True

    assert len(replacement.spawned) == 1
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    note_dispatch = next(
        row
        for row in snapshot["dispatches"]
        if row["agent_id"] == NOTE_AGENT and row["kind"] == "update"
    )
    assert note_dispatch["id"] == dispatch["id"]
    assert note_dispatch["attempt_count"] == 2
    assert note_dispatch["state"] == "complete"


def test_worker_reauthenticates_after_gateway_session_expires(tmp_path: Path) -> None:
    store, ledger, coordinator = _coordinator(tmp_path)
    _speech(store)
    coordinator.allocate_once()
    clients = [_ExpiredClient(), _Client()]
    created: list[_Client] = []

    def client_factory() -> _Client:
        client = clients[len(created)]
        created.append(client)
        if len(created) == 2:
            original_task = client.task

            def finish(task_id: str) -> dict[str, Any]:
                result = original_task(task_id)
                coordinator._stop.set()
                return result

            client.task = finish  # type: ignore[method-assign]
        return client

    coordinator._client_factory = client_factory
    coordinator.poll_interval = 0
    coordinator._worker_loop(NOTE_AGENT)

    assert len(created) == 2
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    note = next(row for row in snapshot["agents"] if row["agent_id"] == NOTE_AGENT)
    assert note["state"] == "caught_up"


def test_one_agent_failure_does_not_block_the_other_agent(tmp_path: Path) -> None:
    store, ledger, coordinator = _coordinator(tmp_path)
    _speech(store)
    coordinator.allocate_once()
    ledger.append_context(
        meeting_id=MEETING_ID,
        owner_id="owner",
        text="The launch date is Friday.",
    )
    failed_client = _Client()
    failed_client.status = {
        "done": True,
        "outcome": "failed",
        "error": "provider rejected the note turn",
    }
    healthy_client = _Client()

    assert coordinator.work_once(agent_id=NOTE_AGENT, client=failed_client) is True
    assert coordinator.work_once(agent_id=SKETCH_AGENT, client=healthy_client) is True
    assert coordinator.work_once(agent_id=NOTE_AGENT, client=healthy_client) is False
    assert coordinator.work_once(agent_id=SKETCH_AGENT, client=healthy_client) is False

    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    states = {row["agent_id"]: row for row in snapshot["agents"]}
    assert states[NOTE_AGENT]["state"] == "failed"
    assert states[SKETCH_AGENT]["state"] == "caught_up"

    assert (
        ledger.retry_failed(
            meeting_id=MEETING_ID,
            owner_id="owner",
            agent_id=NOTE_AGENT,
        )
        == 1
    )
    assert coordinator.work_once(agent_id=NOTE_AGENT, client=healthy_client) is True


def test_default_cadence_delays_note_three_minutes_and_sketch_five(
    tmp_path: Path,
) -> None:
    current_seconds = [0]

    def timestamp() -> str:
        return (datetime.fromisoformat(NOW) + timedelta(seconds=current_seconds[0])).isoformat(
            timespec="milliseconds"
        )

    data_dir = tmp_path / "data"
    store = MeetingStore(
        data_dir,
        clock=timestamp,
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
    )
    store.initialize()
    store.create_meeting(title="Cadence", owner_id="owner")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner")
    ledger = AgentLedger(data_dir, clock=timestamp)
    ledger.initialize_outputs(meeting_id=MEETING_ID, owner_id="owner")
    coordinator = AgentCoordinator(
        ledger,
        app_name="whisper-meeting",
        crew_home=tmp_path,
        wall_clock=lambda: NOW_EPOCH + current_seconds[0],
    )
    _speech(store)
    ledger.append_context(meeting_id=MEETING_ID, owner_id="owner", text="Agenda: costs")

    current_seconds[0] = 179
    assert coordinator.allocate_once() is False
    assert ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")["dispatches"] == []

    current_seconds[0] = 180
    assert coordinator.allocate_once() is True
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    assert [row["agent_id"] for row in snapshot["dispatches"]] == [NOTE_AGENT]

    current_seconds[0] = 300
    assert coordinator.allocate_once() is True
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    assert {row["agent_id"] for row in snapshot["dispatches"]} == {
        NOTE_AGENT,
        SKETCH_AGENT,
    }


def test_new_input_merges_into_pending_note_before_repeat_cadence(
    tmp_path: Path,
) -> None:
    current_seconds = [0]

    def timestamp() -> str:
        return (datetime.fromisoformat(NOW) + timedelta(seconds=current_seconds[0])).isoformat(
            timespec="milliseconds"
        )

    data_dir = tmp_path / "data"
    store = MeetingStore(
        data_dir,
        clock=timestamp,
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
    )
    store.initialize()
    store.create_meeting(title="Pending merge", owner_id="owner")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner")
    ledger = AgentLedger(data_dir, clock=timestamp)
    ledger.initialize_outputs(meeting_id=MEETING_ID, owner_id="owner")
    coordinator = AgentCoordinator(
        ledger,
        app_name="whisper-meeting",
        crew_home=tmp_path,
        wall_clock=lambda: NOW_EPOCH + current_seconds[0],
    )
    _speech(store)

    current_seconds[0] = 180
    assert coordinator.allocate_once() is True

    current_seconds[0] = 200
    ledger.append_context(
        meeting_id=MEETING_ID,
        owner_id="owner",
        text="Correction: the project is Atlas.",
    )
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO transcript_segments (
                meeting_id, epoch, audio_start_sample, audio_end_sample,
                language, text, coverage_kind, committed_at
            ) VALUES (?, 1, 16000, 32000, 'en', 'Budget is approved.', 'speech', ?)
            """,
            (MEETING_ID, timestamp()),
        )
        connection.commit()

    assert coordinator.allocate_once() is True
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    assert len(snapshot["dispatches"]) == 1
    dispatch = snapshot["dispatches"][0]
    assert dispatch["agent_id"] == NOTE_AGENT
    claimed = ledger.claim_next(agent_id=NOTE_AGENT)
    assert claimed is not None
    assert "Budget is approved." in claimed["payload_text"]
    assert "Correction: the project is Atlas." in claimed["payload_text"]


def test_four_minute_meeting_with_two_context_messages_uses_three_tasks(
    tmp_path: Path,
) -> None:
    current_seconds = [0]

    def timestamp() -> str:
        return (datetime.fromisoformat(NOW) + timedelta(seconds=current_seconds[0])).isoformat(
            timespec="milliseconds"
        )

    data_dir = tmp_path / "data"
    store = MeetingStore(
        data_dir,
        clock=timestamp,
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
    )
    store.initialize()
    store.create_meeting(title="Task budget", owner_id="owner")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner")
    ledger = AgentLedger(data_dir, clock=timestamp)
    ledger.initialize_outputs(meeting_id=MEETING_ID, owner_id="owner")
    coordinator = AgentCoordinator(
        ledger,
        app_name="whisper-meeting",
        crew_home=tmp_path,
        wall_clock=lambda: NOW_EPOCH + current_seconds[0],
    )
    _speech(store)
    ledger.append_context(meeting_id=MEETING_ID, owner_id="owner", text="Agenda: costs")

    current_seconds[0] = 180
    assert coordinator.allocate_once() is True
    assert coordinator.work_once(agent_id=NOTE_AGENT, client=_Client()) is True

    current_seconds[0] = 200
    ledger.append_context(meeting_id=MEETING_ID, owner_id="owner", text="Attendee: Alice")
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO transcript_segments (
                meeting_id, epoch, audio_start_sample, audio_end_sample,
                language, text, coverage_kind, committed_at
            ) VALUES (?, 1, 16000, 32000, 'en', 'Final point.', 'speech', ?)
            """,
            (MEETING_ID, timestamp()),
        )
        connection.commit()

    current_seconds[0] = 246
    store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner",
        kind="end",
        epoch=1,
        cut_sequence=-1,
        cut_sample=0,
    )
    assert coordinator.allocate_once() is True

    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    assert len(snapshot["dispatches"]) == 3
    assert sorted((row["agent_id"], row["kind"]) for row in snapshot["dispatches"]) == [
        (NOTE_AGENT, "finalize"),
        (NOTE_AGENT, "update"),
        (SKETCH_AGENT, "finalize"),
    ]
