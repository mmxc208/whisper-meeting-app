"""Validated durable PCM spool objects."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

_OBJECT_RE = re.compile(
    r"^e(?P<epoch>[0-9]+)-s(?P<sequence>[0-9]+)-a(?P<sample_start>[0-9]+)"
    r"-n(?P<sample_count>[0-9]+)-r(?P<sample_rate>[0-9]+)-c(?P<channels>[0-9]+)"
    r"-(?P<sha256>[0-9a-f]{64})\.pcm$"
)


@dataclass(frozen=True)
class ChunkMetadata:
    meeting_id: str
    epoch: int
    sequence: int
    sample_start: int
    sample_count: int
    sample_rate: int
    channels: int
    encoding: str
    sha256: str

    def validate(self, body: bytes) -> None:
        try:
            parsed_id = uuid.UUID(self.meeting_id)
        except ValueError as exc:
            raise ValueError("meeting id must be a UUID") from exc
        if str(parsed_id) != self.meeting_id:
            raise ValueError("meeting id must use canonical UUID form")
        if self.epoch < 1 or self.sequence < 0:
            raise ValueError("epoch and sequence are outside their valid range")
        if self.sample_start < 0 or self.sample_count < 1:
            raise ValueError("sample range is outside its valid range")
        if self.sample_rate != 16_000 or self.channels != 1:
            raise ValueError("audio must be 16 kHz mono")
        if self.encoding != "pcm_s16le":
            raise ValueError("audio encoding must be pcm_s16le")
        if len(body) != self.sample_count * 2:
            raise ValueError("PCM byte length does not match sample count")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 must be lowercase hexadecimal")
        if hashlib.sha256(body).hexdigest() != self.sha256:
            raise ValueError("audio body does not match sha256")

    def filename(self) -> str:
        return (
            f"e{self.epoch}-s{self.sequence}-a{self.sample_start}"
            f"-n{self.sample_count}-r{self.sample_rate}-c{self.channels}"
            f"-{self.sha256}.pcm"
        )

    @classmethod
    def from_filename(cls, meeting_id: str, filename: str) -> ChunkMetadata | None:
        match = _OBJECT_RE.fullmatch(filename)
        if match is None:
            return None
        values = match.groupdict()
        return cls(
            meeting_id=meeting_id,
            epoch=int(values["epoch"]),
            sequence=int(values["sequence"]),
            sample_start=int(values["sample_start"]),
            sample_count=int(values["sample_count"]),
            sample_rate=int(values["sample_rate"]),
            channels=int(values["channels"]),
            encoding="pcm_s16le",
            sha256=values["sha256"],
        )


class AudioSpool:
    """Write content-addressed PCM objects before their ledger transaction."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def object_path(self, metadata: ChunkMetadata) -> Path:
        return self.data_dir / "sessions" / metadata.meeting_id / "spool" / metadata.filename()

    def write(self, metadata: ChunkMetadata, body: bytes) -> Path:
        metadata.validate(body)
        target = self.object_path(metadata)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if self.verify(target, metadata.sha256):
                return target
            raise OSError("existing spool object failed its content hash")

        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            self._fsync_directory(target.parent)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return target

    def relative_path(self, path: Path) -> str:
        return path.relative_to(self.data_dir).as_posix()

    @staticmethod
    def verify(path: Path, expected_sha256: str) -> bool:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for block in iter(lambda: source.read(128 * 1024), b""):
                    digest.update(block)
        except OSError:
            return False
        return digest.hexdigest() == expected_sha256

    def iter_objects(self) -> list[tuple[ChunkMetadata, Path]]:
        sessions = self.data_dir / "sessions"
        if not sessions.is_dir():
            return []
        objects: list[tuple[ChunkMetadata, Path]] = []
        for meeting_dir in sessions.iterdir():
            spool_dir = meeting_dir / "spool"
            if not spool_dir.is_dir():
                continue
            for path in spool_dir.iterdir():
                if path.name.startswith(".") and path.name.endswith(".tmp"):
                    path.unlink(missing_ok=True)
                    continue
                metadata = ChunkMetadata.from_filename(meeting_dir.name, path.name)
                if metadata is not None and path.is_file():
                    objects.append((metadata, path))
        return objects

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
