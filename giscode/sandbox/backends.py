import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from giscode.sandbox.types import SandboxBackend, SandboxConfig, SandboxRequest, SandboxResult, SandboxStatus


class SandboxError(Exception):
    pass


class SandboxUnavailableError(SandboxError):
    pass


class DockerBackend:
    def __init__(self, config: SandboxConfig):
        self._config = config
        self._active_containers: List[str] = []

    @property
    def available(self) -> bool:
        if not shutil.which("docker"):
            return False
        try:
            subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=5)
            return True
        except Exception:
            return False

    def has_suitable_image(self) -> bool:
        if not self._config.image:
            return False
        try:
            result = subprocess.run(["docker", "images", "-q", self._config.image], capture_output=True, text=True, check=True, timeout=5)
            return bool(result.stdout.strip())
        except Exception:
            return False

    def create_sandbox(self, workspace_path: str) -> SandboxResult:
        sandbox_id = f"giscode-sandbox-{int(time.time())}"
        result = SandboxResult(
            sandbox_id=sandbox_id,
            backend=SandboxBackend.DOCKER,
            command="",
            status=SandboxStatus.CREATING,
        )

        if not self.available:
            result.status = SandboxStatus.UNAVAILABLE
            result.error = "Docker is not available"
            return result

        if not self.has_suitable_image():
            result.status = SandboxStatus.UNAVAILABLE
            result.error = f"No suitable Docker image available: {self._config.image}"
            return result

        try:
            cmd = [
                "docker", "run", "-d",
                "--name", sandbox_id,
                "--network", "none" if not self._config.network_enabled else "bridge",
                "-v", f"{workspace_path}:/workspace",
                "-w", "/workspace",
            ]

            if self._config.memory_limit:
                cmd.extend(["--memory", self._config.memory_limit])
            if self._config.cpu_limit:
                cmd.extend(["--cpus", str(self._config.cpu_limit)])

            cmd.append(self._config.image)
            cmd.extend(["sleep", "infinity"])

            subprocess.run(cmd, capture_output=True, check=True, timeout=10)
            self._active_containers.append(sandbox_id)
            result.status = SandboxStatus.RUNNING
        except Exception as e:
            result.status = SandboxStatus.FAILED
            result.error = str(e)

        return result

    def execute(self, request: SandboxRequest, workspace_path: str) -> SandboxResult:
        sandbox_id = f"giscode-sandbox-{int(time.time())}"
        result = SandboxResult(
            sandbox_id=sandbox_id,
            backend=SandboxBackend.DOCKER,
            command=request.command,
            arguments=request.arguments,
            working_directory=request.working_directory,
            status=SandboxStatus.RUNNING,
        )

        if not self.available or not self.has_suitable_image():
            result.status = SandboxStatus.UNAVAILABLE
            result.error = "Docker backend unavailable"
            return result

        start_time = time.time()
        container = None

        try:
            network = "bridge" if request.network_enabled else "none"
            cmd = [
                "docker", "run", "--rm",
                "--network", network,
                "-v", f"{workspace_path}:/workspace",
                "-w", request.working_directory,
            ]

            if self._config.memory_limit:
                cmd.extend(["--memory", self._config.memory_limit])
            if self._config.cpu_limit:
                cmd.extend(["--cpus", str(self._config.cpu_limit)])

            cmd.append(self._config.image)
            cmd.extend([request.command] + list(request.arguments))

            timeout = request.timeout or self._config.default_timeout
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
            container = proc

            try:
                stdout_data, stderr_data = proc.communicate(timeout=timeout)
                elapsed = time.time() - start_time
                result.stdout = stdout_data.decode("utf-8", errors="replace")
                result.stderr = stderr_data.decode("utf-8", errors="replace")
                result.exit_code = proc.returncode
                result.elapsed_time = elapsed
                result.status = SandboxStatus.COMPLETED if proc.returncode == 0 else SandboxStatus.FAILED
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout_data, stderr_data = proc.communicate(timeout=2)
                result.stdout = (stdout_data or b"").decode("utf-8", errors="replace")
                result.stderr = (stderr_data or b"").decode("utf-8", errors="replace")
                result.exit_code = -1
                result.elapsed_time = time.time() - start_time
                result.timed_out = True
                result.status = SandboxStatus.TIMED_OUT
        except Exception as e:
            result.error = str(e)
            result.status = SandboxStatus.FAILED
            result.elapsed_time = time.time() - start_time
        finally:
            if container is not None:
                try:
                    container.kill()
                except Exception:
                    pass

        return result

    def destroy(self, sandbox_id: str) -> None:
        try:
            subprocess.run(["docker", "rm", "-f", sandbox_id], capture_output=True, timeout=5)
            if sandbox_id in self._active_containers:
                self._active_containers.remove(sandbox_id)
        except Exception:
            pass


class UnavailableBackend:
    def __init__(self, config: SandboxConfig):
        self._config = config

    @property
    def available(self) -> bool:
        return False

    def has_suitable_image(self) -> bool:
        return False

    def create_sandbox(self, workspace_path: str) -> SandboxResult:
        return SandboxResult(
            sandbox_id="",
            backend=SandboxBackend.UNAVAILABLE,
            command="",
            status=SandboxStatus.UNAVAILABLE,
            error="Sandbox backend is unavailable",
        )

    def execute(self, request: SandboxRequest, workspace_path: str) -> SandboxResult:
        return SandboxResult(
            sandbox_id="",
            backend=SandboxBackend.UNAVAILABLE,
            command=request.command,
            arguments=request.arguments,
            status=SandboxStatus.UNAVAILABLE,
            error="Sandbox backend is unavailable",
        )

    def destroy(self, sandbox_id: str) -> None:
        pass
