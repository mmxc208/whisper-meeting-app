"""Out-of-process recognition scheduler over the durable chunk ledger."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Protocol

from backend.model import ModelManager, app_dependency_environment
from backend.segmenter import choose_segment
from backend.store import MeetingStore

WORKER_RESPONSE_TIMEOUT_SECONDS = 180.0
OVERLAP_SAMPLES = 4_000
RETRY_BACKOFF_SECONDS = (1.0, 5.0, 30.0, 60.0)


class Recognizer(Protocol):
    def transcribe(self, parts: list[dict[str, Any]]) -> dict[str, Any]: ...

    def close(self) -> None: ...


class WhisperWorker:
    """Keep the native model resident in a child process, never the HTTP server."""

    def __init__(self, model_path: Path, log_path: Path, data_dir: Path) -> None:
        self.model_path = model_path
        self.log_path = log_path
        self.data_dir = data_dir
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._stderr: Any = None

    def transcribe(self, parts: list[dict[str, Any]]) -> dict[str, Any]:
        self._ensure_started()
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("recognition worker is unavailable")
        request_id = uuid.uuid4().hex
        process.stdin.write(
            json.dumps({"id": request_id, "parts": parts}, separators=(",", ":")) + "\n"
        )
        process.stdin.flush()
        try:
            line = self._responses.get(timeout=WORKER_RESPONSE_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            self.close()
            raise RuntimeError("recognition worker timed out") from exc
        if line is None:
            code = process.poll()
            self.close()
            raise RuntimeError(f"recognition worker exited unexpectedly ({code})")
        response = json.loads(line)
        if response.get("id") != request_id:
            self.close()
            raise RuntimeError("recognition worker response id did not match")
        if response.get("ok") is not True:
            raise RuntimeError(str(response.get("error") or "recognition failed"))
        return response

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        self._responses = queue.Queue()

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr = self.log_path.open("a", encoding="utf-8")
        worker = Path(__file__).with_name("recognition_worker.py")
        process = subprocess.Popen(
            [sys.executable, str(worker), "--model", str(self.model_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            bufsize=1,
            env=app_dependency_environment(self.data_dir),
        )
        self._process = process
        responses: queue.Queue[str | None] = queue.Queue()
        self._responses = responses
        reader = threading.Thread(
            target=self._read_responses,
            args=(process, responses),
            name="whisper-meeting-recognizer-output",
            daemon=True,
        )
        reader.start()
        try:
            line = self._responses.get(timeout=WORKER_RESPONSE_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            self.close()
            raise RuntimeError("recognition worker did not become ready") from exc
        if line is None or json.loads(line).get("ready") is not True:
            self.close()
            raise RuntimeError("recognition worker failed during model load")

    @staticmethod
    def _read_responses(process: subprocess.Popen[str], responses: queue.Queue[str | None]) -> None:
        if process.stdout is None:
            responses.put(None)
            return
        for line in process.stdout:
            responses.put(line)
        responses.put(None)


class RecognitionScheduler:
    """Claim pause-aligned work and commit final text in source-audio order."""

    def __init__(
        self,
        store: MeetingStore,
        models: ModelManager,
        *,
        recognizer: Recognizer | None = None,
        retry_backoff_seconds: tuple[float, ...] = RETRY_BACKOFF_SECONDS,
    ) -> None:
        self.store = store
        self.models = models
        self._recognizer = recognizer
        self._owns_recognizer = recognizer is None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run_failed = False
        self._retry_backoff_seconds = retry_backoff_seconds

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="whisper-meeting-recognition-scheduler",
            daemon=True,
        )
        self._thread.start()
        self.notify()

    def notify(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._recognizer is not None:
            self._recognizer.close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._recognizer = None

    def run_once(self) -> bool:
        self._last_run_failed = False
        if not self.models.ready():
            return False
        rows = self.store.pending_recognition_group()
        force = any(bool(row.get("force_decode")) for row in rows) or (
            bool(rows)
            and self.store.force_recognition_tail(
                meeting_id=str(rows[0]["meeting_id"]), epoch=int(rows[0]["epoch"])
            )
        )
        window = choose_segment(rows, self.store.data_dir, force=force)
        if window is None:
            return False
        job = self.store.claim_recognition(
            chunks=window.chunks,
            audio_start_sample=window.audio_start_sample,
            audio_end_sample=window.audio_end_sample,
        )
        if job is None:
            return True
        try:
            filtered_segments: list[dict[str, Any]] = []
            parts, recognition_base = self._recognition_parts(job, window.chunks)
            response = self._worker().transcribe(parts)
            segments = self._absolute_segments(job, response.get("segments", []), recognition_base)
            filtered_segments = self._absolute_filtered_segments(
                job, response.get("filtered_segments", []), recognition_base
            )
            language = str(response.get("language", ""))
            self.store.complete_recognition(
                job=job,
                segments=segments,
                language=language,
                coverage_kind="empty_decode",
                filtered_segments=filtered_segments,
            )
            return True
        except Exception as exc:
            self._last_run_failed = True
            self.store.interrupt_recognition(job=job, error=f"{type(exc).__name__}: {exc}")
            if self._recognizer is not None:
                self._recognizer.close()
                if self._owns_recognizer:
                    self._recognizer = None
            return False

    def _worker(self) -> Recognizer:
        if self._recognizer is None:
            self._recognizer = WhisperWorker(
                self.models.verify_for_load(),
                self.store.data_dir / "logs" / "recognizer.log",
                self.store.data_dir,
            )
        return self._recognizer

    @staticmethod
    def _absolute_segments(
        job: dict[str, Any],
        segments: list[dict[str, Any]],
        recognition_base: int,
    ) -> list[dict[str, Any]]:
        start = int(job["audio_start_sample"])
        end = int(job["audio_end_sample"])
        result: list[dict[str, Any]] = []
        for segment in segments:
            raw_start = recognition_base + int(segment["start"])
            raw_end = recognition_base + int(segment["end"])
            if (raw_start + raw_end) // 2 < start:
                continue
            absolute_start = max(start, raw_start)
            absolute_end = min(end, raw_end)
            text = str(segment.get("text", "")).strip()
            if text and absolute_end > absolute_start:
                result.append({"start": absolute_start, "end": absolute_end, "text": text})
        return result

    @staticmethod
    def _absolute_filtered_segments(
        job: dict[str, Any],
        segments: list[dict[str, Any]],
        recognition_base: int,
    ) -> list[dict[str, Any]]:
        start = int(job["audio_start_sample"])
        end = int(job["audio_end_sample"])
        result: list[dict[str, Any]] = []
        for source in segments:
            raw_start = recognition_base + int(source["start"])
            raw_end = recognition_base + int(source["end"])
            if (raw_start + raw_end) // 2 < start:
                continue
            absolute_start = max(start, raw_start)
            absolute_end = min(end, raw_end)
            if absolute_end <= absolute_start:
                continue
            result.append(
                {
                    "start": absolute_start,
                    "end": absolute_end,
                    "text": str(source.get("text", "")).strip(),
                    "reason": str(source.get("reason", "filtered")),
                    "average_logprob": source.get("average_logprob"),
                    "no_speech_probability": source.get("no_speech_probability"),
                }
            )
        return result

    def _recognition_parts(
        self, job: dict[str, Any], chunks: tuple[dict[str, Any], ...]
    ) -> tuple[list[dict[str, Any]], int]:
        parts: list[dict[str, Any]] = []
        recognition_base = int(job["audio_start_sample"])
        previous = self.store.previous_transcribed_chunk(
            meeting_id=job["meeting_id"],
            epoch=job["epoch"],
            before_sequence=job["first_sequence"],
        )
        if previous is not None:
            sample_count = int(previous["sample_count"])
            overlap = min(OVERLAP_SAMPLES, sample_count)
            parts.append(
                {
                    "path": str(self.store.data_dir / previous["spool_path"]),
                    "start": sample_count - overlap,
                    "end": sample_count,
                }
            )
            recognition_base -= overlap
        parts.extend({"path": str(self.store.data_dir / chunk["spool_path"])} for chunk in chunks)
        return parts, recognition_base

    def _run(self) -> None:
        failures = 0
        retry_after: float | None = None
        while not self._stop.is_set():
            self._wake.wait(timeout=retry_after)
            self._wake.clear()
            if self._stop.is_set():
                break
            while not self._stop.is_set():
                if self.run_once():
                    failures = 0
                    retry_after = None
                    continue
                if self._last_run_failed:
                    failures += 1
                    retry_after = self._retry_backoff_seconds[
                        min(failures - 1, len(self._retry_backoff_seconds) - 1)
                    ]
                else:
                    failures = 0
                    retry_after = None
                break
