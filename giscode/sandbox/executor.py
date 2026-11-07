import os
import time
from pathlib import Path
from typing import Optional

from giscode.sandbox.backends import DockerBackend, UnavailableBackend
from giscode.sandbox.types import SandboxBackend, SandboxConfig, SandboxRequest, SandboxResult, SandboxStatus
from giscode.workspace.workspace import Workspace


class SandboxExecutor:
    def __init__(self, workspace: Workspace, config: Optional[SandboxConfig] = None):
        self._workspace = workspace
        self._config = config or SandboxConfig()
        self._backend = self._create_backend()
        self._active_sandbox: Optional[str] = None

    def _create_backend(self):
        if self._config.backend == SandboxBackend.DOCKER:
            backend = DockerBackend(self._config)
            if backend.available and backend.has_suitable_image():
                return backend
        return UnavailableBackend(self._config)

    @property
    def backend(self) -> SandboxBackend:
        return self._backend.available and SandboxBackend.DOCKER or SandboxBackend.UNAVAILABLE

    @property
    def available(self) -> bool:
        if isinstance(self._backend, DockerBackend):
            return self._backend.available and self._backend.has_suitable_image()
        return False

    def create_sandbox(self) -> SandboxResult:
        result = self._backend.create_sandbox(str(self._workspace.root_path))
        if result.status == SandboxStatus.RUNNING:
            self._active_sandbox = result.sandbox_id
        return result

    def execute(self, request: SandboxRequest) -> SandboxResult:
        result = self._backend.execute(request, str(self._workspace.root_path))
        return result

    def destroy(self) -> Optional[SandboxResult]:
        if self._active_sandbox is None:
            return None
        self._backend.destroy(self._active_sandbox)
        result = SandboxResult(
            sandbox_id=self._active_sandbox,
            backend=self.backend,
            command="",
            status=SandboxStatus.DESTROYED,
        )
        self._active_sandbox = None
        return result

    def get_status(self) -> SandboxStatus:
        if self._active_sandbox is None:
            return SandboxStatus.IDLE
        return SandboxStatus.RUNNING
