"""Deterministic transcript exports for completed or paused meetings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

SAMPLE_RATE = 16_000
SUPPORTED_EXPORTS = frozenset({"markdown", "text", "json"})


@dataclass(frozen=True)
class ExportDocument:
    body: bytes
    content_type: str
    filename: str


def _timestamp(sample: int) -> str:
    milliseconds = sample * 1000 // SAMPLE_RATE
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _filename(title: str, meeting_id: str, extension: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", title.strip()).strip("-._")
    if not stem:
        stem = f"meeting-{meeting_id[:8]}"
    return f"{stem[:80]}.{extension}"


def export_transcript(
    *, meeting: dict[str, Any], segments: list[dict[str, Any]], format_name: str
) -> ExportDocument:
    if format_name not in SUPPORTED_EXPORTS:
        raise ValueError("export format must be markdown, text or json")
    visible = [segment for segment in segments if str(segment["text"]).strip()]
    title = str(meeting["title"]).strip() or "Untitled meeting"
    meeting_id = str(meeting["id"])

    if format_name == "json":
        payload = {
            "schema_version": 1,
            "meeting": meeting,
            "segments": visible,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        return ExportDocument(
            body=text.encode("utf-8"),
            content_type="application/json; charset=utf-8",
            filename=_filename(title, meeting_id, "json"),
        )

    if format_name == "text":
        text = "\n".join(str(segment["text"]).strip() for segment in visible)
        if text:
            text += "\n"
        return ExportDocument(
            body=text.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            filename=_filename(title, meeting_id, "txt"),
        )

    lines = [
        f"# {title}",
        "",
        f"- Created: {meeting['created_at']}",
        f"- State: {meeting['state']}",
        f"- Meeting ID: `{meeting_id}`",
        "",
        "## Transcript",
        "",
    ]
    lines.extend(
        f"- **[{_timestamp(int(segment['audio_start_sample']))}]** "
        f"{str(segment['text']).strip()}"
        for segment in visible
    )
    if not visible:
        lines.append("_No committed speech._")
    return ExportDocument(
        body=("\n".join(lines) + "\n").encode("utf-8"),
        content_type="text/markdown; charset=utf-8",
        filename=_filename(title, meeting_id, "md"),
    )
