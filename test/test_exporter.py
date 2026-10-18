import json

from backend.exporter import export_transcript


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
