"""Pinned, app-owned Whisper model installation and verification."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

MODEL_ID = "large-v3-turbo"
MODEL_FILENAME = "ggml-large-v3-turbo.bin"
MODEL_BYTES = 1_624_555_275
MODEL_SHA256 = "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/" "ggml-large-v3-turbo.bin"
DOWNLOAD_STALL_SECONDS = 60.0
SELF_TEST_TIMEOUT_SECONDS = 300.0
_METAL_ACTIVE = re.compile(r"\busing\s+(?:MTL\d*|Metal)\s+backend\b", re.IGNORECASE)


def app_dependency_environment(
    data_dir: Path, base_environment: dict[str, str] | None = None
) -> dict[str, str]:
    """Give app-owned Python children the provisioned dependency tree."""

    environment = dict(os.environ if base_environment is None else base_environment)
    dependencies = data_dir / ".kirocrew-deps"
    if dependencies.is_dir():
        environment["PYTHONPATH"] = str(dependencies)
    return environment


def whisper_dependency_error() -> str | None:
    """Return setup guidance when the app-owned Whisper runtime is absent."""

    if importlib.util.find_spec("pywhispercpp") is None:
        return (
            "Whisper Meeting requires pywhispercpp>=1.5,<2. Reinstall the app "
            "dependencies, then retry setup; a Metal-enabled build is required."
        )
    if importlib.util.find_spec("_pywhispercpp") is None:
        return (
            "pywhispercpp is installed without its native _pywhispercpp extension. "
            "Reinstall the app dependencies with a Metal-enabled macOS arm64 build."
        )
    if importlib.util.find_spec("opencc") is None:
        return (
            "Whisper Meeting requires opencc-python-reimplemented>=0.1.7,<1 for "
            "Simplified Chinese output. Reinstall the app dependencies, then retry setup."
        )
    return None


class ModelStateWriter(Protocol):
    def model_state(self) -> dict[str, Any]: ...

    def set_model_state(
        self,
        *,
        status: str,
        artifact_id: str = MODEL_ID,
        sha256: str = MODEL_SHA256,
        error: str = "",
        self_tested: bool = False,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ModelArtifact:
    artifact_id: str = MODEL_ID
    filename: str = MODEL_FILENAME
    size_bytes: int = MODEL_BYTES
    sha256: str = MODEL_SHA256
    url: str = MODEL_URL


class ModelInstallError(RuntimeError):
    """The pinned model could not be installed or proved usable."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class ModelManager:
    """Own one immutable model copy below the app's preserved data directory."""

    def __init__(
        self,
        data_dir: Path,
        state: ModelStateWriter,
        *,
        artifact: ModelArtifact = ModelArtifact(),
        source_paths: tuple[Path, ...] | None = None,
        self_test: Callable[[Path], None] | None = None,
        dependency_probe: Callable[[], str | None] = whisper_dependency_error,
    ) -> None:
        self.data_dir = data_dir
        self.models_dir = data_dir / "models"
        self.model_path = self.models_dir / artifact.filename
        self.artifact = artifact
        self._state = state
        self._source_paths = source_paths or self._default_source_paths()
        self._self_test = self_test or self._run_self_test
        self._dependency_probe = dependency_probe
        self._lock = threading.Lock()
        self._install_thread: threading.Thread | None = None
        self._ready_callback: Callable[[], None] | None = None

    def set_ready_callback(self, callback: Callable[[], None]) -> None:
        self._ready_callback = callback

    def status(self) -> dict[str, Any]:
        value = self._state.model_state()
        value.update(
            {
                "model": self.artifact.artifact_id,
                "filename": self.artifact.filename,
                "size_bytes": self.artifact.size_bytes,
                "installed_path": str(self.model_path),
            }
        )
        return value

    def ready(self) -> bool:
        value = self._state.model_state()
        try:
            size_matches = (
                self.model_path.is_file()
                and self.model_path.stat().st_size == self.artifact.size_bytes
            )
        except OSError:
            size_matches = False
        return value["status"] == "ready" and size_matches

    def verify_for_load(self) -> Path:
        """Hash immediately before a native worker opens the app-owned artifact."""

        if not self._verified(self.model_path):
            self._state.set_model_state(
                status="failed",
                error="The app-owned model changed after installation.",
            )
            raise ModelInstallError("The app-owned model failed verification before load.")
        return self.model_path

    def reconcile(self) -> dict[str, Any]:
        """Refuse stale state after an app update or external file mutation."""

        value = self._state.model_state()
        dependency_error = self._dependency_probe()
        if dependency_error:
            return self._state.set_model_state(status="failed", error=dependency_error)
        if value["status"] == "ready" and not self._verified(self.model_path):
            return self._state.set_model_state(
                status="failed",
                error="The app-owned model is missing or failed its pinned hash.",
            )
        if value["status"] == "installing":
            return self._state.set_model_state(
                status="failed",
                error="Model installation was interrupted; retry installation.",
            )
        return value

    def start_install(self) -> dict[str, Any]:
        with self._lock:
            if self._install_thread is not None and self._install_thread.is_alive():
                return self.status()
            self._state.set_model_state(status="installing")
            self._install_thread = threading.Thread(
                target=self._install_background,
                name="whisper-meeting-model-install",
                daemon=True,
            )
            self._install_thread.start()
        return self.status()

    def install(self) -> dict[str, Any]:
        """Install synchronously; exposed for deterministic tests and setup tools."""

        dependency_error = self._dependency_probe()
        if dependency_error:
            self._state.set_model_state(status="failed", error=dependency_error)
            raise ModelInstallError(dependency_error)
        self._state.set_model_state(status="installing")
        try:
            self._install_verified_copy()
            self._self_test(self.model_path)
        except Exception as exc:
            self._state.set_model_state(status="failed", error=str(exc))
            raise
        state = self._state.set_model_state(status="ready", self_tested=True)
        if self._ready_callback is not None:
            self._ready_callback()
        return state

    def _install_background(self) -> None:
        try:
            self.install()
        except Exception:
            return

    def _install_verified_copy(self) -> None:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        if self._verified(self.model_path):
            return
        self.model_path.unlink(missing_ok=True)
        temporary = self.models_dir / f".{self.artifact.filename}.{uuid.uuid4().hex}.part"
        try:
            source = next((path for path in self._source_paths if self._verified(path)), None)
            if source is not None:
                shutil.copyfile(source, temporary)
            else:
                self._download(temporary)
            if not self._verified(temporary):
                raise ModelInstallError("The installed model failed its pinned size or SHA-256.")
            with temporary.open("rb") as staged:
                os.fsync(staged.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.model_path)
            self._fsync_directory(self.models_dir)
        finally:
            temporary.unlink(missing_ok=True)

    def _download(self, target: Path) -> None:
        request = urllib.request.Request(
            self.artifact.url, headers={"User-Agent": "Whisper-Meeting"}
        )
        digest = hashlib.sha256()
        written = 0
        with urllib.request.urlopen(request, timeout=DOWNLOAD_STALL_SECONDS) as response:
            with target.open("xb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
                    digest.update(block)
                    written += len(block)
                output.flush()
                os.fsync(output.fileno())
        if written != self.artifact.size_bytes or digest.hexdigest() != self.artifact.sha256:
            raise ModelInstallError("Downloaded model did not match the pinned artifact.")

    def _verified(self, path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size != self.artifact.size_bytes:
                return False
            return sha256_file(path) == self.artifact.sha256
        except OSError:
            return False

    def _default_source_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        configured = os.environ.get("WHISPER_MEETING_MODEL_SOURCE", "").strip()
        if configured:
            paths.append(Path(configured))
        crew_home = os.environ.get("KIROCREW_HOME", "").strip()
        if crew_home:
            paths.append(Path(crew_home) / "models" / "whisper" / self.artifact.filename)
        default = Path.home() / ".kiro" / "crew" / "models" / "whisper" / self.artifact.filename
        if default not in paths:
            paths.append(default)
        return tuple(paths)

    def _run_self_test(self, model_path: Path) -> None:
        worker = Path(__file__).with_name("recognition_worker.py")
        completed = subprocess.run(
            [sys.executable, str(worker), "--self-test", "--model", str(model_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=SELF_TEST_TIMEOUT_SECONDS,
            env=app_dependency_environment(self.data_dir),
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ModelInstallError("Whisper self-test returned invalid output.") from exc
        metal_active = _METAL_ACTIVE.search(completed.stderr) is not None
        if completed.returncode != 0 or result.get("ok") is not True:
            message = str(result.get("error") or "Whisper self-test failed.")
            raise ModelInstallError(message)
        if not metal_active:
            raise ModelInstallError(
                "Whisper loaded without proof that the Metal backend is active."
            )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
