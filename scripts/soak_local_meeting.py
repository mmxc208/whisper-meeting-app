#!/usr/bin/env python3
"""Accelerated two-hour durability soak for Whisper Meeting."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from backend.chunks import ChunkMetadata  # noqa: E402
from backend.recognition import RecognitionScheduler  # noqa: E402
from backend.store import MeetingStore  # noqa: E402

SAMPLE_RATE = 16_000


class ReadyModels:
    def ready(self) -> bool:
        return True


class EmptyRecognizer:
    def transcribe(self, parts: list[dict[str, Any]]) -> dict[str, Any]:
        return {"language": "", "segments": []}

    def close(self) -> None:
        return


def run_soak(*, data_dir: Path, hours: float, chunk_seconds: int) -> dict[str, Any]:
    if hours <= 0 or chunk_seconds < 1 or chunk_seconds > 30:
        raise ValueError("hours must be positive and chunk seconds must be between 1 and 30")
    total_samples = int(hours * 3600 * SAMPLE_RATE)
    samples_per_chunk = chunk_seconds * SAMPLE_RATE
    chunk_count = (total_samples + samples_per_chunk - 1) // samples_per_chunk
    full_body = bytes(samples_per_chunk * 2)
    full_digest = hashlib.sha256(full_body).hexdigest()
    started = time.monotonic()
    store = MeetingStore(data_dir, min_free_bytes=0)
    store.initialize()
    meeting = store.create_meeting(title="Accelerated two-hour soak", owner_id="soak-owner")
    meeting_id = str(meeting["id"])
    store.start_capture(meeting_id=meeting_id, owner_id="soak-owner")

    sample_start = 0
    for sequence in range(chunk_count):
        sample_count = min(samples_per_chunk, total_samples - sample_start)
        body = full_body if sample_count == samples_per_chunk else bytes(sample_count * 2)
        digest = (
            full_digest if sample_count == samples_per_chunk else hashlib.sha256(body).hexdigest()
        )
        metadata = ChunkMetadata(
            meeting_id=meeting_id,
            epoch=1,
            sequence=sequence,
            sample_start=sample_start,
            sample_count=sample_count,
            sample_rate=SAMPLE_RATE,
            channels=1,
            encoding="pcm_s16le",
            sha256=digest,
        )
        store.store_chunk(metadata=metadata, body=body, owner_id="soak-owner")
        sample_start += sample_count

    before_drain = store.meeting_progress(meeting_id=meeting_id, owner_id="soak-owner")
    store.request_drain(
        meeting_id=meeting_id,
        owner_id="soak-owner",
        kind="end",
        epoch=1,
        cut_sequence=chunk_count - 1,
        cut_sample=total_samples,
    )
    scheduler = RecognitionScheduler(
        store,
        ReadyModels(),  # type: ignore[arg-type]
        recognizer=EmptyRecognizer(),
    )
    jobs = 0
    while scheduler.run_once():
        jobs += 1
    after_drain = store.meeting_progress(meeting_id=meeting_id, owner_id="soak-owner")
    transcript = store.transcript(meeting_id=meeting_id, owner_id="soak-owner")
    with store._connect() as connection:
        events = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    return {
        "schema_version": 1,
        "audio_hours": hours,
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "encoding": "pcm_s16le",
        "chunk_seconds": chunk_seconds,
        "chunks": chunk_count,
        "samples": total_samples,
        "bytes_before_drain": before_drain["progress"]["spool_bytes"],
        "recognition_jobs": jobs,
        "transcript_rows": len(transcript),
        "covered_through_sample": max(
            (int(segment["audio_end_sample"]) for segment in transcript), default=0
        ),
        "terminal_state": after_drain["meeting"]["state"],
        "pending_chunks": after_drain["progress"]["pending_chunks"],
        "spool_bytes_after_drain": after_drain["progress"]["spool_bytes"],
        "spool_objects_after_drain": len(store._spool.iter_objects()),
        "events": events,
        "database_bytes": store.database_path.stat().st_size,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=2.0)
    parser.add_argument("--chunk-seconds", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="whisper-meeting-soak-") as temporary:
        result = run_soak(
            data_dir=Path(temporary) / "data",
            hours=args.hours,
            chunk_seconds=args.chunk_seconds,
        )
    text = json.dumps(result, indent=2) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        args.output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
