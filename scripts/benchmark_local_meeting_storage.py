#!/usr/bin/env python3
"""Measure the durable PCM spool used by the local Meetings MVP design.

The probe deliberately has no recognizer. It writes every 500 ms PCM chunk by
temporary-file + fsync + atomic rename, commits its ledger row to SQLite, and
then verifies the complete sequence. That is the Phase 0 proof that recognition
can remain stopped while capture continues until an explicit storage bound.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

SAMPLE_RATE = 16_000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
DEFAULT_CHUNK_SECONDS = 0.5
DEFAULT_BROWSER_CAP_BYTES = 512 * 1024**2
DEFAULT_BACKEND_CAP_BYTES = 2 * 1024**3
DEFAULT_FREE_DISK_RESERVE_BYTES = 5 * 1024**3


def storage_budget(
    *,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    browser_cap_bytes: int = DEFAULT_BROWSER_CAP_BYTES,
    backend_cap_bytes: int = DEFAULT_BACKEND_CAP_BYTES,
) -> dict[str, float | int]:
    """Return exact raw-PCM rates and conservative cap durations."""

    bytes_per_second = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
    chunk_bytes = round(bytes_per_second * chunk_seconds)
    chunks_per_hour = round(3600 / chunk_seconds)
    bytes_per_hour = bytes_per_second * 3600
    return {
        "bytes_per_second": bytes_per_second,
        "chunk_bytes": chunk_bytes,
        "chunks_per_hour": chunks_per_hour,
        "raw_bytes_per_hour": bytes_per_hour,
        "raw_mib_per_hour": bytes_per_hour / 1024**2,
        "browser_cap_raw_hours": browser_cap_bytes / bytes_per_hour,
        "backend_cap_raw_hours": backend_cap_bytes / bytes_per_hour,
    }


def _allocated_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_blocks * 512
    return total


def _apparent_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _prepare_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("""
        CREATE TABLE audio_chunks (
            sequence INTEGER PRIMARY KEY,
            sample_start INTEGER NOT NULL,
            sample_count INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            spool_name TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL
        )
        """)
    connection.commit()
    return connection


def run_probe(root: Path, *, hours: float, chunk_seconds: float) -> dict[str, Any]:
    """Write and verify a stalled-recognizer spool under *root*."""

    budget = storage_budget(chunk_seconds=chunk_seconds)
    chunk_bytes = int(budget["chunk_bytes"])
    chunk_count = round(hours * 3600 / chunk_seconds)
    sample_count = round(SAMPLE_RATE * chunk_seconds)
    spool = root / "spool"
    spool.mkdir(parents=True)
    database = root / "meetings.sqlite3"
    connection = _prepare_database(database)
    payload = os.urandom(chunk_bytes)
    digest = hashlib.sha256(payload).hexdigest()

    started = time.monotonic()
    try:
        for sequence in range(chunk_count):
            final = spool / f"{sequence:08d}.pcm"
            temporary = spool / f".{sequence:08d}.tmp"
            with temporary.open("wb") as destination:
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            temporary.replace(final)
            connection.execute(
                "INSERT INTO audio_chunks VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sequence,
                    sequence * sample_count,
                    sample_count,
                    digest,
                    final.name,
                    "durable",
                ),
            )
            connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        rows = connection.execute(
            "SELECT sequence, sample_start, sample_count, sha256, spool_name "
            "FROM audio_chunks ORDER BY sequence"
        ).fetchall()
        if len(rows) != chunk_count:
            raise RuntimeError(f"ledger has {len(rows)} rows, expected {chunk_count}")
        for expected, row in enumerate(rows):
            sequence, sample_start, stored_samples, stored_digest, spool_name = row
            if sequence != expected or sample_start != expected * sample_count:
                raise RuntimeError(f"non-contiguous ledger at sequence {expected}")
            if stored_samples != sample_count or stored_digest != digest:
                raise RuntimeError(f"invalid metadata at sequence {expected}")
            path = spool / spool_name
            if not path.is_file() or path.stat().st_size != chunk_bytes:
                raise RuntimeError(f"missing or truncated spool file at sequence {expected}")
    finally:
        connection.close()

    wall_seconds = time.monotonic() - started
    apparent = _apparent_bytes(root)
    allocated = _allocated_bytes(root)
    audio_seconds = chunk_count * chunk_seconds
    return {
        "recognizer_state": "deliberately_stalled",
        "capture_completed": True,
        "hours": hours,
        "chunk_seconds": chunk_seconds,
        "chunk_count": chunk_count,
        "chunk_bytes": chunk_bytes,
        "audio_seconds": audio_seconds,
        "wall_seconds": wall_seconds,
        "ledger_rows": chunk_count,
        "contiguous_through_sequence": chunk_count - 1,
        "spool_files": chunk_count,
        "apparent_bytes": apparent,
        "allocated_bytes": allocated,
        "allocated_mib_per_audio_hour": (allocated / 1024**2) / (audio_seconds / 3600),
        "filesystem_overhead_ratio": allocated / (chunk_count * chunk_bytes),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=2.0)
    parser.add_argument("--chunk-seconds", type=float, default=DEFAULT_CHUNK_SECONDS)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.hours <= 0 or args.chunk_seconds <= 0:
        raise SystemExit("hours and chunk duration must be positive")
    with tempfile.TemporaryDirectory(prefix="local-meeting-storage-") as temporary:
        root = Path(temporary)
        free_before = shutil.disk_usage(root).free
        measurement = run_probe(root, hours=args.hours, chunk_seconds=args.chunk_seconds)
        free_after = shutil.disk_usage(root).free

    report = {
        "schema_version": 1,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "format": {
            "sample_rate_hz": SAMPLE_RATE,
            "channels": CHANNELS,
            "encoding": "pcm_s16le",
        },
        "budget": storage_budget(chunk_seconds=args.chunk_seconds),
        "policy": {
            "browser_queue_cap_bytes": DEFAULT_BROWSER_CAP_BYTES,
            "backend_spool_cap_bytes": DEFAULT_BACKEND_CAP_BYTES,
            "minimum_free_disk_reserve_bytes": DEFAULT_FREE_DISK_RESERVE_BYTES,
        },
        "measurement": measurement,
        "disk_free_before_bytes": free_before,
        "disk_free_after_write_bytes": free_after,
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
