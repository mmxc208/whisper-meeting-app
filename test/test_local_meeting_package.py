import hashlib
import json
import tarfile
from pathlib import Path

from scripts.package_local_meeting_app import build_archive


def test_release_archive_is_deterministic_and_installable(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    first_result = build_archive(repository=repository, output=first)
    build_archive(repository=repository, output=second)

    assert first.read_bytes() == second.read_bytes()
    assert first_result["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert all(
            not member.name.startswith("/") and ".." not in member.name for member in members
        )
        archive.extractall(tmp_path / "extracted", filter="data")
    app_root = tmp_path / "extracted" / str(first_result["root"])
    manifest = json.loads((app_root / "app.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "whisper-meeting"
    assert manifest["minKiroCrewVersion"] == "0.10.0"
    assert manifest["version"] == "0.3.5"
    assert manifest["permissions"]["api"] == [
        "/apps/whisper-meeting/api",
        "/api/spawn",
    ]
    assert manifest["platform"] == {
        "os": ["macos"],
        "arch": ["arm64"],
        "requiresDesktopApp": True,
        "installMode": "server",
    }
    assert manifest["agents"] == [
        "agents/whisper-meeting-note-taker.json",
        "agents/whisper-meeting-sketch-artist.json",
    ]
    for agent_path in manifest["agents"]:
        agent = json.loads((app_root / agent_path).read_text(encoding="utf-8"))
        assert agent["model"] == "auto"
        assert agent["tools"] == ["fs_read", "fs_write", "grep", "glob"]
    assert "KiroCrew's built-in Meetings" in (app_root / "ATTRIBUTION").read_text(encoding="utf-8")
    assert (app_root / "backend" / "server.py").is_file()
    assert (app_root / "requirements.txt").read_text(encoding="utf-8") == (
        "opencc-python-reimplemented>=0.1.7,<1\npywhispercpp>=1.5,<2\n"
    )
    assert (app_root / "ui" / "dist" / "index.mjs").is_file()
    assert not (app_root / "ui" / "src").exists()
