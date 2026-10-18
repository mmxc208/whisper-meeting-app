import json

from backend.exporter import export_agent_artifact, export_transcript


def _meeting() -> dict[str, str]:
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "title": "中英 weekly / review",
        "state": "ended",
        "created_at": "2026-10-17T12:00:00.000+00:00",
        "updated_at": "2026-10-17T12:01:00.000+00:00",
        "last_error": "",
    }


def _segments() -> list[dict[str, object]]:
    return [
        {
            "id": 1,
            "epoch": 1,
            "audio_start_sample": 0,
            "audio_end_sample": 16_000,
            "language": "zh",
            "text": "你好 hello",
            "committed_at": "2026-10-17T12:01:00.000+00:00",
        },
        {
            "id": 2,
            "epoch": 1,
            "audio_start_sample": 16_000,
            "audio_end_sample": 32_000,
            "language": "",
            "text": "",
            "committed_at": "2026-10-17T12:01:00.000+00:00",
        },
    ]


def test_markdown_export_has_timestamp_and_safe_filename() -> None:
    document = export_transcript(meeting=_meeting(), segments=_segments(), format_name="markdown")

    text = document.body.decode()
    assert document.filename == "weekly-review.md"
    assert document.content_type == "text/markdown; charset=utf-8"
    assert "# 中英 weekly / review" in text
    assert "**[00:00:00.000]** 你好 hello" in text


def test_text_export_omits_empty_coverage_rows() -> None:
    document = export_transcript(meeting=_meeting(), segments=_segments(), format_name="text")

    assert document.body.decode() == "你好 hello\n"
    assert document.filename.endswith(".txt")


def test_json_export_is_versioned_and_preserves_unicode() -> None:
    document = export_transcript(meeting=_meeting(), segments=_segments(), format_name="json")
    payload = json.loads(document.body)

    assert payload["schema_version"] == 1
    assert payload["meeting"]["title"] == "中英 weekly / review"
    assert [segment["text"] for segment in payload["segments"]] == ["你好 hello"]


def test_transcript_export_preserves_typed_context_provenance() -> None:
    document = export_transcript(
        meeting=_meeting(),
        segments=_segments(),
        format_name="markdown",
        context=[
            {
                "created_at": "2026-10-17T12:00:30.000+00:00",
                "text": "The correct term is KiroCrew.",
            }
        ],
    )

    assert "## Typed context" in document.body.decode()
    assert "**[2026-10-17T12:00:30.000+00:00] Typed:**" in document.body.decode()


def test_agent_artifact_exports_include_status_and_revision() -> None:
    snapshot = {
        "agents": [
            {
                "agent_id": "whisper-meeting-note-taker",
                "state": "complete",
                "output_revision": 3,
            },
            {
                "agent_id": "whisper-meeting-sketch-artist",
                "state": "failed",
                "output_revision": 1,
            },
        ],
        "outputs": {
            "whisper-meeting-note-taker": "# Notes\n\nDecision recorded.\n",
            "whisper-meeting-sketch-artist": "<!doctype html><p>Safe sketch</p>",
        },
    }

    notes = export_agent_artifact(meeting=_meeting(), snapshot=snapshot, format_name="notes")
    sketch = export_agent_artifact(meeting=_meeting(), snapshot=snapshot, format_name="sketch")
    combined = export_agent_artifact(meeting=_meeting(), snapshot=snapshot, format_name="combined")

    assert notes.body.decode().startswith("# Notes")
    assert notes.filename.endswith("-notes.md")
    assert sketch.content_type == "text/html; charset=utf-8"
    assert sketch.filename.endswith("-sketch.html")
    combined_text = combined.body.decode()
    assert "Note Taker: complete (revision 3)" in combined_text
    assert "Sketch Artist: failed (revision 1)" in combined_text
