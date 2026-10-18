#!/usr/bin/env python3
"""Create, inspect, and remove the synthetic coordinator restart fixture."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from backend.agent_ledger import AGENT_IDS, AgentLedger
from backend.store import MeetingStore, _utc_now

OWNER_ID = "phase-a-restart-probe"
TITLE = "Phase A coordinator restart probe"


def _rows(database: Path, meeting_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, agent_id, kind, state, execution_ref, attempt_count, last_error
            FROM meeting_agent_dispatches
            WHERE meeting_id = ?
            ORDER BY rowid
            """,
            (meeting_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def setup(data_dir: Path) -> dict[str, Any]:
    store = MeetingStore(data_dir, min_free_bytes=0)
    store.initialize()
    meeting = store.create_meeting(title=TITLE, owner_id=OWNER_ID)
    meeting_id = str(meeting["id"])
    now = _utc_now()
    with store._connect() as connection:
        connection.execute(
            "UPDATE meetings SET state = 'paused', updated_at = ? WHERE id = ?",
            (now, meeting_id),
        )
        connection.execute(
            """
            INSERT INTO transcript_segments (
                meeting_id, epoch, audio_start_sample, audio_end_sample,
                language, text, coverage_kind, committed_at
            ) VALUES (?, 1, 0, 16000, 'mixed', ?, 'speech', ?)
            """,
            (
                meeting_id,
                "Restart qualification: Alice owns the checklist. 李明负责记录风险。",
                now,
            ),
        )
        connection.commit()
    AgentLedger(data_dir).initialize_outputs(meeting_id=meeting_id, owner_id=OWNER_ID)
    return {"meeting_id": meeting_id, "owner_id": OWNER_ID, "title": TITLE}


def wait_for_accepted(data_dir: Path, meeting_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows(data_dir / "meetings.sqlite3", meeting_id)
        if (
            len(rows) == len(AGENT_IDS)
            and {row["agent_id"] for row in rows} == set(AGENT_IDS)
            and all(row["state"] == "accepted" and row["execution_ref"] for row in rows)
        ):
            return {"meeting_id": meeting_id, "dispatches": rows}
        time.sleep(0.2)
    return {
        "meeting_id": meeting_id,
        "dispatches": _rows(data_dir / "meetings.sqlite3", meeting_id),
    }


def status(data_dir: Path, meeting_id: str) -> dict[str, Any]:
    return {
        "meeting_id": meeting_id,
        "dispatches": _rows(data_dir / "meetings.sqlite3", meeting_id),
    }


def wait_for_terminal(data_dir: Path, meeting_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = status(data_dir, meeting_id)
        rows = result["dispatches"]
        if len(rows) == len(AGENT_IDS) and all(
            row["state"] in {"complete", "failed"} for row in rows
        ):
            return result
        time.sleep(0.5)
    return status(data_dir, meeting_id)


def cleanup(data_dir: Path, meeting_id: str) -> dict[str, Any]:
    rows = _rows(data_dir / "meetings.sqlite3", meeting_id)
    if any(row["state"] in {"pending", "running", "accepted"} for row in rows):
        raise RuntimeError("refusing to remove a probe with unfinished dispatches")
    MeetingStore(data_dir, min_free_bytes=0).delete_meeting(
        meeting_id=meeting_id,
        owner_id=OWNER_ID,
    )
    return {"meeting_id": meeting_id, "deleted": True, "dispatches": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("setup", "wait-accepted", "wait-terminal", "status", "cleanup"),
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--meeting-id")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.command == "setup":
        result = setup(args.data_dir)
    else:
        if not args.meeting_id:
            parser.error("--meeting-id is required")
        if args.command == "wait-accepted":
            result = wait_for_accepted(args.data_dir, args.meeting_id, args.timeout)
        elif args.command == "wait-terminal":
            result = wait_for_terminal(args.data_dir, args.meeting_id, args.timeout)
        elif args.command == "status":
            result = status(args.data_dir, args.meeting_id)
        else:
            result = cleanup(args.data_dir, args.meeting_id)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
