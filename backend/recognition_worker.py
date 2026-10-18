"""Resident pywhispercpp worker with a small JSON-lines protocol."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.transcript_quality import (
    MIN_AVERAGE_LOGPROB,
    NO_SPEECH_THRESHOLD,
    average_logprob,
    hallucination_reason,
    traditional_to_simplified,
)

SAMPLE_RATE = 16_000


def _load_model(path: Path) -> Any:
    from pywhispercpp.model import Model

    return Model(
        model=str(path),
        print_progress=False,
        print_realtime=False,
        language="auto",
        no_context=True,
        no_timestamps=False,
        suppress_nst=True,
        logprob_thold=MIN_AVERAGE_LOGPROB,
        no_speech_thold=NO_SPEECH_THRESHOLD,
    )


def _read_audio(parts: list[dict[str, Any]]) -> np.ndarray:
    arrays: list[np.ndarray] = []
    for part in parts:
        path = Path(str(part["path"]))
        start = int(part.get("start", 0))
        end_value = part.get("end")
        samples = np.fromfile(path, dtype="<i2")
        end = len(samples) if end_value is None else int(end_value)
        if start < 0 or end < start or end > len(samples):
            raise ValueError("recognition input slice is outside the PCM object")
        arrays.append(samples[start:end].astype(np.float32) / 32768.0)
    if not arrays:
        raise ValueError("recognition request contains no PCM")
    return np.concatenate(arrays)


def _transcribe(model: Any, request: dict[str, Any]) -> dict[str, Any]:
    pcm = _read_audio(request.get("parts", []))
    segments: list[dict[str, Any]] = []
    filtered_segments: list[dict[str, Any]] = []
    detected, _probabilities = model.auto_detect_language(pcm)
    language = str(detected[0])
    for segment in model.transcribe(pcm, language=language, extract_probability=True):
        raw_text = str(getattr(segment, "text", "")).strip()
        text = traditional_to_simplified(raw_text)
        if not text:
            continue
        start = max(0, int(getattr(segment, "t0", 0)) * SAMPLE_RATE // 100)
        end = max(start, int(getattr(segment, "t1", 0)) * SAMPLE_RATE // 100)
        logprob = average_logprob(getattr(segment, "probability", None))
        no_speech_value = getattr(segment, "no_speech_probability", None)
        no_speech = float(no_speech_value) if no_speech_value is not None else None
        reason = hallucination_reason(
            text,
            segment_average_logprob=logprob,
            no_speech_probability=no_speech,
        )
        record = {
            "start": start,
            "end": min(len(pcm), end),
            "text": text,
            "average_logprob": logprob,
            "no_speech_probability": no_speech,
        }
        if reason is None:
            segments.append(record)
        else:
            filtered_segments.append({**record, "text": raw_text, "reason": reason})
    return {
        "id": request.get("id"),
        "ok": True,
        "samples": len(pcm),
        "language": language,
        "segments": segments,
        "filtered_segments": filtered_segments,
    }


def _self_test(model_path: Path) -> int:
    result: dict[str, Any]
    try:
        if traditional_to_simplified("會議") != "会议":
            raise RuntimeError("OpenCC Simplified Chinese conversion failed")
        model = _load_model(model_path)
        list(model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32)))
        result = {"ok": True}
        code = 0
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        code = 1
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return code


def _serve(model_path: Path) -> int:
    model = _load_model(model_path)
    print(json.dumps({"ready": True}, separators=(",", ":")), flush=True)
    for line in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            response = _transcribe(model, request)
        except Exception as exc:
            response = {
                "id": request.get("id") if isinstance(request, dict) else None,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    return _self_test(args.model) if args.self_test else _serve(args.model)


if __name__ == "__main__":
    raise SystemExit(main())
