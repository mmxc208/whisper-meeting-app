import hashlib
import math
import struct
import threading
from pathlib import Path
from typing import Any

from backend.chunks import ChunkMetadata
from backend.model import ModelArtifact, ModelManager
from backend.recognition import RecognitionScheduler
from backend.segmenter import MAX_UTTERANCE_SAMPLES, choose_segment
from backend.store import MeetingStore

MEETING_ID = "11111111-1111-4111-8111-111111111111"


class FakeRecognizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[dict[str, Any]]] = []

    def transcribe(self, parts: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(parts)
        if self.fail:
            raise RuntimeError("worker exited")
        return {
            "ok": True,
            "language": "zh",
            "segments": [{"start": 0, "end": 32_000, "text": "你好 hello"}],
            "filtered_segments": [],
        }

    def close(self) -> None:
        return


class FailOnceRecognizer(FakeRecognizer):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0
        self.succeeded = threading.Event()

    def transcribe(self, parts: list[dict[str, Any]]) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("transient worker failure")
        result = super().transcribe(parts)
        self.succeeded.set()
        return result


class EmptyRecognizer(FakeRecognizer):
    def transcribe(self, parts: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(parts)
        return {
            "ok": True,
            "language": "en",
            "segments": [],
            "filtered_segments": [],
        }


def _store(tmp_path: Path) -> MeetingStore:
    store = MeetingStore(
        tmp_path / "data",
        clock=lambda: "2026-10-17T12:00:00.000+00:00",
        id_factory=lambda: MEETING_ID,
        min_free_bytes=0,
    )
    store.initialize()
    store.create_meeting(title="Bilingual", owner_id="owner-a")
    store.start_capture(meeting_id=MEETING_ID, owner_id="owner-a")
    return store


def _models(store: MeetingStore) -> ModelManager:
    body = b"model"
    artifact = ModelArtifact(
        artifact_id="test-model",
        filename="ggml-test.bin",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        url="https://invalid.example/model",
    )
    source = store.data_dir.parent / "source.bin"
    source.write_bytes(body)
    manager = ModelManager(
        store.data_dir,
        store,
        artifact=artifact,
        source_paths=(source,),
        self_test=lambda _path: None,
        dependency_probe=lambda: None,
    )
    manager.install()
    return manager


def _pcm(*, tone: bool) -> bytes:
    values = []
    for sample in range(8000):
        value = int(12_000 * math.sin(2 * math.pi * 440 * sample / 16_000)) if tone else 0
        values.append(value)
    return struct.pack(f"<{len(values)}h", *values)


def _put(store: MeetingStore, sequence: int, body: bytes) -> None:
    metadata = ChunkMetadata(
        meeting_id=MEETING_ID,
        epoch=1,
        sequence=sequence,
        sample_start=sequence * 8000,
        sample_count=8000,
        sample_rate=16_000,
        channels=1,
        encoding="pcm_s16le",
        sha256=hashlib.sha256(body).hexdigest(),
    )
    store.store_chunk(metadata=metadata, body=body, owner_id="owner-a")


def _speech_then_pause(store: MeetingStore, start_sequence: int = 0) -> None:
    for offset in range(6):
        _put(store, start_sequence + offset, _pcm(tone=offset < 4))


def test_scheduler_commits_pause_aligned_bilingual_segment(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _speech_then_pause(store)
    recognizer = FakeRecognizer()
    scheduler = RecognitionScheduler(store, _models(store), recognizer=recognizer)

    assert scheduler.run_once() is True

    transcript = store.transcript(meeting_id=MEETING_ID, owner_id="owner-a")
    assert [segment["text"] for segment in transcript if segment["text"]] == ["你好 hello"]
    assert transcript[0]["audio_start_sample"] == 0
    assert transcript[0]["audio_end_sample"] == 32_000
    assert transcript[0]["coverage_kind"] == "speech"
    assert transcript[0]["language"] == "zh"
    assert transcript[-1]["coverage_kind"] == "empty_decode"
    ranges = [
        (segment["audio_start_sample"], segment["audio_end_sample"]) for segment in transcript
    ]
    assert ranges[0][0] == 0
    assert ranges[-1][1] == 48_000
    assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))
    assert len(recognizer.calls) == 1
    with store._connect() as connection:
        states = {row[0] for row in connection.execute("SELECT state FROM audio_chunks").fetchall()}
    assert states == {"transcribed"}


def test_adjacent_windows_include_bounded_overlap_without_reordering(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recognizer = FakeRecognizer()
    scheduler = RecognitionScheduler(store, _models(store), recognizer=recognizer)
    _speech_then_pause(store)
    assert scheduler.run_once() is True
    _speech_then_pause(store, start_sequence=6)

    assert scheduler.run_once() is True

    assert len(recognizer.calls) == 2
    overlap = recognizer.calls[1][0]
    assert overlap["start"] == 4000
    assert overlap["end"] == 8000
    transcript = store.transcript(meeting_id=MEETING_ID, owner_id="owner-a")
    ranges = [
        (segment["audio_start_sample"], segment["audio_end_sample"]) for segment in transcript
    ]
    assert ranges[0][0] == 0
    assert ranges[-1][1] == 96_000
    assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))


def test_worker_failure_releases_claim_and_retains_pcm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _speech_then_pause(store)
    scheduler = RecognitionScheduler(store, _models(store), recognizer=FakeRecognizer(fail=True))

    assert scheduler.run_once() is False

    with store._connect() as connection:
        states = [row[0] for row in connection.execute("SELECT state FROM audio_chunks")]
        job_state = connection.execute("SELECT state FROM recognition_jobs").fetchone()[0]
    assert set(states) == {"durable"}
    assert job_state == "interrupted"
    assert all(path.is_file() for _metadata, path in store._spool.iter_objects())
    meeting = store.get_meeting(meeting_id=MEETING_ID, owner_id="owner-a")
    assert meeting["state"] == "recording"
    assert meeting["last_error"] == "RuntimeError: worker exited"
    events = store.events_after(meeting_id=MEETING_ID, owner_id="owner-a", cursor=0)
    assert events[-1]["kind"] == "recognition.interrupted"


def test_scheduler_retries_failed_drain_without_an_external_wake(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _put(store, 0, _pcm(tone=True))
    store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="end",
        epoch=1,
        cut_sequence=0,
        cut_sample=8000,
    )
    recognizer = FailOnceRecognizer()
    scheduler = RecognitionScheduler(
        store,
        _models(store),
        recognizer=recognizer,
        retry_backoff_seconds=(0.01,),
    )

    scheduler.start()
    try:
        assert recognizer.succeeded.wait(timeout=2.0)
    finally:
        scheduler.close()

    progress = store.meeting_progress(meeting_id=MEETING_ID, owner_id="owner-a")
    assert recognizer.attempts == 2
    assert progress["meeting"]["state"] == "ended"
    assert progress["meeting"]["last_error"] == ""
    assert progress["progress"]["pending_chunks"] == 0


def test_restart_releases_claim_left_by_dead_worker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _speech_then_pause(store)
    rows = store.pending_recognition_group()
    job = store.claim_recognition(chunks=tuple(rows), audio_start_sample=0, audio_end_sample=48_000)
    assert job is not None

    reopened = MeetingStore(store.data_dir, min_free_bytes=0)
    reopened.initialize()

    assert len(reopened.pending_recognition_group()) == 6
    with reopened._connect() as connection:
        state = connection.execute("SELECT state FROM recognition_jobs").fetchone()[0]
    assert state == "interrupted"


def test_continuous_speech_is_bounded_at_thirty_seconds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for sequence in range(60):
        _put(store, sequence, _pcm(tone=True))

    window = choose_segment(store.pending_recognition_group(), store.data_dir)

    assert window is not None
    assert window.has_speech is True
    assert window.audio_end_sample - window.audio_start_sample == MAX_UTTERANCE_SAMPLES
    assert len(window.chunks) == 60


def test_energy_vad_never_skips_whisper_decode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for sequence in range(20):
        _put(store, sequence, _pcm(tone=False))
    recognizer = EmptyRecognizer()
    scheduler = RecognitionScheduler(store, _models(store), recognizer=recognizer)

    assert scheduler.run_once() is True

    assert len(recognizer.calls) == 1
    transcript = store.transcript(meeting_id=MEETING_ID, owner_id="owner-a")
    assert {segment["coverage_kind"] for segment in transcript} == {"empty_decode"}


def test_pause_forces_a_short_unfinished_tail_to_final_coverage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _put(store, 0, _pcm(tone=True))
    operation = store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="pause",
        epoch=1,
        cut_sequence=0,
        cut_sample=8000,
    )
    recognizer = FakeRecognizer()
    scheduler = RecognitionScheduler(store, _models(store), recognizer=recognizer)

    assert operation["state"] == "transcribing"
    assert scheduler.run_once() is True

    progress = store.meeting_progress(meeting_id=MEETING_ID, owner_id="owner-a")
    assert progress["meeting"]["state"] == "paused"
    assert progress["progress"]["pending_chunks"] == 0
    assert len(recognizer.calls) == 1


def test_restart_recovers_an_interrupted_drain_and_reaches_paused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _put(store, 0, _pcm(tone=True))
    store.request_drain(
        meeting_id=MEETING_ID,
        owner_id="owner-a",
        kind="pause",
        epoch=1,
        cut_sequence=0,
        cut_sample=8000,
    )
    rows = store.pending_recognition_group()
    claimed = store.claim_recognition(
        chunks=tuple(rows), audio_start_sample=0, audio_end_sample=8000
    )
    assert claimed is not None

    reopened = MeetingStore(store.data_dir, min_free_bytes=0)
    reopened.initialize()
    scheduler = RecognitionScheduler(reopened, _models(reopened), recognizer=FakeRecognizer())

    assert scheduler.run_once() is True
    progress = reopened.meeting_progress(meeting_id=MEETING_ID, owner_id="owner-a")
    assert progress["meeting"]["state"] == "paused"
    assert progress["operation"]["state"] == "complete"
