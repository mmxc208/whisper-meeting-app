from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_soak_module():
    path = Path(__file__).parents[1] / "scripts" / "soak_local_meeting.py"
    spec = importlib.util.spec_from_file_location("local_meeting_soak", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_accelerated_soak_preserves_exact_coverage_and_pcm(tmp_path: Path) -> None:
    result = _load_soak_module().run_soak(
        data_dir=tmp_path / "data",
        hours=1 / 60,
        chunk_seconds=30,
    )

    assert result["chunks"] == 2
    assert result["samples"] == 960_000
    assert result["bytes_before_drain"] == 1_920_000
    assert result["covered_through_sample"] == result["samples"]
    assert result["terminal_state"] == "ended"
    assert result["pending_chunks"] == 0
    assert result["spool_bytes_after_drain"] == result["bytes_before_drain"]
    assert result["spool_objects_after_drain"] == result["chunks"]
