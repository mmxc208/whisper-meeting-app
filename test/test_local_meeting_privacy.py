from __future__ import annotations

import hashlib
import importlib
import socket
import sys
from array import array
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.chunks import ChunkMetadata  # noqa: E402
from backend.recognition import RecognitionScheduler  # noqa: E402
from backend.store import MeetingStore  # noqa: E402


class _ReadyModels:
    def ready(self) -> bool:
        return True


class _Recognizer:
    def transcribe(self, parts: list[dict[str, Any]]) -> dict[str, Any]:
        assert all(Path(str(part["path"])).is_file() for part in parts)
        return {
            "language": "en",
            "segments": [{"start": 0, "end": 32_000, "text": "local only"}],
        }

    def close(self) -> None:
        return


def test_capture_and_recognition_open_no_network_connection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    attempted: list[object] = []

    def record_connect(sock: socket.socket, address: object) -> None:
        attempted.append(address)
        raise AssertionError("capture and recognition must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", record_connect)
    urllib_request = importlib.import_module("urllib.request")

    def record_urlopen(*args: object, **kwargs: object) -> None:
        attempted.append(args[0] if args else kwargs)
        raise AssertionError("capture and recognition must not open a URL")

    monkeypatch.setattr(urllib_request, "urlopen", record_urlopen)

    store = MeetingStore(tmp_path / "data", min_free_bytes=0)
    store.initialize()
    meeting = store.create_meeting(title="Privacy probe", owner_id="owner")
    meeting_id = str(meeting["id"])
    store.start_capture(meeting_id=meeting_id, owner_id="owner")
    samples = array("h", [8_000] * 32_000).tobytes()
    store.store_chunk(
        metadata=ChunkMetadata(
            meeting_id=meeting_id,
            epoch=1,
            sequence=0,
            sample_start=0,
            sample_count=32_000,
            sample_rate=16_000,
            channels=1,
            encoding="pcm_s16le",
            sha256=hashlib.sha256(samples).hexdigest(),
        ),
        body=samples,
        owner_id="owner",
    )
    store.request_drain(
        meeting_id=meeting_id,
        owner_id="owner",
        kind="end",
        epoch=1,
        cut_sequence=0,
        cut_sample=32_000,
    )
    scheduler = RecognitionScheduler(
        store,
        _ReadyModels(),  # type: ignore[arg-type]
        recognizer=_Recognizer(),
    )

    assert scheduler.run_once() is True
    assert attempted == []
    assert store.get_meeting(meeting_id=meeting_id, owner_id="owner")["state"] == "ended"


def test_runtime_network_surface_is_pinned_and_loopback_only() -> None:
    backend_sources = {
        path.relative_to(APP_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in (APP_ROOT / "backend").glob("*.py")
    }
    external_urls = [
        (name, line.strip())
        for name, source in backend_sources.items()
        for line in source.splitlines()
        if "https://" in line or "http://" in line
    ]
    ui_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (APP_ROOT / "ui" / "src").glob("*.ts*")
    )

    assert external_urls == [
        (
            "backend/model.py",
            'MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/" "ggml-large-v3-turbo.bin"',
        )
    ]
    assert "http://" not in ui_source
    assert "https://" not in ui_source
    assert '        ("127.0.0.1", port),' in backend_sources["backend/server.py"]
