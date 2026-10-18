"""App-owned pause/VAD segmentation over durable 16 kHz PCM chunks."""

from __future__ import annotations

import math
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 320
SILENCE_FRAMES = 40
MIN_SPEECH_FRAMES = 3
MAX_UTTERANCE_SAMPLES = 30 * SAMPLE_RATE
LOW_CONFIDENCE_WINDOW_SAMPLES = 10 * SAMPLE_RATE
SPEECH_MARGIN_DB = 12.0
MAX_SPEECH_THRESHOLD_DB = -35.0
MIN_SPEECH_DB = -60.0


@dataclass(frozen=True)
class SegmentWindow:
    chunks: tuple[dict[str, Any], ...]
    has_speech: bool
    audio_start_sample: int
    audio_end_sample: int


def _samples(path: Path) -> array[int]:
    values: array[int] = array("h")
    with path.open("rb") as source:
        values.frombytes(source.read())
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _frame_db(values: array[int], start: int) -> float:
    frame = values[start : start + FRAME_SAMPLES]
    if not frame:
        return -200.0
    square_sum = sum(value * value for value in frame)
    rms = math.sqrt(square_sum / len(frame)) / 32768.0
    return 20.0 * math.log10(max(rms, 1e-10))


def choose_segment(
    rows: list[dict[str, Any]], data_dir: Path, *, force: bool = False
) -> SegmentWindow | None:
    """Choose a bounded pause-aligned prefix without changing durable state."""

    if not rows:
        return None
    values: array[int] = array("h")
    chunk_ends: list[int] = []
    for row in rows:
        path = data_dir / str(row["spool_path"])
        chunk = _samples(path)
        if len(chunk) != int(row["sample_count"]):
            raise ValueError("durable PCM length no longer matches its ledger")
        values.extend(chunk)
        chunk_ends.append(len(values))
        if len(values) >= MAX_UTTERANCE_SAMPLES + SAMPLE_RATE:
            break
    if len(values) < FRAME_SAMPLES:
        return None

    energies = [
        _frame_db(values, start)
        for start in range(0, len(values) - FRAME_SAMPLES + 1, FRAME_SAMPLES)
    ]
    ordered = sorted(energies)
    floor = ordered[max(0, len(ordered) // 5 - 1)]
    threshold = min(floor + SPEECH_MARGIN_DB, MAX_SPEECH_THRESHOLD_DB)
    speech = [energy >= threshold and energy >= MIN_SPEECH_DB for energy in energies]

    speech_run = 0
    speech_seen = False
    silence_run = 0
    endpoint = 0
    for index, is_speech in enumerate(speech):
        frame_end = (index + 1) * FRAME_SAMPLES
        if is_speech:
            speech_run += 1
            silence_run = 0
            if speech_run >= MIN_SPEECH_FRAMES:
                speech_seen = True
        else:
            speech_run = 0
            if speech_seen:
                silence_run += 1
        if speech_seen and silence_run >= SILENCE_FRAMES:
            endpoint = frame_end
            break
        if frame_end >= MAX_UTTERANCE_SAMPLES:
            endpoint = frame_end
            break

    if not speech_seen:
        if len(values) < LOW_CONFIDENCE_WINDOW_SAMPLES and not force:
            return None
        endpoint = min(
            len(values),
            MAX_UTTERANCE_SAMPLES if force else LOW_CONFIDENCE_WINDOW_SAMPLES,
        )
    elif endpoint == 0:
        if not force:
            return None
        endpoint = min(len(values), MAX_UTTERANCE_SAMPLES)

    selected_count = next(
        (index + 1 for index, end in enumerate(chunk_ends) if end >= endpoint),
        len(chunk_ends),
    )
    selected = tuple(rows[:selected_count])
    return SegmentWindow(
        chunks=selected,
        has_speech=speech_seen,
        audio_start_sample=int(selected[0]["sample_start"]),
        audio_end_sample=int(selected[-1]["sample_start"]) + int(selected[-1]["sample_count"]),
    )
