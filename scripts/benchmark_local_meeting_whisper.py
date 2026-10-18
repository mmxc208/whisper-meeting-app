#!/usr/bin/env python3
"""Benchmark the local Meetings MVP against Kiro Crew's Metal Whisper runtime.

Run this script with the Python interpreter bundled in KiroCrew.app. The parent
process creates reproducible Mandarin and English speech with macOS ``say`` and
then starts one child per model. A child owns exactly one native Whisper context,
which keeps model lifetime and stderr backend evidence separate.

The default run feeds one hour of 30-second windows to each candidate model. It
does not wait for real time: backlog is reconstructed from each decode's measured
wall time and the window arrival interval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave
from pathlib import Path
from typing import Any, Iterable, Sequence

SAMPLE_RATE = 16_000
DEFAULT_MODELS = ("small", "large-v3-turbo")
MODEL_FILENAMES = {
    "small": "ggml-small.bin",
    "large-v3-turbo": "ggml-large-v3-turbo.bin",
}
MODEL_SHA256 = {
    "small": "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
    "large-v3-turbo": "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
}
ZH_TEXT = (
    "今天我们测试本地会议转录。项目进度正常。下一步检查每一个音频分块是否完整。"
    "所有内容只在这台电脑上处理。"
)
EN_TEXT = (
    "Today we are testing local meeting transcription. The project is on schedule. "
    "The next step is to verify that every audio chunk is complete. "
    "All content stays on this computer."
)
_METAL_ACTIVE = re.compile(r"\busing\s+(?:MTL\d*|Metal)\s+backend\b", re.IGNORECASE)
_THERMAL_STATES = {
    0: "nominal",
    1: "fair",
    2: "serious",
    3: "critical",
}


def _levenshtein(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Return edit distance with memory proportional to the shorter sequence."""

    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row, expected in enumerate(reference, 1):
        current = [row]
        for column, actual in enumerate(hypothesis, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected != actual),
                )
            )
        previous = current
    return previous[-1]


def error_rate(reference: Sequence[str], hypothesis: Sequence[str]) -> float:
    """Return an edit error rate, including insertion cost for empty references."""

    if not reference:
        return float(bool(hypothesis))
    return _levenshtein(reference, hypothesis) / len(reference)


def _english_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.casefold())


def _han_characters(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text)
    return [char for char in normalized if "CJK UNIFIED IDEOGRAPH" in unicodedata.name(char, "")]


def accumulated_backlog(decode_seconds: Iterable[float], window_seconds: float) -> dict[str, float]:
    """Calculate delay behind a real-time source after sequential window decodes."""

    backlog = 0.0
    maximum = 0.0
    for elapsed in decode_seconds:
        backlog = max(0.0, backlog + float(elapsed) - window_seconds)
        maximum = max(maximum, backlog)
    return {"final_seconds": backlog, "maximum_seconds": maximum}


def metal_was_used(native_log: str) -> bool:
    """Recognise the whisper.cpp loader's proof that a Metal device was selected."""

    return _METAL_ACTIVE.search(native_log) is not None


def _thermal_state() -> dict[str, str | int]:
    """Read macOS ProcessInfo.thermalState without requiring root privileges."""

    completed = subprocess.run(
        [
            "/usr/bin/osascript",
            "-l",
            "JavaScript",
            "-e",
            'ObjC.import("Foundation"); $.NSProcessInfo.processInfo.thermalState',
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        raw = int(completed.stdout.strip())
    except ValueError:
        return {"raw": completed.stdout.strip(), "state": "unavailable"}
    return {"raw": raw, "state": _THERMAL_STATES.get(raw, "unknown")}


def _read_wav(path: Path) -> Any:
    import numpy as np

    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getframerate() != SAMPLE_RATE:
            raise RuntimeError(f"{path} is not mono {SAMPLE_RATE} Hz PCM")
        if source.getsampwidth() != 2:
            raise RuntimeError(f"{path} is not 16-bit PCM")
        raw = source.readframes(source.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _window(pcm: Any, seconds: float) -> Any:
    import numpy as np

    wanted = round(seconds * SAMPLE_RATE)
    if pcm.size == 0:
        raise RuntimeError("the generated speech fixture is empty")
    repeats = math.ceil(wanted / pcm.size)
    return np.tile(pcm, repeats)[:wanted]


def _write_fixture(directory: Path, stem: str, voice: str, text: str) -> Path:
    aiff = directory / f"{stem}.aiff"
    wav = directory / f"{stem}.wav"
    subprocess.run(
        ["/usr/bin/say", "-v", voice, "-r", "175", "-o", str(aiff), text],
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/afconvert",
            "-f",
            "WAVE",
            "-d",
            f"LEI16@{SAMPLE_RATE}",
            "-c",
            "1",
            str(aiff),
            str(wav),
        ],
        check=True,
    )
    if _read_wav(wav).size == 0:
        raise RuntimeError(f"macOS voice {voice!r} generated no audio")
    return wav


def create_fixtures(directory: Path) -> dict[str, Path]:
    """Create local TTS fixtures; no downloaded or private meeting audio is used."""

    return {
        "zh": _write_fixture(directory, "zh", "Tingting", ZH_TEXT),
        "en": _write_fixture(directory, "en", "Samantha", EN_TEXT),
    }


def _segments_text(segments: Iterable[Any]) -> str:
    return " ".join(str(getattr(segment, "text", segment)).strip() for segment in segments).strip()


def _worker(
    model_path: Path, fixture_dir: Path, duration_seconds: float, window_seconds: float
) -> int:
    import numpy as np
    from pywhispercpp.model import Model

    fixtures = {language: _read_wav(fixture_dir / f"{language}.wav") for language in ("zh", "en")}
    windows = {
        "zh": _window(fixtures["zh"], window_seconds),
        "en": _window(fixtures["en"], window_seconds),
    }
    half = round(window_seconds * SAMPLE_RATE / 2)
    windows["mixed"] = np.concatenate((windows["zh"][:half], windows["en"][:half]))

    thermal_samples: list[dict[str, Any]] = [
        {"after_window": 0, "elapsed_seconds": 0.0, **_thermal_state()}
    ]
    load_started = time.monotonic()
    model = Model(
        model=str(model_path),
        print_progress=False,
        print_realtime=False,
        n_threads=max(1, min(8, os.cpu_count() or 1)),
        language="",
        no_context=True,
        no_timestamps=True,
        suppress_nst=True,
    )
    load_seconds = time.monotonic() - load_started

    accuracy: dict[str, dict[str, Any]] = {}
    for language, pcm in fixtures.items():
        started = time.monotonic()
        transcript = _segments_text(model.transcribe(pcm))
        elapsed = time.monotonic() - started
        if language == "zh":
            reference_tokens = _han_characters(ZH_TEXT)
            hypothesis_tokens = _han_characters(transcript)
            metric = "cer_han"
        else:
            reference_tokens = _english_words(EN_TEXT)
            hypothesis_tokens = _english_words(transcript)
            metric = "wer"
        accuracy[language] = {
            "audio_seconds": pcm.size / SAMPLE_RATE,
            "decode_seconds": elapsed,
            "metric": metric,
            "error_rate": error_rate(reference_tokens, hypothesis_tokens),
            "normalization": (
                "NFKC only; traditional/simplified differences count as errors"
                if language == "zh"
                else "case-folded words without punctuation"
            ),
            "transcript": transcript,
        }

    mixed_transcript = _segments_text(model.transcribe(windows["mixed"]))
    accuracy["mixed"] = {
        "audio_seconds": window_seconds,
        "transcript": mixed_transcript,
        "contains_han": bool(_han_characters(mixed_transcript)),
        "contains_english": bool(_english_words(mixed_transcript)),
    }
    segmented_started = time.monotonic()
    segmented_transcript = " ".join(
        (
            _segments_text(model.transcribe(windows["mixed"][:half])),
            _segments_text(model.transcribe(windows["mixed"][half:])),
        )
    ).strip()
    accuracy["mixed_known_boundary"] = {
        "audio_seconds": window_seconds,
        "decode_seconds": time.monotonic() - segmented_started,
        "transcript": segmented_transcript,
        "contains_han": bool(_han_characters(segmented_transcript)),
        "contains_english": bool(_english_words(segmented_transcript)),
        "note": "oracle split at the known language boundary; this measures the benefit, not VAD accuracy",
    }

    window_count = math.ceil(duration_seconds / window_seconds)
    names = ("zh", "en", "mixed")
    timings: list[float] = []
    throughput_started = time.monotonic()
    sample_after = {max(1, round(window_count * fraction)) for fraction in (0.25, 0.5, 0.75, 1.0)}
    for index in range(window_count):
        started = time.monotonic()
        model.transcribe(windows[names[index % len(names)]])
        timings.append(time.monotonic() - started)
        completed_windows = index + 1
        if completed_windows in sample_after:
            thermal_samples.append(
                {
                    "after_window": completed_windows,
                    "elapsed_seconds": time.monotonic() - throughput_started,
                    **_thermal_state(),
                }
            )

    measured_audio = window_count * window_seconds
    total_decode = sum(timings)
    quarter = max(1, window_count // 4)
    first_quarter_rtf = sum(timings[:quarter]) / (len(timings[:quarter]) * window_seconds)
    last_quarter_rtf = sum(timings[-quarter:]) / (len(timings[-quarter:]) * window_seconds)
    result = {
        "model": model_path.name.removeprefix("ggml-").removesuffix(".bin"),
        "model_bytes": model_path.stat().st_size,
        "load_seconds": load_seconds,
        "measured_audio_seconds": measured_audio,
        "window_seconds": window_seconds,
        "window_count": window_count,
        "decode_wall_seconds": total_decode,
        "real_time_factor": total_decode / measured_audio,
        "sustained_load": {
            "first_quarter_real_time_factor": first_quarter_rtf,
            "last_quarter_real_time_factor": last_quarter_rtf,
            "last_to_first_ratio": last_quarter_rtf / first_quarter_rtf,
            "thermal_samples": thermal_samples,
        },
        "decode_seconds": {
            "minimum": min(timings),
            "median": sorted(timings)[len(timings) // 2],
            "maximum": max(timings),
        },
        "backlog": accumulated_backlog(timings, window_seconds),
        "accuracy": accuracy,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _capabilities() -> dict[str, Any]:
    from kiro_crew.stt.capabilities import detect

    detected = detect()
    return {
        "backend": detected.backend,
        "accelerated": detected.accelerated,
        "encoder_only": detected.encoder_only,
        "detail": detected.detail,
        "raw": detected.raw,
    }


def _safe_hardware() -> dict[str, str]:
    result = {
        "architecture": platform.machine(),
        "macos": platform.mac_ver()[0],
    }
    completed = subprocess.run(
        ["/usr/sbin/system_profiler", "SPHardwareDataType"],
        check=False,
        capture_output=True,
        text=True,
    )
    for line in completed.stdout.splitlines():
        label, separator, value = line.strip().partition(":")
        if separator and label in {"Model Name", "Model Identifier", "Chip", "Memory"}:
            result[label.lower().replace(" ", "_")] = value.strip()
    return result


def _run_model(
    model_name: str,
    model_path: Path,
    fixture_dir: Path,
    duration_seconds: float,
    window_seconds: float,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_worker",
        "--model-path",
        str(model_path),
        "--fixture-dir",
        str(fixture_dir),
        "--duration-seconds",
        str(duration_seconds),
        "--window-seconds",
        str(window_seconds),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    native_log = completed.stderr
    if completed.returncode != 0:
        raise RuntimeError(
            f"{model_name} benchmark failed with exit {completed.returncode}:\n{native_log[-4000:]}"
        )
    if not metal_was_used(native_log):
        raise RuntimeError(
            f"{model_name} loaded without native proof that Metal was active; refusing CPU result"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{model_name} worker returned invalid JSON: {completed.stdout[-1000:]}"
        ) from exc
    result["metal_active"] = True
    result["metal_evidence"] = next(
        line.strip() for line in native_log.splitlines() if metal_was_used(line)
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", choices=tuple(MODEL_FILENAMES))
    parser.add_argument("--model-dir", type=Path, default=Path.home() / ".kiro/crew/models/whisper")
    parser.add_argument("--duration-seconds", type=float, default=3600.0)
    parser.add_argument("--window-seconds", type=float, default=30.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--model-path", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-dir", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.duration_seconds <= 0 or args.window_seconds <= 0:
        raise SystemExit("duration and window must both be positive")
    if args._worker:
        if args.model_path is None or args.fixture_dir is None:
            raise SystemExit("worker mode requires model and fixture paths")
        return _worker(
            args.model_path, args.fixture_dir, args.duration_seconds, args.window_seconds
        )

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Phase 0 supports only Apple Silicon macOS")
    capabilities = _capabilities()
    if capabilities["backend"] != "metal":
        raise SystemExit(f"Metal is required; loaded build reports {capabilities['backend']!r}")

    models = args.model or list(DEFAULT_MODELS)
    model_paths: dict[str, Path] = {}
    model_hashes: dict[str, str] = {}
    for name in models:
        path = args.model_dir / MODEL_FILENAMES[name]
        if not path.is_file():
            raise SystemExit(f"missing model: {path}")
        actual = _sha256(path)
        if actual != MODEL_SHA256[name]:
            raise SystemExit(f"SHA-256 mismatch for {path}")
        model_paths[name] = path
        model_hashes[name] = actual

    with tempfile.TemporaryDirectory(prefix="local-meeting-whisper-") as temporary:
        fixture_dir = Path(temporary)
        create_fixtures(fixture_dir)
        results = [
            _run_model(
                name, model_paths[name], fixture_dir, args.duration_seconds, args.window_seconds
            )
            for name in models
        ]

    import _pywhispercpp

    report = {
        "schema_version": 1,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime": {
            "python": sys.version.split()[0],
            "python_executable": str(Path(sys.executable).resolve()),
            "native_binary": str(Path(_pywhispercpp.__file__).resolve()),
            "capabilities": capabilities,
        },
        "host": _safe_hardware(),
        "fixture": {
            "kind": "macOS synthesized bilingual speech",
            "voices": {"zh": "Tingting", "en": "Samantha"},
            "sample_rate_hz": SAMPLE_RATE,
        },
        "requested_audio_seconds_per_model": args.duration_seconds,
        "model_sha256": model_hashes,
        "models": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
