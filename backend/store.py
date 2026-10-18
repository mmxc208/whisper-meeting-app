"""Durable SQLite store for Whisper Meeting."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.chunks import AudioSpool, ChunkMetadata

SCHEMA_VERSION = 7
MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
SPOOL_CAP_BYTES = 2 * 1024 * 1024 * 1024
RAW_AUDIO_RETENTION_DAYS = 7
RAW_AUDIO_RECENT_MEETINGS = 20
RAW_AUDIO_TOTAL_BYTES = 5 * 1024 * 1024 * 1024
_APP_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DELETE_QUARANTINE_RE = re.compile(
    r"^\.deleting-(?P<meeting_id>[0-9a-f-]{36})-(?P<token>[0-9a-f]{32})$"
)


class StoreError(Exception):
    """Base class for store failures safe to classify at the HTTP boundary."""


class InvalidMeeting(StoreError):
    """A meeting payload did not satisfy the local data contract."""


class MeetingNotFound(StoreError):
    """No meeting owned by the caller has the requested id."""


class ChunkConflict(StoreError):
    """A sequence already names different audio or metadata."""


class StorageLimit(StoreError):
    """The spool cannot durably accept another chunk."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def app_data_dir() -> Path:
    """Resolve the app-owned data directory supplied by the gateway."""

    home = os.environ.get("KIROCREW_HOME", "").strip()
    app_name = os.environ.get("KIROCREW_APP_NAME", "").strip()
    if not home:
        raise RuntimeError("KIROCREW_HOME is required")
    if not _APP_NAME_RE.fullmatch(app_name):
        raise RuntimeError("KIROCREW_APP_NAME is invalid")
    return Path(home) / "apps" / app_name / "data"


class MeetingStore:
    """Own schema migration, meeting history and the durable chunk ledger."""

    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], str] = _utc_now,
        id_factory: Callable[[], str] | None = None,
        min_free_bytes: int = MIN_FREE_BYTES,
        spool_cap_bytes: int = SPOOL_CAP_BYTES,
        raw_audio_retention_days: int = RAW_AUDIO_RETENTION_DAYS,
        raw_audio_recent_meetings: int = RAW_AUDIO_RECENT_MEETINGS,
        raw_audio_total_bytes: int = RAW_AUDIO_TOTAL_BYTES,
        disk_free: Callable[[Path], int] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.database_path = data_dir / "meetings.sqlite3"
        self._clock = clock
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._min_free_bytes = min_free_bytes
        self._spool_cap_bytes = spool_cap_bytes
        self._raw_audio_retention_days = raw_audio_retention_days
        self._raw_audio_recent_meetings = raw_audio_recent_meetings
        self._raw_audio_total_bytes = raw_audio_total_bytes
        self._disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self._migration_lock = threading.Lock()
        self._chunk_lock = threading.Lock()
        self._event_condition = threading.Condition()
        self._spool = AudioSpool(data_dir)

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "models").mkdir(exist_ok=True)
        (self.data_dir / "sessions").mkdir(exist_ok=True)
        (self.data_dir / "exports").mkdir(exist_ok=True)
        with self._migration_lock, self._connect() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current} is newer than supported schema {SCHEMA_VERSION}"
                )
            if current < 1:
                self._migrate_to_v1(connection)
                connection.execute("PRAGMA user_version = 1")
                current = 1
            if current < 2:
                self._migrate_to_v2(connection)
                connection.execute("PRAGMA user_version = 2")
                current = 2
            if current < 3:
                self._migrate_to_v3(connection)
                connection.execute("PRAGMA user_version = 3")
                current = 3
            if current < 4:
                self._migrate_to_v4(connection)
                connection.execute("PRAGMA user_version = 4")
                current = 4
            if current < 5:
                self._migrate_to_v5(connection)
                connection.execute("PRAGMA user_version = 5")
                current = 5
            if current < 6:
                self._migrate_to_v6(connection)
                connection.execute("PRAGMA user_version = 6")
                current = 6
            if current < 7:
                self._migrate_to_v7(connection)
                connection.execute("PRAGMA user_version = 7")
            connection.execute(
                "UPDATE audio_chunks SET state = 'durable' WHERE state = 'recognizing'"
            )
            connection.execute(
                "UPDATE recognition_jobs SET state = 'interrupted' WHERE state = 'running'"
            )
            connection.commit()
        self._reconcile_deletions()
        self.enforce_audio_retention()

    def schema_version(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def create_meeting(self, *, title: str, owner_id: str) -> dict[str, Any]:
        normalized_title = title.strip()
        if len(normalized_title) > 200:
            raise InvalidMeeting("title must be at most 200 characters")
        if not owner_id:
            raise InvalidMeeting("owner identity is required")

        meeting_id = self._id_factory()
        now = self._clock()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO meetings (
                    id, owner_id, title, state, created_at, updated_at
                ) VALUES (?, ?, ?, 'draft', ?, ?)
                """,
                (meeting_id, owner_id, normalized_title, now, now),
            )
            self._append_event(
                connection,
                meeting_id=meeting_id,
                kind="meeting.created",
                payload='{"state":"draft"}',
                created_at=now,
            )
            connection.commit()
        self._notify_events()
        return self.get_meeting(meeting_id=meeting_id, owner_id=owner_id)

    def list_meetings(self, *, owner_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, state, created_at, updated_at, last_error
                FROM meetings
                WHERE owner_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (owner_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_meeting(self, *, meeting_id: str, owner_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, title, state, created_at, updated_at, last_error
                FROM meetings
                WHERE id = ? AND owner_id = ?
                """,
                (meeting_id, owner_id),
            ).fetchone()
        if row is None:
            raise MeetingNotFound(meeting_id)
        return dict(row)

    def delete_meeting(self, *, meeting_id: str, owner_id: str) -> dict[str, str]:
        """Delete one non-active meeting after isolating its owned files."""

        try:
            if str(uuid.UUID(meeting_id)) != meeting_id:
                raise ValueError
        except ValueError as exc:
            raise MeetingNotFound(meeting_id) from exc
        sessions = self.data_dir / "sessions"
        original = sessions / meeting_id
        quarantine = sessions / f".deleting-{meeting_id}-{uuid.uuid4().hex}"
        moved = False
        with self._chunk_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            meeting = connection.execute(
                "SELECT state FROM meetings WHERE id = ? AND owner_id = ?",
                (meeting_id, owner_id),
            ).fetchone()
            if meeting is None:
                connection.rollback()
                raise MeetingNotFound(meeting_id)
            if meeting["state"] in {"recording", "draining"}:
                connection.rollback()
                raise InvalidMeeting("an active or draining meeting cannot be deleted")
            try:
                if original.exists():
                    os.replace(original, quarantine)
                    self._fsync_directory(sessions)
                    moved = True
                connection.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
                connection.commit()
            except BaseException:
                connection.rollback()
                if moved and quarantine.exists() and not original.exists():
                    os.replace(quarantine, original)
                    self._fsync_directory(sessions)
                raise
        if moved:
            try:
                shutil.rmtree(quarantine)
                self._fsync_directory(sessions)
            except OSError:
                pass
        return {"deleted": meeting_id}

    def start_capture(self, *, meeting_id: str, owner_id: str) -> dict[str, Any]:
        now = self._clock()
        with self._connect() as connection:
            meeting = connection.execute(
                "SELECT state FROM meetings WHERE id = ? AND owner_id = ?",
                (meeting_id, owner_id),
            ).fetchone()
            if meeting is None:
                raise MeetingNotFound(meeting_id)
            if meeting["state"] not in {"draft", "paused", "recovering"}:
                raise InvalidMeeting("meeting cannot start from its current state")
            active = connection.execute(
                "SELECT id FROM meetings WHERE state = 'recording' AND id != ? LIMIT 1",
                (meeting_id,),
            ).fetchone()
            if active is not None:
                raise InvalidMeeting("another meeting is already recording")
            epoch_row = connection.execute(
                "SELECT COALESCE(MAX(epoch), 0) + 1 FROM capture_epochs WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            epoch = int(epoch_row[0])
            connection.execute(
                """
                INSERT INTO capture_epochs (meeting_id, epoch, started_at)
                VALUES (?, ?, ?)
                """,
                (meeting_id, epoch, now),
            )
            connection.execute(
                "UPDATE meetings SET state = 'recording', updated_at = ? WHERE id = ?",
                (now, meeting_id),
            )
            self._append_event(
                connection,
                meeting_id=meeting_id,
                kind="capture.started",
                payload=f'{{"epoch":{epoch}}}',
                created_at=now,
            )
            connection.commit()
        self._notify_events()
        return {"meeting_id": meeting_id, "epoch": epoch, "state": "recording"}

    def request_drain(
        self,
        *,
        meeting_id: str,
        owner_id: str,
        kind: str,
        epoch: int,
        cut_sequence: int,
        cut_sample: int,
    ) -> dict[str, Any]:
        if kind not in {"pause", "end"}:
            raise InvalidMeeting("drain kind must be pause or end")
        if epoch < 1 or cut_sequence < -1 or cut_sample < 0:
            raise InvalidMeeting("drain cut point is invalid")
        now = self._clock()
        operation_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            meeting = connection.execute(
                "SELECT state FROM meetings WHERE id = ? AND owner_id = ?",
                (meeting_id, owner_id),
            ).fetchone()
            if meeting is None:
                connection.rollback()
                raise MeetingNotFound(meeting_id)
            existing = connection.execute(
                """
                SELECT * FROM operations
                WHERE meeting_id = ? AND state != 'complete'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
            if existing is not None:
                stored_cut = connection.execute(
                    """
                    SELECT cut_sample FROM capture_epochs
                    WHERE meeting_id = ? AND epoch = ?
                    """,
                    (meeting_id, existing["cut_epoch"]),
                ).fetchone()
                if (
                    existing["kind"] != kind
                    or int(existing["cut_epoch"]) != epoch
                    or int(existing["cut_sequence"]) != cut_sequence
                    or stored_cut is None
                    or int(stored_cut["cut_sample"] or 0) != cut_sample
                ):
                    connection.rollback()
                    raise InvalidMeeting("another drain operation is already in progress")
                connection.rollback()
                return dict(existing)
            latest = connection.execute(
                """
                SELECT * FROM operations
                WHERE meeting_id = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
            expected_terminal = "paused" if kind == "pause" else "ended"
            latest_cut = (
                connection.execute(
                    """
                    SELECT cut_sample FROM capture_epochs
                    WHERE meeting_id = ? AND epoch = ?
                    """,
                    (meeting_id, latest["cut_epoch"]),
                ).fetchone()
                if latest is not None
                else None
            )
            if (
                latest is not None
                and latest["state"] == "complete"
                and latest["kind"] == kind
                and int(latest["cut_epoch"]) == epoch
                and int(latest["cut_sequence"]) == cut_sequence
                and latest_cut is not None
                and int(latest_cut["cut_sample"] or 0) == cut_sample
                and meeting["state"] == expected_terminal
            ):
                connection.rollback()
                return dict(latest)
            if meeting["state"] == "paused" and kind == "end":
                latest_epoch = connection.execute(
                    """
                    SELECT epoch, cut_sample FROM capture_epochs
                    WHERE meeting_id = ? AND stopped_at IS NOT NULL
                    ORDER BY epoch DESC LIMIT 1
                    """,
                    (meeting_id,),
                ).fetchone()
                if latest_epoch is None or int(latest_epoch["epoch"]) != epoch:
                    connection.rollback()
                    raise InvalidMeeting("capture epoch does not match the paused meeting")
                latest_sequence = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) FROM audio_chunks
                    WHERE meeting_id = ? AND epoch = ?
                    """,
                    (meeting_id, epoch),
                ).fetchone()
                expected_sequence = int(latest_sequence[0])
                expected_sample = int(latest_epoch["cut_sample"] or 0)
                if cut_sequence != expected_sequence or cut_sample != expected_sample:
                    connection.rollback()
                    raise InvalidMeeting("drain cut point does not match the paused meeting")
                connection.execute(
                    """
                    INSERT INTO operations (
                        id, meeting_id, kind, state, cut_epoch, cut_sequence,
                        created_at, updated_at
                    ) VALUES (?, ?, 'end', 'complete', ?, ?, ?, ?)
                    """,
                    (operation_id, meeting_id, epoch, cut_sequence, now, now),
                )
                connection.execute(
                    "UPDATE meetings SET state = 'ended', updated_at = ? WHERE id = ?",
                    (now, meeting_id),
                )
                self._append_event(
                    connection,
                    meeting_id=meeting_id,
                    kind="meeting.ended",
                    payload=f'{{"operation_id":"{operation_id}"}}',
                    created_at=now,
                )
                connection.commit()
                operation = connection.execute(
                    "SELECT * FROM operations WHERE id = ?", (operation_id,)
                ).fetchone()
                self._notify_events()
                if operation is None:
                    raise RuntimeError("end operation disappeared")
                return dict(operation)
            if meeting["state"] != "recording":
                connection.rollback()
                raise InvalidMeeting("meeting is not recording")
            active_epoch = connection.execute(
                """
                SELECT epoch FROM capture_epochs
                WHERE meeting_id = ? AND stopped_at IS NULL
                ORDER BY epoch DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
            if active_epoch is None or int(active_epoch["epoch"]) != epoch:
                connection.rollback()
                raise InvalidMeeting("capture epoch does not match the active recording")
            rows = connection.execute(
                """
                SELECT sequence, sample_start, sample_count
                FROM audio_chunks WHERE meeting_id = ? AND epoch = ?
                ORDER BY sequence
                """,
                (meeting_id, epoch),
            ).fetchall()
            if cut_sequence == -1:
                valid_cut = not rows and cut_sample == 0
            else:
                valid_cut = (
                    len(rows) == cut_sequence + 1
                    and all(int(row["sequence"]) == index for index, row in enumerate(rows))
                    and int(rows[-1]["sample_start"]) + int(rows[-1]["sample_count"]) == cut_sample
                )
            if not valid_cut:
                connection.rollback()
                raise InvalidMeeting("drain cut point has a missing or unexpected audio range")
            connection.execute(
                """
                UPDATE capture_epochs SET stopped_at = ?, cut_sample = ?
                WHERE meeting_id = ? AND epoch = ?
                """,
                (now, cut_sample, meeting_id, epoch),
            )
            connection.execute(
                "UPDATE meetings SET state = 'draining', updated_at = ? WHERE id = ?",
                (now, meeting_id),
            )
            connection.execute(
                """
                INSERT INTO operations (
                    id, meeting_id, kind, state, cut_epoch, cut_sequence,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'transcribing', ?, ?, ?, ?)
                """,
                (operation_id, meeting_id, kind, epoch, cut_sequence, now, now),
            )
            self._append_event(
                connection,
                meeting_id=meeting_id,
                kind=f"meeting.{kind}.started",
                payload=(
                    f'{{"operation_id":"{operation_id}","epoch":{epoch},'
                    f'"cut_sequence":{cut_sequence},"cut_sample":{cut_sample}}}'
                ),
                created_at=now,
            )
            self._advance_operations_in_connection(connection, meeting_id=meeting_id, now=now)
            connection.commit()
            operation = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        self._notify_events()
        if operation is None:
            raise RuntimeError("drain operation disappeared")
        return dict(operation)

    def resume_capture(self, *, meeting_id: str, owner_id: str) -> dict[str, Any]:
        meeting = self.get_meeting(meeting_id=meeting_id, owner_id=owner_id)
        if meeting["state"] != "paused":
            raise InvalidMeeting("only a paused meeting can resume")
        return self.start_capture(meeting_id=meeting_id, owner_id=owner_id)

    def operation_for_meeting(self, *, meeting_id: str, owner_id: str) -> dict[str, Any] | None:
        self.get_meeting(meeting_id=meeting_id, owner_id=owner_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM operations WHERE meeting_id = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def meeting_progress(self, *, meeting_id: str, owner_id: str) -> dict[str, Any]:
        meeting = self.get_meeting(meeting_id=meeting_id, owner_id=owner_id)
        with self._connect() as connection:
            counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN state = 'transcribed' THEN 1 ELSE 0 END) AS transcribed,
                       SUM(CASE WHEN state IN ('durable', 'recognizing') THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN pcm_retained = 1 THEN sample_count * 2 ELSE 0 END)
                           AS spool_bytes
                FROM audio_chunks WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()
            epoch = connection.execute(
                """
                SELECT epoch, started_at, stopped_at, cut_sample
                FROM capture_epochs WHERE meeting_id = ?
                ORDER BY epoch DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
            capture: dict[str, Any] | None = None
            if epoch is not None:
                chunk = connection.execute(
                    """
                    SELECT sequence, sample_start, sample_count
                    FROM audio_chunks WHERE meeting_id = ? AND epoch = ?
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (meeting_id, epoch["epoch"]),
                ).fetchone()
                capture = {
                    "epoch": int(epoch["epoch"]),
                    "active": epoch["stopped_at"] is None,
                    "cut_sequence": int(chunk["sequence"]) if chunk is not None else -1,
                    "cut_sample": (
                        int(chunk["sample_start"]) + int(chunk["sample_count"])
                        if chunk is not None
                        else int(epoch["cut_sample"] or 0)
                    ),
                }
        return {
            "meeting": meeting,
            "capture": capture,
            "operation": self.operation_for_meeting(meeting_id=meeting_id, owner_id=owner_id),
            "progress": {
                "chunks": int(counts["total"] or 0),
                "transcribed_chunks": int(counts["transcribed"] or 0),
                "pending_chunks": int(counts["pending"] or 0),
                "spool_bytes": int(counts["spool_bytes"] or 0),
            },
        }

    def force_recognition_tail(self, *, meeting_id: str, epoch: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM operations
                WHERE meeting_id = ? AND cut_epoch = ?
                  AND state = 'transcribing'
                LIMIT 1
                """,
                (meeting_id, epoch),
            ).fetchone()
        return row is not None

    def events_after(self, *, meeting_id: str, owner_id: str, cursor: int) -> list[dict[str, Any]]:
        self.get_meeting(meeting_id=meeting_id, owner_id=owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT cursor, kind, payload_json, created_at
                FROM events WHERE meeting_id = ? AND cursor > ?
                ORDER BY cursor LIMIT 100
                """,
                (meeting_id, cursor),
            ).fetchall()
        return [dict(row) for row in rows]

    def wait_for_events(
        self,
        *,
        meeting_id: str,
        owner_id: str,
        cursor: int,
        timeout: float,
    ) -> list[dict[str, Any]]:
        with self._event_condition:
            events = self.events_after(meeting_id=meeting_id, owner_id=owner_id, cursor=cursor)
            if events:
                return events
            self._event_condition.wait(timeout=timeout)
            return self.events_after(meeting_id=meeting_id, owner_id=owner_id, cursor=cursor)

    def store_chunk(
        self,
        *,
        metadata: ChunkMetadata,
        body: bytes,
        owner_id: str,
    ) -> dict[str, Any]:
        metadata.validate(body)
        with self._chunk_lock:
            with self._connect() as connection:
                epoch_active = self._require_epoch(connection, metadata=metadata, owner_id=owner_id)
                existing = connection.execute(
                    """
                    SELECT sha256, sample_start, sample_count, sample_rate,
                           channels, encoding, spool_path
                    FROM audio_chunks
                    WHERE meeting_id = ? AND epoch = ? AND sequence = ?
                    """,
                    (metadata.meeting_id, metadata.epoch, metadata.sequence),
                ).fetchone()
                if existing is not None:
                    expected = (
                        metadata.sha256,
                        metadata.sample_start,
                        metadata.sample_count,
                        metadata.sample_rate,
                        metadata.channels,
                        metadata.encoding,
                    )
                    observed = (
                        existing["sha256"],
                        existing["sample_start"],
                        existing["sample_count"],
                        existing["sample_rate"],
                        existing["channels"],
                        existing["encoding"],
                    )
                    if observed != expected:
                        raise ChunkConflict("sequence already contains different audio")
                    path = self.data_dir / existing["spool_path"]
                    if not self._spool.verify(path, metadata.sha256):
                        path = self._spool.write(metadata, body)
                        connection.execute(
                            """
                            UPDATE audio_chunks SET spool_path = ?, state = 'durable'
                            WHERE meeting_id = ? AND epoch = ? AND sequence = ?
                            """,
                            (
                                self._spool.relative_path(path),
                                metadata.meeting_id,
                                metadata.epoch,
                                metadata.sequence,
                            ),
                        )
                        connection.commit()
                    return self._chunk_ack(metadata, duplicate=True)

                if not epoch_active:
                    raise InvalidMeeting("capture epoch is already stopped")

                used = int(
                    connection.execute(
                        """
                        SELECT COALESCE(SUM(sample_count * 2), 0)
                        FROM audio_chunks WHERE state != 'transcribed'
                        """
                    ).fetchone()[0]
                )
                if used + len(body) > self._spool_cap_bytes:
                    raise StorageLimit("backend audio spool limit reached")
                if self._disk_free(self.data_dir) - len(body) < self._min_free_bytes:
                    raise StorageLimit("minimum free disk reserve reached")

                path = self._spool.write(metadata, body)
                connection.execute(
                    """
                    INSERT INTO audio_chunks (
                        meeting_id, epoch, sequence, sha256, sample_start,
                        sample_count, sample_rate, channels, encoding,
                        spool_path, state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'durable', ?)
                    """,
                    (
                        metadata.meeting_id,
                        metadata.epoch,
                        metadata.sequence,
                        metadata.sha256,
                        metadata.sample_start,
                        metadata.sample_count,
                        metadata.sample_rate,
                        metadata.channels,
                        metadata.encoding,
                        self._spool.relative_path(path),
                        self._clock(),
                    ),
                )
                connection.commit()
        return self._chunk_ack(metadata, duplicate=False)

    def reconcile_spool(self) -> dict[str, int]:
        recovered = 0
        missing = 0
        with self._chunk_lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT meeting_id, epoch, sequence, sha256, spool_path, state,
                       pcm_retained
                FROM audio_chunks
                """
            ).fetchall()
            for row in rows:
                path = self.data_dir / row["spool_path"]
                if not self._spool.verify(path, row["sha256"]):
                    if row["state"] == "transcribed":
                        connection.execute(
                            """
                            UPDATE audio_chunks SET pcm_retained = 0
                            WHERE meeting_id = ? AND epoch = ? AND sequence = ?
                            """,
                            (row["meeting_id"], row["epoch"], row["sequence"]),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE audio_chunks SET state = 'missing', pcm_retained = 0
                            WHERE meeting_id = ? AND epoch = ? AND sequence = ?
                            """,
                            (row["meeting_id"], row["epoch"], row["sequence"]),
                        )
                        missing += 1
            for metadata, path in self._spool.iter_objects():
                if not self._spool.verify(path, metadata.sha256):
                    continue
                exists = connection.execute(
                    """
                    SELECT 1 FROM audio_chunks
                    WHERE meeting_id = ? AND epoch = ? AND sequence = ?
                    """,
                    (metadata.meeting_id, metadata.epoch, metadata.sequence),
                ).fetchone()
                if exists is not None:
                    continue
                epoch = connection.execute(
                    """
                    SELECT 1 FROM capture_epochs
                    WHERE meeting_id = ? AND epoch = ?
                    """,
                    (metadata.meeting_id, metadata.epoch),
                ).fetchone()
                if epoch is None:
                    continue
                connection.execute(
                    """
                    INSERT INTO audio_chunks (
                        meeting_id, epoch, sequence, sha256, sample_start,
                        sample_count, sample_rate, channels, encoding,
                        spool_path, state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'durable', ?)
                    """,
                    (
                        metadata.meeting_id,
                        metadata.epoch,
                        metadata.sequence,
                        metadata.sha256,
                        metadata.sample_start,
                        metadata.sample_count,
                        metadata.sample_rate,
                        metadata.channels,
                        metadata.encoding,
                        self._spool.relative_path(path),
                        self._clock(),
                    ),
                )
                recovered += 1
            connection.execute(
                """
                UPDATE audio_chunks SET state = 'durable'
                WHERE force_decode = 1 AND pcm_retained = 1
                  AND state = 'transcribed'
                """
            )
            connection.commit()
        self._notify_events()
        return {"recovered": recovered, "missing": missing}

    def enforce_audio_retention(self) -> int:
        """Retire ended-meeting PCM only when the explicit retention policy allows it."""

        now = datetime.fromisoformat(self._clock().replace("Z", "+00:00"))
        cutoff = now - timedelta(days=self._raw_audio_retention_days)
        with self._connect() as connection:
            meetings = connection.execute(
                """
                SELECT meetings.id, meetings.updated_at,
                       COALESCE(SUM(
                           CASE WHEN audio_chunks.pcm_retained = 1
                                THEN audio_chunks.sample_count * 2 ELSE 0 END
                       ), 0) AS audio_bytes
                FROM meetings
                LEFT JOIN audio_chunks ON audio_chunks.meeting_id = meetings.id
                WHERE meetings.state = 'ended'
                GROUP BY meetings.id
                ORDER BY meetings.updated_at DESC, meetings.id DESC
                """
            ).fetchall()
        total = sum(int(row["audio_bytes"]) for row in meetings)
        recent_ids = {str(row["id"]) for row in meetings[: self._raw_audio_recent_meetings]}
        selected: list[str] = []
        for row in reversed(meetings):
            meeting_id = str(row["id"])
            updated = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
            expired = updated < cutoff and meeting_id not in recent_ids
            over_cap = total > self._raw_audio_total_bytes
            if not expired and not over_cap:
                continue
            selected.append(meeting_id)
            total -= int(row["audio_bytes"])

        retired = 0
        for meeting_id in selected:
            with self._connect() as connection:
                chunks = connection.execute(
                    """
                    SELECT epoch, sequence, spool_path FROM audio_chunks
                    WHERE meeting_id = ? AND state = 'transcribed' AND pcm_retained = 1
                    """,
                    (meeting_id,),
                ).fetchall()
            retired_keys: list[tuple[int, int]] = []
            for chunk in chunks:
                path = self.data_dir / chunk["spool_path"]
                try:
                    path.unlink(missing_ok=True)
                    retired += 1
                    retired_keys.append((int(chunk["epoch"]), int(chunk["sequence"])))
                except OSError:
                    continue
            if retired_keys:
                with self._connect() as connection:
                    connection.executemany(
                        """
                        UPDATE audio_chunks SET pcm_retained = 0
                        WHERE meeting_id = ? AND epoch = ? AND sequence = ?
                        """,
                        ((meeting_id, epoch, sequence) for epoch, sequence in retired_keys),
                    )
                    connection.commit()
        return retired

    def model_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT artifact_id, sha256, status, self_tested_at, error
                FROM model_state WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("model state is missing")
        return dict(row)

    def set_model_state(
        self,
        *,
        status: str,
        artifact_id: str = "large-v3-turbo",
        sha256: str = "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
        error: str = "",
        self_tested: bool = False,
    ) -> dict[str, Any]:
        if status not in {"missing", "installing", "ready", "failed"}:
            raise ValueError("invalid model status")
        tested_at = self._clock() if self_tested else None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE model_state
                SET artifact_id = ?, sha256 = ?, status = ?,
                    self_tested_at = ?, error = ?
                WHERE singleton = 1
                """,
                (artifact_id, sha256, status, tested_at, error),
            )
            connection.commit()
        return self.model_state()

    def pending_recognition_group(self) -> list[dict[str, Any]]:
        """Return one ordered, contiguous durable prefix for the scheduler."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chunks.meeting_id, chunks.epoch, chunks.sequence,
                       chunks.sample_start, chunks.sample_count, chunks.spool_path,
                       chunks.force_decode
                FROM audio_chunks AS chunks
                JOIN meetings ON meetings.id = chunks.meeting_id
                WHERE chunks.state = 'durable'
                ORDER BY meetings.created_at, chunks.meeting_id,
                         chunks.epoch, chunks.sequence
                """
            ).fetchall()
        if not rows:
            return []
        first = rows[0]
        selected: list[dict[str, Any]] = []
        expected_sequence = int(first["sequence"])
        expected_sample = int(first["sample_start"])
        for row in rows:
            if row["meeting_id"] != first["meeting_id"] or row["epoch"] != first["epoch"]:
                break
            if int(row["sequence"]) != expected_sequence:
                break
            if int(row["sample_start"]) != expected_sample:
                break
            value = dict(row)
            selected.append(value)
            expected_sequence += 1
            expected_sample += int(row["sample_count"])
        return selected

    def previous_transcribed_chunk(
        self, *, meeting_id: str, epoch: int, before_sequence: int
    ) -> dict[str, Any] | None:
        if before_sequence < 1:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT meeting_id, epoch, sequence, sample_start,
                       sample_count, spool_path
                FROM audio_chunks
                WHERE meeting_id = ? AND epoch = ? AND sequence = ?
                  AND state = 'transcribed' AND pcm_retained = 1
                """,
                (meeting_id, epoch, before_sequence - 1),
            ).fetchone()
        return dict(row) if row is not None else None

    def claim_recognition(
        self,
        *,
        chunks: tuple[dict[str, Any], ...],
        audio_start_sample: int,
        audio_end_sample: int,
    ) -> dict[str, Any] | None:
        if not chunks:
            return None
        first = chunks[0]
        last = chunks[-1]
        job_id = str(uuid.uuid4())
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in chunks)
            parameters: list[Any] = [first["meeting_id"], first["epoch"]]
            parameters.extend(int(chunk["sequence"]) for chunk in chunks)
            count = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM audio_chunks
                    WHERE meeting_id = ? AND epoch = ?
                      AND sequence IN ({placeholders}) AND state = 'durable'
                    """,
                    parameters,
                ).fetchone()[0]
            )
            if count != len(chunks):
                connection.rollback()
                return None
            connection.execute(
                f"""
                UPDATE audio_chunks SET state = 'recognizing'
                WHERE meeting_id = ? AND epoch = ?
                  AND sequence IN ({placeholders})
                """,
                parameters,
            )
            connection.execute(
                """
                INSERT INTO recognition_jobs (
                    id, meeting_id, epoch, first_sequence, last_sequence,
                    audio_start_sample, audio_end_sample, state,
                    created_at, updated_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, '')
                """,
                (
                    job_id,
                    first["meeting_id"],
                    first["epoch"],
                    first["sequence"],
                    last["sequence"],
                    audio_start_sample,
                    audio_end_sample,
                    now,
                    now,
                ),
            )
            connection.commit()
        return {
            "id": job_id,
            "meeting_id": first["meeting_id"],
            "epoch": int(first["epoch"]),
            "first_sequence": int(first["sequence"]),
            "last_sequence": int(last["sequence"]),
            "audio_start_sample": audio_start_sample,
            "audio_end_sample": audio_end_sample,
            "chunks": [dict(chunk) for chunk in chunks],
        }

    def complete_recognition(
        self,
        *,
        job: dict[str, Any],
        segments: list[dict[str, Any]],
        language: str,
        coverage_kind: str = "empty_decode",
        filtered_segments: list[dict[str, Any]] | None = None,
    ) -> None:
        if coverage_kind not in {"silence", "empty_decode"}:
            raise ValueError("invalid empty coverage kind")
        now = self._clock()
        committed: list[dict[str, Any]] = []
        cursor = int(job["audio_start_sample"])
        end = int(job["audio_end_sample"])
        for segment in sorted(segments, key=lambda value: (value["start"], value["end"])):
            segment_start = max(cursor, int(segment["start"]))
            segment_end = min(end, int(segment["end"]))
            if segment_start > cursor:
                committed.append(
                    {
                        "start": cursor,
                        "end": segment_start,
                        "text": "",
                        "coverage_kind": coverage_kind,
                    }
                )
            if segment_end > segment_start:
                committed.append(
                    {
                        "start": segment_start,
                        "end": segment_end,
                        "text": segment["text"],
                        "coverage_kind": "speech",
                    }
                )
                cursor = segment_end
        if cursor < end:
            committed.append(
                {"start": cursor, "end": end, "text": "", "coverage_kind": coverage_kind}
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                "SELECT state FROM recognition_jobs WHERE id = ?", (job["id"],)
            ).fetchone()
            if running is None or running["state"] != "running":
                connection.rollback()
                raise RuntimeError("recognition claim is no longer active")
            replacing = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audio_chunks
                    WHERE meeting_id = ? AND epoch = ?
                      AND sequence BETWEEN ? AND ? AND force_decode = 1
                    """,
                    (
                        job["meeting_id"],
                        job["epoch"],
                        job["first_sequence"],
                        job["last_sequence"],
                    ),
                ).fetchone()[0]
            )
            if replacing:
                connection.execute(
                    """
                    DELETE FROM transcript_segments
                    WHERE meeting_id = ? AND epoch = ?
                      AND audio_start_sample < ? AND audio_end_sample > ?
                    """,
                    (
                        job["meeting_id"],
                        job["epoch"],
                        job["audio_end_sample"],
                        job["audio_start_sample"],
                    ),
                )
            for segment in committed:
                connection.execute(
                    """
                    INSERT INTO transcript_segments (
                        meeting_id, epoch, audio_start_sample, audio_end_sample,
                        language, text, coverage_kind, committed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job["meeting_id"],
                        job["epoch"],
                        segment["start"],
                        segment["end"],
                        language,
                        segment["text"],
                        segment["coverage_kind"],
                        now,
                    ),
                )
            for filtered in filtered_segments or []:
                connection.execute(
                    """
                    INSERT INTO filtered_transcript_segments (
                        meeting_id, epoch, audio_start_sample, audio_end_sample,
                        language, text, reason, average_logprob,
                        no_speech_probability, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job["meeting_id"],
                        job["epoch"],
                        filtered["start"],
                        filtered["end"],
                        language,
                        filtered["text"],
                        filtered["reason"],
                        filtered.get("average_logprob"),
                        filtered.get("no_speech_probability"),
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE audio_chunks SET state = 'transcribed', force_decode = 0
                WHERE meeting_id = ? AND epoch = ?
                  AND sequence BETWEEN ? AND ?
                """,
                (
                    job["meeting_id"],
                    job["epoch"],
                    job["first_sequence"],
                    job["last_sequence"],
                ),
            )
            connection.execute(
                "UPDATE recognition_jobs SET state = 'complete', updated_at = ? WHERE id = ?",
                (now, job["id"]),
            )
            self._append_event(
                connection,
                meeting_id=job["meeting_id"],
                kind="transcript.committed",
                payload=f'{{"job_id":"{job["id"]}","segments":{len(committed)}}}',
                created_at=now,
            )
            self._advance_operations_in_connection(
                connection, meeting_id=job["meeting_id"], now=now
            )
            connection.commit()
        self._notify_events()
        with self._connect() as connection:
            meeting_state = connection.execute(
                "SELECT state FROM meetings WHERE id = ?", (job["meeting_id"],)
            ).fetchone()[0]
        if meeting_state == "ended":
            self.enforce_audio_retention()

    def interrupt_recognition(self, *, job: dict[str, Any], error: str) -> None:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE audio_chunks SET state = 'durable'
                WHERE meeting_id = ? AND epoch = ?
                  AND sequence BETWEEN ? AND ? AND state = 'recognizing'
                """,
                (
                    job["meeting_id"],
                    job["epoch"],
                    job["first_sequence"],
                    job["last_sequence"],
                ),
            )
            connection.execute(
                """
                UPDATE recognition_jobs
                SET state = 'interrupted', updated_at = ?, error = ?
                WHERE id = ?
                """,
                (now, error[:1000], job["id"]),
            )
            connection.execute(
                """
                UPDATE meetings
                SET state = CASE
                        WHEN state IN ('recording', 'draining') THEN state
                        ELSE 'recovering'
                    END,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:1000], now, job["meeting_id"]),
            )
            self._append_event(
                connection,
                meeting_id=job["meeting_id"],
                kind="recognition.interrupted",
                payload=f'{{"job_id":"{job["id"]}"}}',
                created_at=now,
            )
            connection.commit()
        self._notify_events()

    def transcript(self, *, meeting_id: str, owner_id: str) -> list[dict[str, Any]]:
        self.get_meeting(meeting_id=meeting_id, owner_id=owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, epoch, audio_start_sample, audio_end_sample,
                       language, text, coverage_kind, committed_at
                FROM transcript_segments
                WHERE meeting_id = ?
                ORDER BY epoch, audio_start_sample, id
                """,
                (meeting_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def filtered_transcript(self, *, meeting_id: str, owner_id: str) -> list[dict[str, Any]]:
        self.get_meeting(meeting_id=meeting_id, owner_id=owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, epoch, audio_start_sample, audio_end_sample,
                       language, text, reason, average_logprob,
                       no_speech_probability, created_at
                FROM filtered_transcript_segments
                WHERE meeting_id = ?
                ORDER BY epoch, audio_start_sample, id
                """,
                (meeting_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def retry_coverage(self, *, meeting_id: str, segment_id: int, owner_id: str) -> dict[str, Any]:
        """Queue a chunk-aligned forced decode while keeping current text visible."""

        meeting = self.get_meeting(meeting_id=meeting_id, owner_id=owner_id)
        if meeting["state"] in {"recording", "draining"}:
            raise InvalidMeeting("coverage cannot be retried while capture is active")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            segment = connection.execute(
                """
                SELECT epoch, audio_start_sample, audio_end_sample
                FROM transcript_segments WHERE id = ? AND meeting_id = ?
                """,
                (segment_id, meeting_id),
            ).fetchone()
            if segment is None:
                connection.rollback()
                raise InvalidMeeting("coverage segment does not exist")
            chunks = connection.execute(
                """
                SELECT sequence, pcm_retained, spool_path FROM audio_chunks
                WHERE meeting_id = ? AND epoch = ?
                  AND sample_start < ? AND sample_start + sample_count > ?
                ORDER BY sequence
                """,
                (
                    meeting_id,
                    segment["epoch"],
                    segment["audio_end_sample"],
                    segment["audio_start_sample"],
                ),
            ).fetchall()
            if not chunks:
                connection.rollback()
                raise InvalidMeeting("coverage has no source audio")
            if any(
                not int(chunk["pcm_retained"])
                or not (self.data_dir / chunk["spool_path"]).is_file()
                for chunk in chunks
            ):
                connection.rollback()
                raise InvalidMeeting("source audio is outside the retention window")
            first_sequence = int(chunks[0]["sequence"])
            last_sequence = int(chunks[-1]["sequence"])
            busy = connection.execute(
                """
                SELECT COUNT(*) FROM audio_chunks
                WHERE meeting_id = ? AND epoch = ?
                  AND sequence BETWEEN ? AND ? AND state != 'transcribed'
                """,
                (meeting_id, segment["epoch"], first_sequence, last_sequence),
            ).fetchone()[0]
            if busy:
                connection.rollback()
                raise InvalidMeeting("coverage is already queued for recognition")
            connection.execute(
                """
                UPDATE audio_chunks SET state = 'durable', force_decode = 1
                WHERE meeting_id = ? AND epoch = ?
                  AND sequence BETWEEN ? AND ?
                """,
                (meeting_id, segment["epoch"], first_sequence, last_sequence),
            )
            now = self._clock()
            self._append_event(
                connection,
                meeting_id=meeting_id,
                kind="transcript.retry_requested",
                payload=f'{{"segment_id":{segment_id}}}',
                created_at=now,
            )
            connection.commit()
        self._notify_events()
        return {"meeting_id": meeting_id, "segment_id": segment_id, "state": "queued"}

    def _advance_operations_in_connection(
        self, connection: sqlite3.Connection, *, meeting_id: str, now: str
    ) -> None:
        operation = connection.execute(
            """
            SELECT * FROM operations
            WHERE meeting_id = ? AND state = 'transcribing'
            ORDER BY created_at, id LIMIT 1
            """,
            (meeting_id,),
        ).fetchone()
        if operation is None:
            return
        pending = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM audio_chunks
                WHERE meeting_id = ? AND epoch = ?
                  AND sequence <= ? AND state != 'transcribed'
                """,
                (
                    meeting_id,
                    operation["cut_epoch"],
                    operation["cut_sequence"],
                ),
            ).fetchone()[0]
        )
        if pending:
            return
        terminal_state = "paused" if operation["kind"] == "pause" else "ended"
        connection.execute(
            "UPDATE operations SET state = 'complete', updated_at = ? WHERE id = ?",
            (now, operation["id"]),
        )
        connection.execute(
            """
            UPDATE meetings SET state = ?, updated_at = ?, last_error = ''
            WHERE id = ?
            """,
            (terminal_state, now, meeting_id),
        )
        self._append_event(
            connection,
            meeting_id=meeting_id,
            kind=f"meeting.{terminal_state}",
            payload=f'{{"operation_id":"{operation["id"]}"}}',
            created_at=now,
        )

    def _notify_events(self) -> None:
        with self._event_condition:
            self._event_condition.notify_all()

    def _reconcile_deletions(self) -> None:
        sessions = self.data_dir / "sessions"
        if not sessions.is_dir():
            return
        with self._connect() as connection:
            for candidate in sessions.iterdir():
                match = _DELETE_QUARANTINE_RE.fullmatch(candidate.name)
                if match is None or not candidate.is_dir():
                    continue
                meeting_id = match.group("meeting_id")
                exists = connection.execute(
                    "SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)
                ).fetchone()
                original = sessions / meeting_id
                if exists is None:
                    shutil.rmtree(candidate)
                elif not original.exists():
                    os.replace(candidate, original)
                else:
                    raise RuntimeError(
                        f"meeting deletion recovery found two session trees for {meeting_id}"
                    )
                self._fsync_directory(sessions)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _require_epoch(
        connection: sqlite3.Connection,
        *,
        metadata: ChunkMetadata,
        owner_id: str,
    ) -> bool:
        row = connection.execute(
            """
            SELECT epochs.stopped_at
            FROM capture_epochs AS epochs
            JOIN meetings ON meetings.id = epochs.meeting_id
            WHERE epochs.meeting_id = ? AND epochs.epoch = ?
              AND meetings.owner_id = ?
            """,
            (metadata.meeting_id, metadata.epoch, owner_id),
        ).fetchone()
        if row is None:
            raise MeetingNotFound(metadata.meeting_id)
        return row["stopped_at"] is None

    @staticmethod
    def _chunk_ack(metadata: ChunkMetadata, *, duplicate: bool) -> dict[str, Any]:
        return {
            "meeting_id": metadata.meeting_id,
            "epoch": metadata.epoch,
            "sequence": metadata.sequence,
            "sha256": metadata.sha256,
            "durable": True,
            "duplicate": duplicate,
        }

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        meeting_id: str,
        kind: str,
        payload: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (meeting_id, kind, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (meeting_id, kind, payload, created_at),
        )

    @staticmethod
    def _migrate_to_v1(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL CHECK (
                    state IN (
                        'draft', 'recording', 'draining', 'paused',
                        'recovering', 'ended', 'failed'
                    )
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX meetings_owner_history
                ON meetings (owner_id, created_at DESC, id DESC);

            CREATE TABLE capture_epochs (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                epoch INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                stopped_at TEXT,
                cut_sample INTEGER,
                PRIMARY KEY (meeting_id, epoch)
            );

            CREATE TABLE audio_chunks (
                meeting_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                sample_start INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                spool_path TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (meeting_id, epoch, sequence),
                FOREIGN KEY (meeting_id, epoch)
                    REFERENCES capture_epochs(meeting_id, epoch) ON DELETE CASCADE
            );

            CREATE TABLE transcript_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                epoch INTEGER NOT NULL,
                audio_start_sample INTEGER NOT NULL,
                audio_end_sample INTEGER NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                committed_at TEXT NOT NULL
            );

            CREATE INDEX transcript_order
                ON transcript_segments (
                    meeting_id, epoch, audio_start_sample, id
                );

            CREATE TABLE operations (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN ('pause', 'end')),
                state TEXT NOT NULL,
                cut_epoch INTEGER NOT NULL,
                cut_sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX events_replay
                ON events (meeting_id, cursor);

            CREATE TABLE model_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                artifact_id TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'missing',
                self_tested_at TEXT,
                error TEXT NOT NULL DEFAULT ''
            );

            INSERT INTO model_state (singleton) VALUES (1);
            """
        )

    @staticmethod
    def _migrate_to_v2(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            ALTER TABLE meetings
                ADD COLUMN last_error TEXT NOT NULL DEFAULT '';

            CREATE INDEX operations_by_meeting
                ON operations (meeting_id, created_at, id);
            """
        )

    @staticmethod
    def _migrate_to_v3(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            ALTER TABLE audio_chunks
                ADD COLUMN sample_rate INTEGER NOT NULL DEFAULT 16000;
            ALTER TABLE audio_chunks
                ADD COLUMN channels INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE audio_chunks
                ADD COLUMN encoding TEXT NOT NULL DEFAULT 'pcm_s16le';
            """
        )

    @staticmethod
    def _migrate_to_v4(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE recognition_jobs (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                epoch INTEGER NOT NULL,
                first_sequence INTEGER NOT NULL,
                last_sequence INTEGER NOT NULL,
                audio_start_sample INTEGER NOT NULL,
                audio_end_sample INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('running', 'complete', 'interrupted')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX recognition_jobs_by_state
                ON recognition_jobs (state, created_at, id);
            """
        )

    @staticmethod
    def _migrate_to_v5(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            ALTER TABLE audio_chunks
                ADD COLUMN pcm_retained INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE audio_chunks
                ADD COLUMN force_decode INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE transcript_segments
                ADD COLUMN coverage_kind TEXT NOT NULL DEFAULT 'empty_decode'
                CHECK (coverage_kind IN ('speech', 'silence', 'empty_decode'));

            UPDATE transcript_segments
            SET coverage_kind = CASE WHEN trim(text) = '' THEN 'empty_decode' ELSE 'speech' END;

            CREATE TABLE filtered_transcript_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                epoch INTEGER NOT NULL,
                audio_start_sample INTEGER NOT NULL,
                audio_end_sample INTEGER NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                reason TEXT NOT NULL,
                average_logprob REAL,
                no_speech_probability REAL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX filtered_transcript_order
                ON filtered_transcript_segments (
                    meeting_id, epoch, audio_start_sample, id
                );
            """
        )

    @staticmethod
    def _migrate_to_v6(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE audio_chunks AS chunks
            SET force_decode = 1
            WHERE chunks.state = 'transcribed'
              AND chunks.pcm_retained = 1
              AND EXISTS (
                  SELECT 1 FROM transcript_segments AS segments
                  WHERE segments.meeting_id = chunks.meeting_id
                    AND segments.coverage_kind = 'silence'
              )
            """
        )

    @staticmethod
    def _migrate_to_v7(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE meeting_agent_state (
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL DEFAULT '',
                execution_ref TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'pending' CHECK (
                    state IN (
                        'pending', 'working', 'caught_up', 'finalizing',
                        'complete', 'unavailable', 'failed'
                    )
                ),
                delivered_through_segment_id INTEGER NOT NULL DEFAULT 0,
                output_revision INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (meeting_id, agent_id)
            );

            CREATE TABLE meeting_context_events (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX meeting_context_timeline
                ON meeting_context_events (meeting_id, created_at, id);

            CREATE TABLE meeting_agent_dispatches (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('speech', 'chat', 'finalize')),
                first_segment_id INTEGER,
                last_segment_id INTEGER,
                context_event_id TEXT REFERENCES meeting_context_events(id) ON DELETE CASCADE,
                payload_text TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending' CHECK (
                    state IN ('pending', 'accepted', 'running', 'complete', 'failed')
                ),
                execution_ref TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX meeting_agent_dispatch_queue
                ON meeting_agent_dispatches (state, created_at, id);
            CREATE INDEX meeting_agent_dispatch_timeline
                ON meeting_agent_dispatches (meeting_id, agent_id, created_at, id);
            """
        )
