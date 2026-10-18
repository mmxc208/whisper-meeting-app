import hashlib
from pathlib import Path

import pytest

from backend.model import (
    ModelArtifact,
    ModelInstallError,
    ModelManager,
    app_dependency_environment,
    whisper_dependency_error,
)
from backend.store import MeetingStore


def _manager(tmp_path: Path, source: Path, body: bytes) -> tuple[MeetingStore, ModelManager]:
    store = MeetingStore(tmp_path / "data", min_free_bytes=0)
    store.initialize()
    artifact = ModelArtifact(
        artifact_id="test-model",
        filename="ggml-test.bin",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        url="https://invalid.example/model",
    )
    manager = ModelManager(
        store.data_dir,
        store,
        artifact=artifact,
        source_paths=(source,),
        self_test=lambda _path: None,
        dependency_probe=lambda: None,
    )
    return store, manager


def test_install_copies_verified_model_into_app_owned_data(tmp_path: Path) -> None:
    source = tmp_path / "shared-model.bin"
    source.write_bytes(b"pinned model bytes")
    _store, manager = _manager(tmp_path, source, source.read_bytes())

    state = manager.install()
    source.unlink()

    assert state["status"] == "ready"
    assert manager.ready() is True
    assert manager.model_path.read_bytes() == b"pinned model bytes"


def test_install_refuses_source_with_wrong_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "shared-model.bin"
    source.write_bytes(b"wrong")
    _store, manager = _manager(tmp_path, source, b"expected")

    def refuse_download(_target: Path) -> None:
        raise ModelInstallError("no verified local source")

    monkeypatch.setattr(manager, "_download", refuse_download)
    with pytest.raises(ModelInstallError, match="no verified local source"):
        manager.install()

    assert manager.status()["status"] == "failed"


def test_reconcile_refuses_ready_state_after_model_mutation(tmp_path: Path) -> None:
    source = tmp_path / "shared-model.bin"
    source.write_bytes(b"pinned")
    _store, manager = _manager(tmp_path, source, source.read_bytes())
    manager.install()
    manager.model_path.write_bytes(b"changed")

    state = manager.reconcile()

    assert state["status"] == "failed"
    assert "pinned hash" in state["error"]


def test_worker_load_rehashes_same_size_model_mutation(tmp_path: Path) -> None:
    source = tmp_path / "shared-model.bin"
    source.write_bytes(b"pinned")
    _store, manager = _manager(tmp_path, source, source.read_bytes())
    manager.install()
    manager.model_path.write_bytes(b"tamper")

    assert manager.ready() is True
    with pytest.raises(ModelInstallError, match="failed verification"):
        manager.verify_for_load()
    assert manager.status()["status"] == "failed"


def test_missing_runtime_dependency_fails_setup_with_install_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MeetingStore(tmp_path / "data", min_free_bytes=0)
    store.initialize()
    monkeypatch.setattr("backend.model.importlib.util.find_spec", lambda _name: None)
    error = whisper_dependency_error()
    assert error is not None
    manager = ModelManager(store.data_dir, store, dependency_probe=lambda: error)

    state = manager.reconcile()

    assert state["status"] == "failed"
    assert "pywhispercpp>=1.5,<2" in state["error"]
    with pytest.raises(ModelInstallError, match="Metal-enabled"):
        manager.install()


def test_missing_opencc_dependency_fails_setup_with_install_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available = {"pywhispercpp", "_pywhispercpp"}
    monkeypatch.setattr(
        "backend.model.importlib.util.find_spec",
        lambda name: object() if name in available else None,
    )

    error = whisper_dependency_error()

    assert error is not None
    assert "opencc-python-reimplemented>=0.1.7,<1" in error
    assert "Simplified Chinese" in error


def test_app_dependency_environment_replaces_inherited_pythonpath(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    dependencies = data_dir / ".kirocrew-deps"
    dependencies.mkdir(parents=True)

    environment = app_dependency_environment(
        data_dir, {"PYTHONPATH": "/wrong/runtime", "PRESERVE_ME": "yes"}
    )

    assert environment["PYTHONPATH"] == str(dependencies)
    assert environment["PRESERVE_ME"] == "yes"
