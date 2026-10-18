from pathlib import Path

from backend.agent_ledger import NOTE_AGENT, SKETCH_AGENT, AgentLedger
from backend.store import MeetingStore

MEETING_ID = "00000000-0000-0000-0000-000000000123"
NOW = "2026-10-17T20:00:00.000+00:00"


def _active_meeting(tmp_path: Path) -> tuple[MeetingStore, AgentLedger]:
    data_dir = tmp_path / "data"
    store = MeetingStore(
        data_dir,
        clock=lambda: NOW,
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
    )
    store.initialize()
    store.create_meeting(title="Agent test", owner_id="owner")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner")
    ledger = AgentLedger(data_dir, clock=lambda: NOW, id_factory=lambda: "context-1")
    return store, ledger


def _insert_speech(store: MeetingStore) -> None:
    with store._connect() as connection:
        connection.executemany(
            """
            INSERT INTO transcript_segments (
                meeting_id, epoch, audio_start_sample, audio_end_sample,
                language, text, coverage_kind, committed_at
            ) VALUES (?, 1, ?, ?, ?, ?, 'speech', ?)
            """,
            (
                (MEETING_ID, 0, 16_000, "en", "Alice owns the checklist.", NOW),
                (MEETING_ID, 16_000, 32_000, "en", "Review is Friday.", NOW),
            ),
        )
        connection.commit()


def test_agent_outputs_and_state_are_initialized_without_touching_capture(tmp_path: Path) -> None:
    store, ledger = _active_meeting(tmp_path)

    outputs = ledger.initialize_outputs(meeting_id=MEETING_ID, owner_id="owner")

    assert Path(outputs[NOTE_AGENT]).read_text(encoding="utf-8").startswith("# Meeting notes")
    assert "Sketch pending" in Path(outputs[SKETCH_AGENT]).read_text(encoding="utf-8")
    assert store.get_meeting(meeting_id=MEETING_ID, owner_id="owner")["state"] == "recording"
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    assert [row["agent_id"] for row in snapshot["agents"]] == sorted([NOTE_AGENT, SKETCH_AGENT])


def test_speech_and_context_allocate_durable_idempotent_work(tmp_path: Path) -> None:
    store, ledger = _active_meeting(tmp_path)
    ledger.initialize_outputs(meeting_id=MEETING_ID, owner_id="owner")
    _insert_speech(store)

    speech = ledger.queue_speech(meeting_id=MEETING_ID, owner_id="owner")
    assert len(speech) == 2
    assert ledger.queue_speech(meeting_id=MEETING_ID, owner_id="owner") == []
    assert {row["agent_id"] for row in speech} == {NOTE_AGENT, SKETCH_AGENT}
    assert all(row["payload_hash"] for row in speech)

    note = ledger.claim_next(agent_id=NOTE_AGENT)
    assert note is not None
    assert note["kind"] == "speech"
    assert note["attempt_count"] == 1
    ledger.mark_accepted(dispatch_id=note["id"], execution_ref="task-1")
    ledger.complete(dispatch_id=note["id"])

    context = ledger.append_context(
        meeting_id=MEETING_ID,
        owner_id="owner",
        text="  The correct product name is KiroCrew.  ",
    )
    assert context["text"] == "The correct product name is KiroCrew."
    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    note_state = next(row for row in snapshot["agents"] if row["agent_id"] == NOTE_AGENT)
    assert note_state["delivered_through_segment_id"] == 2
    assert note_state["output_revision"] == 1
    assert snapshot["context"] == [context]
    assert {row["kind"] for row in snapshot["dispatches"]} == {"speech", "chat"}
    assert all("payload_text" not in row for row in snapshot["dispatches"])


def test_finalize_is_idempotent_and_does_not_hold_meeting_end(tmp_path: Path) -> None:
    store, ledger = _active_meeting(tmp_path)
    ledger.initialize_outputs(meeting_id=MEETING_ID, owner_id="owner")
    operation = store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner",
        kind="end",
        epoch=1,
        cut_sequence=-1,
        cut_sample=0,
    )
    assert operation["state"] == "complete"
    assert store.get_meeting(meeting_id=MEETING_ID, owner_id="owner")["state"] == "ended"

    first = ledger.queue_finalize(meeting_id=MEETING_ID, owner_id="owner")
    second = ledger.queue_finalize(meeting_id=MEETING_ID, owner_id="owner")

    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert len(first) == 2


def test_failed_dispatches_can_be_retried_by_meeting_agent(tmp_path: Path) -> None:
    store, ledger = _active_meeting(tmp_path)
    _insert_speech(store)
    ledger.queue_speech(meeting_id=MEETING_ID, owner_id="owner")
    dispatch = ledger.claim_next(agent_id=NOTE_AGENT)
    assert dispatch is not None
    ledger.fail(dispatch_id=dispatch["id"], error="provider unavailable")

    assert (
        ledger.retry_failed(
            meeting_id=MEETING_ID,
            owner_id="owner",
            agent_id=NOTE_AGENT,
        )
        == 1
    )
    assert (
        ledger.retry_failed(
            meeting_id=MEETING_ID,
            owner_id="owner",
            agent_id=NOTE_AGENT,
        )
        == 0
    )

    snapshot = ledger.snapshot(meeting_id=MEETING_ID, owner_id="owner")
    note = next(row for row in snapshot["agents"] if row["agent_id"] == NOTE_AGENT)
    assert note["state"] == "pending"
    assert note["last_error"] == ""


def test_long_transcript_backlog_is_allocated_in_bounded_batches(tmp_path: Path) -> None:
    store, ledger = _active_meeting(tmp_path)
    with store._connect() as connection:
        connection.executemany(
            """
            INSERT INTO transcript_segments (
                meeting_id, epoch, audio_start_sample, audio_end_sample,
                language, text, coverage_kind, committed_at
            ) VALUES (?, 1, ?, ?, 'en', ?, 'speech', ?)
            """,
            (
                (MEETING_ID, 0, 16_000, "a" * 8_000, NOW),
                (MEETING_ID, 16_000, 32_000, "b" * 8_000, NOW),
            ),
        )
        connection.commit()

    first = ledger.queue_speech(meeting_id=MEETING_ID, owner_id="owner")
    second = ledger.queue_speech(meeting_id=MEETING_ID, owner_id="owner")
    third = ledger.queue_speech(meeting_id=MEETING_ID, owner_id="owner")

    assert {row["last_segment_id"] for row in first} == {1}
    assert {row["last_segment_id"] for row in second} == {2}
    assert third == []
