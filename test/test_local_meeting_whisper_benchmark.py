"""Pure tests for the local Meetings MVP's host benchmark."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_benchmark_module():
    path = Path(__file__).parents[1] / "scripts" / "benchmark_local_meeting_whisper.py"
    name = "_test_local_meeting_whisper_benchmark"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


benchmark = _load_benchmark_module()


def _load_storage_module():
    path = Path(__file__).parents[1] / "scripts" / "benchmark_local_meeting_storage.py"
    name = "_test_local_meeting_storage_benchmark"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


storage = _load_storage_module()


def test_accumulated_backlog_tracks_catch_up_and_peak():
    assert benchmark.accumulated_backlog([35.0, 20.0, 45.0], 30.0) == {
        "final_seconds": 15.0,
        "maximum_seconds": 15.0,
    }


def test_accumulated_backlog_never_goes_negative():
    assert benchmark.accumulated_backlog([1.0, 2.0], 30.0) == {
        "final_seconds": 0.0,
        "maximum_seconds": 0.0,
    }


def test_error_rate_counts_substitution_insertion_and_deletion():
    assert benchmark.error_rate(list("abc"), list("adc")) == 1 / 3
    assert benchmark.error_rate([], ["unexpected"]) == 1.0
    assert benchmark.error_rate([], []) == 0.0


def test_metal_evidence_requires_selected_backend_line():
    linked_only = "MTL : EMBED_LIBRARY = 1 | CPU : NEON = 1"
    selected = "whisper_backend_init_gpu: using MTL0 backend"
    assert benchmark.metal_was_used(linked_only) is False
    assert benchmark.metal_was_used(selected) is True


def test_thermal_state_names_match_macos_process_info_ordinals():
    assert benchmark._THERMAL_STATES == {
        0: "nominal",
        1: "fair",
        2: "serious",
        3: "critical",
    }


def test_storage_budget_covers_multi_hour_backend_stall():
    budget = storage.storage_budget()
    assert budget["chunk_bytes"] == 16_000
    assert budget["chunks_per_hour"] == 7_200
    assert budget["raw_bytes_per_hour"] == 115_200_000
    assert budget["browser_cap_raw_hours"] > 4
    assert budget["backend_cap_raw_hours"] > 18


def test_storage_probe_keeps_every_chunk_when_recognizer_is_stalled(tmp_path):
    result = storage.run_probe(tmp_path, hours=1 / 3600, chunk_seconds=0.5)
    assert result["recognizer_state"] == "deliberately_stalled"
    assert result["capture_completed"] is True
    assert result["chunk_count"] == 2
    assert result["ledger_rows"] == 2
    assert result["spool_files"] == 2
    assert result["contiguous_through_sequence"] == 1
