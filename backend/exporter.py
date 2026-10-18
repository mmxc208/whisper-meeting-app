"""Deterministic transcript exports for completed or paused meetings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

SAMPLE_RATE = 16_000
SUPPORTED_EXPORTS = frozenset({"markdown", "text", "json", "notes", "sketch", "combined"})


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
    *,
    meeting: dict[str, Any],
    segments: list[dict[str, Any]],
    format_name: str,
    context: list[dict[str, Any]] | None = None,
) -> ExportDocument:
    if format_name not in {"markdown", "text", "json"}:
        raise ValueError("export format must be markdown, text or json")
    typed_context = context or []
    visible = [segment for segment in segments if str(segment["text"]).strip()]
    title = str(meeting["title"]).strip() or "Untitled meeting"
    meeting_id = str(meeting["id"])

    if format_name == "json":
        payload = {
            "schema_version": 1,
            "meeting": meeting,
            "segments": visible,
            "typed_context": typed_context,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        return ExportDocument(
            body=text.encode("utf-8"),
            content_type="application/json; charset=utf-8",
            filename=_filename(title, meeting_id, "json"),
        )

    if format_name == "text":
        lines = [str(segment["text"]).strip() for segment in visible]
        lines.extend(f"[Typed] {event['text']}" for event in typed_context)
        text = "\n".join(lines)
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
    if typed_context:
        lines.extend(("", "## Typed context", ""))
        lines.extend(
            f"- **[{event['created_at']}] Typed:** {event['text']}" for event in typed_context
        )
    return ExportDocument(
        body=("\n".join(lines) + "\n").encode("utf-8"),
        content_type="text/markdown; charset=utf-8",
        filename=_filename(title, meeting_id, "md"),
    )


def export_agent_artifact(
    *, meeting: dict[str, Any], snapshot: dict[str, Any], format_name: str
) -> ExportDocument:
    """Export sanitized agent artifacts and their durable status."""

    title = str(meeting["title"]).strip() or "Untitled meeting"
    meeting_id = str(meeting["id"])
    outputs = snapshot["outputs"]
    agents = {row["agent_id"]: row for row in snapshot["agents"]}
    note_id = "whisper-meeting-note-taker"
    sketch_id = "whisper-meeting-sketch-artist"
    if format_name == "notes":
        return ExportDocument(
            body=str(outputs.get(note_id, "")).encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
            filename=_filename(f"{title}-notes", meeting_id, "md"),
        )
    if format_name == "sketch":
        return ExportDocument(
            body=str(outputs.get(sketch_id, "")).encode("utf-8"),
            content_type="text/html; charset=utf-8",
            filename=_filename(f"{title}-sketch", meeting_id, "html"),
        )
    if format_name != "combined":
        raise ValueError("agent export format must be notes, sketch or combined")
    note_state = agents.get(note_id, {})
    sketch_state = agents.get(sketch_id, {})
    lines = [
        f"# {title}",
        "",
        f"- Meeting ID: `{meeting_id}`",
        f"- Meeting state: {meeting['state']}",
        (
            "- Note Taker: "
            f"{note_state.get('state', 'unavailable')} "
            f"(revision {note_state.get('output_revision', 0)})"
        ),
        (
            "- Sketch Artist: "
            f"{sketch_state.get('state', 'unavailable')} "
            f"(revision {sketch_state.get('output_revision', 0)})"
        ),
        "",
        "## Notes",
        "",
        str(outputs.get(note_id, "")).strip() or "_Notes unavailable._",
        "",
        "## Sketch (sanitized HTML)",
        "",
        "```html",
        str(outputs.get(sketch_id, "")).strip(),
        "```",
    ]
    return ExportDocument(
        body=("\n".join(lines) + "\n").encode("utf-8"),
        content_type="text/markdown; charset=utf-8",
        filename=_filename(f"{title}-meeting", meeting_id, "md"),
    )
