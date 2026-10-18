from pathlib import Path
from types import SimpleNamespace

import numpy as np

from backend.recognition_worker import _transcribe


class FakeModel:
    def auto_detect_language(self, pcm: np.ndarray):
        assert len(pcm) == 1600
        return (("zh", np.float32(0.9)), {"zh": np.float32(0.9)})

    def transcribe(self, pcm: np.ndarray, **params):
        assert params == {"language": "zh", "extract_probability": True}
        return [
            SimpleNamespace(t0=0, t1=5, text="軟體會議", probability=0.9),
            SimpleNamespace(t0=5, t1=10, text="Thank you.", probability=0.9),
        ]


def test_worker_records_language_simplifies_and_preserves_filtered_text(tmp_path: Path) -> None:
    pcm = tmp_path / "audio.pcm"
    np.zeros(1600, dtype="<i2").tofile(pcm)

    result = _transcribe(FakeModel(), {"id": "job", "parts": [{"path": str(pcm)}]})

    assert result["language"] == "zh"
    assert [segment["text"] for segment in result["segments"]] == ["软体会议"]
    assert result["filtered_segments"][0]["text"] == "Thank you."
    assert result["filtered_segments"][0]["reason"] == "known_hallucination"
