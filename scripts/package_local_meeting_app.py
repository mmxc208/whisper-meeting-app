#!/usr/bin/env python3
"""Build a deterministic standalone Whisper Meeting source archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

RUNTIME_PATTERNS = (
    "app.json",
    "requirements.txt",
    "backend/*.py",
    "ui/dist/index.mjs",
    "ui/dist/whisper-meeting-worklet.js",
)


def runtime_files(app_dir: Path) -> list[Path]:
    files: set[Path] = set()
    for pattern in RUNTIME_PATTERNS:
        files.update(path for path in app_dir.glob(pattern) if path.is_file())
    expected = {
        app_dir / "app.json",
        app_dir / "requirements.txt",
        app_dir / "ui" / "dist" / "index.mjs",
        app_dir / "ui" / "dist" / "whisper-meeting-worklet.js",
    }
    missing = sorted(str(path.relative_to(app_dir)) for path in expected if not path.is_file())
    if missing:
        raise RuntimeError(f"runtime package is missing: {', '.join(missing)}")
    if any(path.is_symlink() for path in files):
        raise RuntimeError("runtime package cannot contain symlinks")
    return sorted(files, key=lambda path: path.relative_to(app_dir).as_posix())


def build_archive(*, repository: Path, output: Path) -> dict[str, object]:
    app_dir = repository
    manifest = json.loads((app_dir / "app.json").read_text(encoding="utf-8"))
    root = f"{manifest['name']}-{manifest['version']}"
    entries = runtime_files(app_dir)
    license_path = repository / "LICENSE"
    if not license_path.is_file():
        raise RuntimeError("repository LICENSE is missing")

    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for source in [*entries, license_path]:
            relative = (
                source.relative_to(app_dir).as_posix() if source != license_path else "LICENSE"
            )
            body = source.read_bytes()
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(body)
            info.mode = 0o755 if relative == "backend/server.py" else 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(body))

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
            compressed.write(tar_buffer.getvalue())
    body = output.read_bytes()
    return {
        "path": str(output),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "root": root,
        "files": len(entries) + 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parent.parent
    print(json.dumps(build_archive(repository=repository, output=args.output), indent=2))


if __name__ == "__main__":
    main()
