from pathlib import Path

import pytest

from scripts.probe_meeting_agents import GatewayClient, ProbeError, _prompt, run_probe


def test_probe_refuses_non_loopback_gateway() -> None:
    with pytest.raises(ProbeError, match="loopback"):
        GatewayClient(origin="https://gateway.example", app_name="app", secret="secret")


def test_probe_requires_explicit_live_dispatch_before_reading_secrets(tmp_path: Path) -> None:
    with pytest.raises(ProbeError, match="--execute"):
        run_probe(
            origin="http://127.0.0.1:5476",
            crew_home=tmp_path,
            mode="all",
            timeout=1,
            execute=False,
        )


def test_probe_prompt_names_only_the_assigned_output(tmp_path: Path) -> None:
    output = tmp_path / "agents" / "note.md"
    prompt = _prompt(output=output, kind="note")

    assert prompt.startswith(f"OUTPUT_FILE: {output}\n")
    assert "Alice owns the launch checklist" in prompt
    assert "sketch.html" not in prompt
