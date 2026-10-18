"""Durable downstream ledger for meeting-agent work."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.sketch_sanitizer import sanitize_sketch
from backend.store import InvalidMeeting, MeetingNotFound, _utc_now

NOTE_AGENT = "whisper-meeting-note-taker"
SKETCH_AGENT = "whisper-meeting-sketch-artist"
AGENT_IDS = (NOTE_AGENT, SKETCH_AGENT)
MAX_CONTEXT_CHARS = 4_000
MAX_BATCH_CHARS = 12_000


class DispatchNotFound(RuntimeError):
    """The requested agent dispatch does not exist."""


class AgentLedger:
    """Persist agent inputs and delivery state independently of capture state."""

    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], str] = _utc_now,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.database_path = data_dir / "meetings.sqlite3"
        self._clock = clock
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def initialize_outputs(self, *, meeting_id: str, owner_id: str) -> dict[str, str]:
        self._require_meeting(meeting_id=meeting_id, owner_id=owner_id)
        output_dir = self.data_dir / "sessions" / meeting_id / "agents"
        output_dir.mkdir(parents=True, exist_ok=True)
        note = output_dir / "note-taker.md"
        sketch = output_dir / "sketch-artist.html"
        if not note.exists():
            note.write_text("# Meeting notes\n\n_Notes pending._\n", encoding="utf-8")
        if not sketch.exists():
            sketch.write_text(
                '<!doctype html><html><head><meta charset="utf-8"><title>Meeting sketch'
                "</title></head><body><p>Sketch pending.</p></body></html>\n",
                encoding="utf-8",
            )
        now = self._clock()
        with self._connect() as connection:
            for agent_id in AGENT_IDS:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO meeting_agent_state (
                        meeting_id, agent_id, state, updated_at
                    ) VALUES (?, ?, 'pending', ?)
                    """,
                    (meeting_id, agent_id, now),
                )
            connection.commit()
        return {NOTE_AGENT: str(note), SKETCH_AGENT: str(sketch)}

    def output_path(self, *, meeting_id: str, agent_id: str) -> Path:
        self._require_agent(agent_id)
        filename = "note-taker.md" if agent_id == NOTE_AGENT else "sketch-artist.html"
        return self.data_dir / "sessions" / meeting_id / "agents" / filename

    def append_context(self, *, meeting_id: str, owner_id: str, text: str) -> dict[str, Any]:
        normalized = text.strip()
        if not normalized:
            raise InvalidMeeting("meeting context cannot be empty")
        if len(normalized) > MAX_CONTEXT_CHARS:
            raise InvalidMeeting(f"meeting context must be at most {MAX_CONTEXT_CHARS} characters")
        meeting = self._require_meeting(meeting_id=meeting_id, owner_id=owner_id)
        if meeting["state"] != "recording":
            raise InvalidMeeting("meeting context is available only while a meeting is active")
        event_id = self._id_factory()
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO meeting_context_events (id, meeting_id, text, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, meeting_id, normalized, now),
            )
            for agent_id in AGENT_IDS:
                self._insert_dispatch(
                    connection,
                    meeting_id=meeting_id,
                    agent_id=agent_id,
                    kind="chat",
                    payload=f"[typed context]\n{normalized}",
                    now=now,
                    context_event_id=event_id,
                )
                self._ensure_state(connection, meeting_id=meeting_id, agent_id=agent_id, now=now)
            connection.commit()
        return {"id": event_id, "text": normalized, "created_at": now}

    def queue_speech(self, *, meeting_id: str, owner_id: str) -> list[dict[str, Any]]:
        self._require_meeting(meeting_id=meeting_id, owner_id=owner_id)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            allocated_through = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(last_segment_id), 0)
                    FROM meeting_agent_dispatches
                    WHERE meeting_id = ? AND kind = 'speech'
                    """,
                    (meeting_id,),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT id, epoch, audio_start_sample, language, text
                FROM transcript_segments
                WHERE meeting_id = ? AND coverage_kind = 'speech' AND id > ?
                ORDER BY id
                """,
                (meeting_id, allocated_through),
            ).fetchall()
            selected: list[sqlite3.Row] = []
            size = 0
            for row in rows:
                line = self._speech_line(row)
                if selected and size + len(line) > MAX_BATCH_CHARS:
                    break
                selected.append(row)
                size += len(line)
            if not selected:
                connection.rollback()
                return []
            payload = "[finalized speech]\n" + "\n\n".join(
                self._speech_line(row) for row in selected
            )
            first_segment_id = int(selected[0]["id"])
            last_segment_id = int(selected[-1]["id"])
            dispatches = []
            for agent_id in AGENT_IDS:
                dispatches.append(
                    dict(
                        self._insert_dispatch(
                            connection,
                            meeting_id=meeting_id,
                            agent_id=agent_id,
                            kind="speech",
                            payload=payload,
                            now=now,
                            first_segment_id=first_segment_id,
                            last_segment_id=last_segment_id,
                        )
                    )
                )
                self._ensure_state(connection, meeting_id=meeting_id, agent_id=agent_id, now=now)
            connection.commit()
        return dispatches

    def queue_finalize(self, *, meeting_id: str, owner_id: str) -> list[dict[str, Any]]:
        meeting = self._require_meeting(meeting_id=meeting_id, owner_id=owner_id)
        if meeting["state"] != "ended":
            raise InvalidMeeting("agent finalization requires an ended meeting")
        now = self._clock()
        dispatches = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for agent_id in AGENT_IDS:
                existing = connection.execute(
                    """
                    SELECT * FROM meeting_agent_dispatches
                    WHERE meeting_id = ? AND agent_id = ? AND kind = 'finalize'
                    """,
                    (meeting_id, agent_id),
                ).fetchone()
                if existing is None:
                    existing = self._insert_dispatch(
                        connection,
                        meeting_id=meeting_id,
                        agent_id=agent_id,
                        kind="finalize",
                        payload=(
                            "[finalize]\nThe meeting has ended. Read the current output and make "
                            "one final coherent pass. Do not invent missing details."
                        ),
                        now=now,
                    )
                self._ensure_state(connection, meeting_id=meeting_id, agent_id=agent_id, now=now)
                dispatches.append(dict(existing))
            connection.commit()
        return dispatches

    def claim_next(self, *, agent_id: str) -> dict[str, Any] | None:
        self._require_agent(agent_id)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT candidate.* FROM meeting_agent_dispatches AS candidate
                WHERE candidate.agent_id = ? AND candidate.state = 'pending'
                  AND NOT EXISTS (
                      SELECT 1 FROM meeting_agent_dispatches AS blocker
                      WHERE blocker.meeting_id = candidate.meeting_id
                        AND blocker.agent_id = candidate.agent_id
                        AND blocker.rowid < candidate.rowid
                        AND blocker.state != 'complete'
                  )
                ORDER BY candidate.rowid LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE meeting_agent_dispatches
                SET state = 'running', attempt_count = attempt_count + 1,
                    updated_at = ?, last_error = ''
                WHERE id = ? AND state = 'pending'
                """,
                (now, row["id"]),
            )
            state = "finalizing" if row["kind"] == "finalize" else "working"
            self._update_agent_state(
                connection,
                meeting_id=row["meeting_id"],
                agent_id=agent_id,
                state=state,
                now=now,
                last_error="",
            )
            claimed = connection.execute(
                "SELECT * FROM meeting_agent_dispatches WHERE id = ?", (row["id"],)
            ).fetchone()
            connection.commit()
        return dict(claimed) if claimed is not None else None

    def inflight(self, *, agent_id: str) -> dict[str, Any] | None:
        self._require_agent(agent_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM meeting_agent_dispatches
                WHERE agent_id = ? AND state IN ('running', 'accepted')
                ORDER BY created_at, id LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def meetings_for_allocation(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT meeting.id, meeting.owner_id, meeting.state
                FROM meetings AS meeting
                WHERE meeting.state IN ('recording', 'paused', 'draining', 'ended')
                  AND EXISTS (
                      SELECT 1 FROM meeting_agent_state AS agent_state
                      WHERE agent_state.meeting_id = meeting.id
                  )
                ORDER BY created_at, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_accepted(self, *, dispatch_id: str, execution_ref: str) -> None:
        self._transition(dispatch_id, state="accepted", execution_ref=execution_ref)

    def complete(self, *, dispatch_id: str) -> None:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._dispatch(connection, dispatch_id)
            connection.execute(
                """
                UPDATE meeting_agent_dispatches
                SET state = 'complete', updated_at = ?, last_error = ''
                WHERE id = ?
                """,
                (now, dispatch_id),
            )
            pending = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM meeting_agent_dispatches
                    WHERE meeting_id = ? AND agent_id = ?
                      AND state IN ('pending', 'accepted', 'running')
                    """,
                    (row["meeting_id"], row["agent_id"]),
                ).fetchone()[0]
            )
            next_state = (
                "complete" if row["kind"] == "finalize" else ("working" if pending else "caught_up")
            )
            self._update_agent_state(
                connection,
                meeting_id=row["meeting_id"],
                agent_id=row["agent_id"],
                state=next_state,
                now=now,
                delivered_through_segment_id=int(row["last_segment_id"] or 0),
                increment_revision=True,
                last_error="",
            )
            connection.commit()

    def fail(self, *, dispatch_id: str, error: str) -> None:
        now = self._clock()
        message = error[:1_000]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._dispatch(connection, dispatch_id)
            connection.execute(
                """
                UPDATE meeting_agent_dispatches
                SET state = 'failed', updated_at = ?, last_error = ?
                WHERE id = ?
                """,
                (now, message, dispatch_id),
            )
            self._update_agent_state(
                connection,
                meeting_id=row["meeting_id"],
                agent_id=row["agent_id"],
                state="failed",
                now=now,
                last_error=message,
            )
            connection.commit()

    def retry(self, *, dispatch_id: str) -> None:
        self._transition(dispatch_id, state="pending", execution_ref="", allow_failed_only=True)

    def retry_failed(self, *, meeting_id: str, owner_id: str, agent_id: str) -> int:
        """Return every failed dispatch for one meeting agent to the durable queue."""

        self._require_agent(agent_id)
        self._require_meeting(meeting_id=meeting_id, owner_id=owner_id)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE meeting_agent_dispatches
                SET state = 'pending', execution_ref = '', updated_at = ?, last_error = ''
                WHERE meeting_id = ? AND agent_id = ? AND state = 'failed'
                """,
                (now, meeting_id, agent_id),
            )
            count = int(cursor.rowcount)
            if count:
                self._update_agent_state(
                    connection,
                    meeting_id=meeting_id,
                    agent_id=agent_id,
                    state="pending",
                    now=now,
                    last_error="",
                )
            connection.commit()
        return count

    def reconcile_missing_task(self, *, dispatch_id: str) -> None:
        """Return a gateway-confirmed missing dispatch to the durable queue."""

        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._dispatch(connection, dispatch_id)
            preaccept = row["state"] == "running" and not row["execution_ref"]
            lost_accepted = row["state"] == "accepted" and bool(row["execution_ref"])
            if not (preaccept or lost_accepted):
                connection.rollback()
                raise InvalidMeeting("only missing running or accepted work can be reconciled")
            connection.execute(
                """
                UPDATE meeting_agent_dispatches
                SET state = 'pending', execution_ref = '', updated_at = ?, last_error = ''
                WHERE id = ?
                """,
                (now, dispatch_id),
            )
            self._update_agent_state(
                connection,
                meeting_id=row["meeting_id"],
                agent_id=row["agent_id"],
                state="pending",
                now=now,
                last_error="",
            )
            connection.commit()

    def set_unavailable(self, *, agent_id: str, error: str) -> None:
        self._require_agent(agent_id)
        now = self._clock()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT meeting_id FROM meeting_agent_dispatches
                WHERE agent_id = ? AND state IN ('pending', 'running', 'accepted')
                """,
                (agent_id,),
            ).fetchall()
            for row in rows:
                self._update_agent_state(
                    connection,
                    meeting_id=row["meeting_id"],
                    agent_id=agent_id,
                    state="unavailable",
                    now=now,
                    last_error=error[:1_000],
                )
            connection.commit()

    def snapshot(self, *, meeting_id: str, owner_id: str) -> dict[str, Any]:
        self._require_meeting(meeting_id=meeting_id, owner_id=owner_id)
        with self._connect() as connection:
            states = connection.execute(
                """
                SELECT * FROM meeting_agent_state
                WHERE meeting_id = ? ORDER BY agent_id
                """,
                (meeting_id,),
            ).fetchall()
            context = connection.execute(
                """
                SELECT id, text, created_at FROM meeting_context_events
                WHERE meeting_id = ? ORDER BY created_at, id
                """,
                (meeting_id,),
            ).fetchall()
            dispatch_counts = connection.execute(
                """
                SELECT agent_id, state, COUNT(*) AS count
                FROM meeting_agent_dispatches WHERE meeting_id = ?
                GROUP BY agent_id, state ORDER BY agent_id, state
                """,
                (meeting_id,),
            ).fetchall()
            dispatches = connection.execute(
                """
                SELECT id, agent_id, kind, first_segment_id, last_segment_id,
                       state, attempt_count, last_error, created_at, updated_at
                FROM meeting_agent_dispatches
                WHERE meeting_id = ?
                ORDER BY rowid DESC
                LIMIT 200
                """,
                (meeting_id,),
            ).fetchall()
        return {
            "agents": [dict(row) for row in states],
            "context": [dict(row) for row in context],
            "dispatch_counts": [dict(row) for row in dispatch_counts],
            "dispatches": [dict(row) for row in dispatches],
            "outputs": {
                NOTE_AGENT: self._read_output(meeting_id=meeting_id, agent_id=NOTE_AGENT),
                SKETCH_AGENT: self._read_output(meeting_id=meeting_id, agent_id=SKETCH_AGENT),
            },
        }

    def _read_output(self, *, meeting_id: str, agent_id: str) -> str:
        path = self.output_path(meeting_id=meeting_id, agent_id=agent_id)
        try:
            output = path.read_text(encoding="utf-8")
            return sanitize_sketch(output) if agent_id == SKETCH_AGENT else output
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise InvalidMeeting(f"cannot read {agent_id} output") from exc

    def _transition(
        self,
        dispatch_id: str,
        *,
        state: str,
        execution_ref: str,
        allow_failed_only: bool = False,
    ) -> None:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._dispatch(connection, dispatch_id)
            if allow_failed_only and row["state"] != "failed":
                connection.rollback()
                raise InvalidMeeting("only a failed agent dispatch can be retried")
            connection.execute(
                """
                UPDATE meeting_agent_dispatches
                SET state = ?, execution_ref = ?, updated_at = ?, last_error = ''
                WHERE id = ?
                """,
                (state, execution_ref, now, dispatch_id),
            )
            connection.commit()

    def _require_meeting(self, *, meeting_id: str, owner_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, state FROM meetings WHERE id = ? AND owner_id = ?",
                (meeting_id, owner_id),
            ).fetchone()
        if row is None:
            raise MeetingNotFound(meeting_id)
        return row

    @staticmethod
    def _require_agent(agent_id: str) -> None:
        if agent_id not in AGENT_IDS:
            raise InvalidMeeting("unknown meeting agent")

    @staticmethod
    def _speech_line(row: sqlite3.Row) -> str:
        seconds = int(row["audio_start_sample"]) // 16_000
        timestamp = f"{seconds // 60:02d}:{seconds % 60:02d}"
        language = f" ({row['language']})" if row["language"] else ""
        return f"[{timestamp}{language}] {str(row['text']).strip()}"

    def _insert_dispatch(
        self,
        connection: sqlite3.Connection,
        *,
        meeting_id: str,
        agent_id: str,
        kind: str,
        payload: str,
        now: str,
        first_segment_id: int | None = None,
        last_segment_id: int | None = None,
        context_event_id: str | None = None,
    ) -> sqlite3.Row:
        identity = "|".join(
            (
                meeting_id,
                agent_id,
                kind,
                str(first_segment_id or ""),
                str(last_segment_id or ""),
                context_event_id or "",
            )
        )
        dispatch_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT OR IGNORE INTO meeting_agent_dispatches (
                id, meeting_id, agent_id, kind, first_segment_id,
                last_segment_id, context_event_id, payload_text, payload_hash,
                state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                dispatch_id,
                meeting_id,
                agent_id,
                kind,
                first_segment_id,
                last_segment_id,
                context_event_id,
                payload,
                payload_hash,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM meeting_agent_dispatches WHERE id = ?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("agent dispatch disappeared")
        return row

    @staticmethod
    def _ensure_state(
        connection: sqlite3.Connection, *, meeting_id: str, agent_id: str, now: str
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO meeting_agent_state (
                meeting_id, agent_id, state, updated_at
            ) VALUES (?, ?, 'pending', ?)
            """,
            (meeting_id, agent_id, now),
        )

    @classmethod
    def _update_agent_state(
        cls,
        connection: sqlite3.Connection,
        *,
        meeting_id: str,
        agent_id: str,
        state: str,
        now: str,
        delivered_through_segment_id: int = 0,
        increment_revision: bool = False,
        last_error: str,
    ) -> None:
        cls._ensure_state(connection, meeting_id=meeting_id, agent_id=agent_id, now=now)
        connection.execute(
            """
            UPDATE meeting_agent_state
            SET state = ?,
                delivered_through_segment_id = MAX(
                    delivered_through_segment_id, ?
                ),
                output_revision = output_revision + ?,
                last_error = ?, updated_at = ?
            WHERE meeting_id = ? AND agent_id = ?
            """,
            (
                state,
                delivered_through_segment_id,
                1 if increment_revision else 0,
                last_error,
                now,
                meeting_id,
                agent_id,
            ),
        )

    @staticmethod
    def _dispatch(connection: sqlite3.Connection, dispatch_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM meeting_agent_dispatches WHERE id = ?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise DispatchNotFound(dispatch_id)
        return row

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection
