import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from backend.chunks import ChunkMetadata
from backend.store import (
    SCHEMA_VERSION,
    ChunkConflict,
    InvalidMeeting,
    MeetingNotFound,
    MeetingStore,
    StorageLimit,
)

MEETING_ID = "11111111-1111-4111-8111-111111111111"


def _store(data_dir: Path) -> MeetingStore:
    store = MeetingStore(
        data_dir,
        clock=lambda: "2026-10-17T12:00:00.000+00:00",
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
    )
    store.initialize()
    return store


def test_initialize_creates_schema_and_owned_directories(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")

    assert store.schema_version() == SCHEMA_VERSION
    assert store.database_path.is_file()
    assert (store.data_dir / "models").is_dir()
    assert (store.data_dir / "sessions").is_dir()
    assert (store.data_dir / "exports").is_dir()


def test_meeting_history_survives_store_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = _store(data_dir)
    created = store.create_meeting(title="中英 weekly", owner_id="owner-a")

    reopened = MeetingStore(data_dir)
    reopened.initialize()

    assert created["id"] == MEETING_ID
    assert reopened.list_meetings(owner_id="owner-a") == [created]
    assert reopened.list_meetings(owner_id="owner-b") == []


def test_v1_to_current_migration_preserves_meeting_history(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = MeetingStore(data_dir)
    with sqlite3.connect(store.database_path) as connection:
        store._migrate_to_v1(connection)
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            """
            INSERT INTO meetings (
                id, owner_id, title, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "meeting-v1",
                "owner-a",
                "Before migration",
                "draft",
                "2026-10-17T11:00:00.000+00:00",
                "2026-10-17T11:00:00.000+00:00",
            ),
        )

    store.initialize()

    assert store.schema_version() == SCHEMA_VERSION
    assert store.list_meetings(owner_id="owner-a")[0]["title"] == "Before migration"
    with store._connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(meetings)")}
    assert {"last_error"}.issubset(columns)
    with store._connect() as connection:
        chunk_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(audio_chunks)")
        }
    assert {"sample_rate", "channels", "encoding"}.issubset(chunk_columns)


def test_meeting_lookup_does_not_reveal_another_owner(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Private", owner_id="owner-a")

    with pytest.raises(MeetingNotFound):
        store.get_meeting(meeting_id=MEETING_ID, owner_id="owner-b")


def test_title_limit_is_enforced_before_insert(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")

    with pytest.raises(InvalidMeeting):
        store.create_meeting(title="x" * 201, owner_id="owner-a")

    assert store.list_meetings(owner_id="owner-a") == []


def test_newer_schema_is_refused_without_mutation(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = _store(data_dir)
    with store._connect() as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()

    with pytest.raises(RuntimeError, match="newer than supported"):
        store.initialize()

    with store._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


def _chunk(body: bytes, *, sequence: int = 0) -> ChunkMetadata:
    return ChunkMetadata(
        meeting_id=MEETING_ID,
        epoch=1,
        sequence=sequence,
        sample_start=sequence * (len(body) // 2),
        sample_count=len(body) // 2,
        sample_rate=16_000,
        channels=1,
        encoding="pcm_s16le",
        sha256=hashlib.sha256(body).hexdigest(),
    )


def test_chunk_is_durable_and_duplicate_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Durable", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    body = bytes(range(256)) * 4
    metadata = _chunk(body)

    first = store.store_chunk(metadata=metadata, body=body, owner_id="owner-a")
    duplicate = store.store_chunk(metadata=metadata, body=body, owner_id="owner-a")

    assert first["durable"] is True
    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    path = store.data_dir / "sessions" / MEETING_ID / "spool" / metadata.filename()
    assert path.read_bytes() == body


def test_same_sequence_with_different_audio_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Conflict", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    first = bytes([1, 0]) * 8000
    second = bytes([2, 0]) * 8000
    store.store_chunk(metadata=_chunk(first), body=first, owner_id="owner-a")

    with pytest.raises(ChunkConflict):
        store.store_chunk(metadata=_chunk(second), body=second, owner_id="owner-a")


def test_reconcile_recovers_file_published_before_ledger_commit(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Recover", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    body = bytes([3, 0]) * 8000
    metadata = _chunk(body)
    store._spool.write(metadata, body)

    result = store.reconcile_spool()

    assert result == {"recovered": 1, "missing": 0}
    assert store.store_chunk(metadata=metadata, body=body, owner_id="owner-a")["duplicate"] is True


def test_spool_cap_refuses_next_chunk_without_evicting_first(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = MeetingStore(
        data_dir,
        clock=lambda: "2026-10-17T12:00:00.000+00:00",
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
        spool_cap_bytes=16_000,
    )
    store.initialize()
    store.create_meeting(title="Bounded", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    first = bytes([1, 0]) * 8000
    second = bytes([2, 0]) * 8000
    first_metadata = _chunk(first)
    store.store_chunk(metadata=first_metadata, body=first, owner_id="owner-a")

    with pytest.raises(StorageLimit):
        store.store_chunk(
            metadata=_chunk(second, sequence=1),
            body=second,
            owner_id="owner-a",
        )

    first_path = store.data_dir / "sessions" / MEETING_ID / "spool" / first_metadata.filename()
    assert first_path.is_file()


def test_pause_completes_only_after_exact_transcript_coverage(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Exact pause", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    body = bytes([1, 0]) * 8000
    store.store_chunk(metadata=_chunk(body), body=body, owner_id="owner-a")

    operation = store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="pause",
        epoch=1,
        cut_sequence=0,
        cut_sample=8000,
    )

    assert operation["state"] == "transcribing"
    assert store.get_meeting(meeting_id=MEETING_ID, owner_id="owner-a")["state"] == "draining"
    with pytest.raises(InvalidMeeting, match="already in progress"):
        store.request_drain(
            meeting_id=MEETING_ID,
            owner_id="owner-a",
            kind="end",
            epoch=1,
            cut_sequence=0,
            cut_sample=8000,
        )
    rows = store.pending_recognition_group()
    job = store.claim_recognition(chunks=tuple(rows), audio_start_sample=0, audio_end_sample=8000)
    assert job is not None
    store.complete_recognition(job=job, segments=[], language="")

    progress = store.meeting_progress(meeting_id=MEETING_ID, owner_id="owner-a")
    assert progress["meeting"]["state"] == "paused"
    assert progress["operation"]["state"] == "complete"
    assert progress["progress"]["transcribed_chunks"] == 1
    assert progress["progress"]["spool_bytes"] == len(body)
    assert len(store._spool.iter_objects()) == 1
    transcript = store.transcript(meeting_id=MEETING_ID, owner_id="owner-a")
    assert transcript[0]["coverage_kind"] == "empty_decode"


def test_drain_rejects_a_cut_with_a_missing_chunk(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Gap", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    body = bytes([1, 0]) * 8000
    store.store_chunk(metadata=_chunk(body), body=body, owner_id="owner-a")
    store.store_chunk(metadata=_chunk(body, sequence=2), body=body, owner_id="owner-a")

    with pytest.raises(InvalidMeeting, match="missing or unexpected"):
        store.request_drain(
            meeting_id=MEETING_ID,
            owner_id="owner-a",
            kind="pause",
            epoch=1,
            cut_sequence=2,
            cut_sample=24_000,
        )


def test_empty_pause_resume_and_end_are_durable_operations(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Empty", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")

    pause = store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="pause",
        epoch=1,
        cut_sequence=-1,
        cut_sample=0,
    )
    resumed = store.resume_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    end = store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="end",
        epoch=2,
        cut_sequence=-1,
        cut_sample=0,
    )

    assert pause["state"] == "complete"
    assert resumed["epoch"] == 2
    assert end["state"] == "complete"
    assert store.get_meeting(meeting_id=MEETING_ID, owner_id="owner-a")["state"] == "ended"


def test_paused_meeting_can_end_without_opening_another_epoch(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Pause then end", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="pause",
        epoch=1,
        cut_sequence=-1,
        cut_sample=0,
    )

    operation = store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="end",
        epoch=1,
        cut_sequence=-1,
        cut_sample=0,
    )

    assert operation["state"] == "complete"
    assert store.get_meeting(meeting_id=MEETING_ID, owner_id="owner-a")["state"] == "ended"


def test_event_cursor_replays_only_later_events(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Replay", owner_id="owner-a")
    first = store.events_after(meeting_id=MEETING_ID, owner_id="owner-a", cursor=0)
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")

    replay = store.events_after(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        cursor=first[-1]["cursor"],
    )

    assert [event["kind"] for event in replay] == ["capture.started"]


def test_completed_pause_is_idempotent_but_rejects_new_audio(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Retry", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    first = store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="pause",
        epoch=1,
        cut_sequence=-1,
        cut_sample=0,
    )

    retry = store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="pause",
        epoch=1,
        cut_sequence=-1,
        cut_sample=0,
    )

    assert retry["id"] == first["id"]
    with pytest.raises(InvalidMeeting):
        store.request_drain(
            meeting_id=MEETING_ID,
            owner_id="owner-a",
            kind="pause",
            epoch=1,
            cut_sequence=-1,
            cut_sample=1,
        )
    body = bytes([1, 0]) * 8000
    with pytest.raises(InvalidMeeting, match="already stopped"):
        store.store_chunk(metadata=_chunk(body), body=body, owner_id="owner-a")


def test_retry_keeps_old_coverage_visible_and_queues_retained_pcm(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Retry coverage", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    body = bytes([1, 0]) * 8000
    store.store_chunk(metadata=_chunk(body), body=body, owner_id="owner-a")
    store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="end",
        epoch=1,
        cut_sequence=0,
        cut_sample=8000,
    )
    rows = store.pending_recognition_group()
    job = store.claim_recognition(chunks=tuple(rows), audio_start_sample=0, audio_end_sample=8000)
    assert job is not None
    store.complete_recognition(job=job, segments=[], language="", coverage_kind="silence")
    original = store.transcript(meeting_id=MEETING_ID, owner_id="owner-a")

    queued = store.retry_coverage(
        meeting_id=MEETING_ID,
        segment_id=int(original[0]["id"]),
        owner_id="owner-a",
    )

    assert queued["state"] == "queued"
    assert store.transcript(meeting_id=MEETING_ID, owner_id="owner-a") == original
    pending = store.pending_recognition_group()
    assert len(pending) == 1
    assert pending[0]["force_decode"] == 1


def test_v6_requeues_retained_silence_for_real_decode(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Old VAD coverage", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    body = bytes([1, 0]) * 8000
    store.store_chunk(metadata=_chunk(body), body=body, owner_id="owner-a")
    store.store_chunk(metadata=_chunk(body, sequence=1), body=body, owner_id="owner-a")
    store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="end",
        epoch=1,
        cut_sequence=1,
        cut_sample=16000,
    )
    rows = store.pending_recognition_group()
    first_job = store.claim_recognition(
        chunks=(rows[0],), audio_start_sample=0, audio_end_sample=8000
    )
    assert first_job is not None
    store.complete_recognition(
        job=first_job,
        segments=[{"start": 0, "end": 8000, "text": "heard"}],
        language="en",
    )
    second_job = store.claim_recognition(
        chunks=(rows[1],), audio_start_sample=8000, audio_end_sample=16000
    )
    assert second_job is not None
    store.complete_recognition(job=second_job, segments=[], language="", coverage_kind="silence")
    with store._connect() as connection:
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    reopened = MeetingStore(store.data_dir, min_free_bytes=0)
    reopened.initialize()
    reopened.reconcile_spool()

    assert reopened.schema_version() == 6
    pending = reopened.pending_recognition_group()
    assert len(pending) == 2
    assert {row["force_decode"] for row in pending} == {1}
    transcript = reopened.transcript(meeting_id=MEETING_ID, owner_id="owner-a")
    assert [row["coverage_kind"] for row in transcript] == ["speech", "silence"]


def test_retention_removes_only_expired_ended_pcm(tmp_path: Path) -> None:
    current = ["2026-10-01T12:00:00.000+00:00"]
    store = MeetingStore(
        tmp_path / "data",
        clock=lambda: current[0],
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
        raw_audio_retention_days=7,
        raw_audio_recent_meetings=0,
    )
    store.initialize()
    store.create_meeting(title="Expired audio", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    body = bytes([1, 0]) * 8000
    store.store_chunk(metadata=_chunk(body), body=body, owner_id="owner-a")
    store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="end",
        epoch=1,
        cut_sequence=0,
        cut_sample=8000,
    )
    rows = store.pending_recognition_group()
    job = store.claim_recognition(chunks=tuple(rows), audio_start_sample=0, audio_end_sample=8000)
    assert job is not None
    store.complete_recognition(job=job, segments=[], language="", coverage_kind="silence")
    assert len(store._spool.iter_objects()) == 1

    current[0] = "2026-10-09T12:00:00.000+00:00"
    assert store.enforce_audio_retention() == 1

    assert store._spool.iter_objects() == []
    transcript = store.transcript(meeting_id=MEETING_ID, owner_id="owner-a")
    assert transcript[0]["coverage_kind"] == "silence"
    with store._connect() as connection:
        assert connection.execute("SELECT pcm_retained FROM audio_chunks").fetchone()[0] == 0


def test_delete_refuses_active_meeting_and_removes_one_owned_tree(tmp_path: Path) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Delete me", owner_id="owner-a")
    session_dir = store.data_dir / "sessions" / MEETING_ID
    session_dir.mkdir()
    (session_dir / "owned.txt").write_text("owned", encoding="utf-8")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")

    with pytest.raises(InvalidMeeting, match="cannot be deleted"):
        store.delete_meeting(meeting_id=MEETING_ID, owner_id="owner-a")

    store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="pause",
        epoch=1,
        cut_sequence=-1,
        cut_sample=0,
    )
    assert store.delete_meeting(meeting_id=MEETING_ID, owner_id="owner-a") == {
        "deleted": MEETING_ID
    }
    assert not session_dir.exists()
    assert store.list_meetings(owner_id="owner-a") == []


def test_startup_restores_a_quarantine_when_database_delete_did_not_commit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Restore delete", owner_id="owner-a")
    session_dir = store.data_dir / "sessions" / MEETING_ID
    session_dir.mkdir()
    (session_dir / "owned.txt").write_text("owned", encoding="utf-8")
    quarantine = session_dir.parent / f".deleting-{MEETING_ID}-{'a' * 32}"
    session_dir.rename(quarantine)

    reopened = MeetingStore(store.data_dir, min_free_bytes=0)
    reopened.initialize()

    assert session_dir.is_dir()
    assert not quarantine.exists()
    assert (
        reopened.get_meeting(meeting_id=MEETING_ID, owner_id="owner-a")["title"] == "Restore delete"
    )


def test_startup_finishes_file_cleanup_after_database_delete_commits(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = _store(tmp_path / "data")
    store.create_meeting(title="Finish delete", owner_id="owner-a")
    session_dir = store.data_dir / "sessions" / MEETING_ID
    session_dir.mkdir()
    (session_dir / "owned.txt").write_text("owned", encoding="utf-8")
    real_rmtree = shutil.rmtree

    def interrupt_cleanup(path: Path) -> None:
        raise OSError(f"injected cleanup interruption for {path.name}")

    monkeypatch.setattr(shutil, "rmtree", interrupt_cleanup)
    store.delete_meeting(meeting_id=MEETING_ID, owner_id="owner-a")

    assert not session_dir.exists()
    quarantine = next((store.data_dir / "sessions").glob(".deleting-*"))
    assert quarantine.is_dir()
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    reopened = MeetingStore(store.data_dir, min_free_bytes=0)
    reopened.initialize()

    assert not quarantine.exists()
    with pytest.raises(MeetingNotFound):
        reopened.get_meeting(meeting_id=MEETING_ID, owner_id="owner-a")
