"""Runtime provider abstraction.

Only llama.cpp is implemented in this task.  The contract is deliberately
small so an ONNX Runtime GenAI provider can be added later without changing the
module manager or the GUI: discover a local installation, describe the download
plan, and construct the object the existing ``InferenceAPI`` already accepts.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from giscode.ai_module.manifest import RuntimeBuild, RUNTIME_VERSION
from giscode.inference.runtime import SubprocessLlamaCppRuntime


class RuntimeProvider(Protocol):
    id: str
    label: str

    def find_installed(self, runtime_root: Path) -> Optional[str]:
        """Return the path to an already installed server executable."""

    def create_runtime(self, server_path: str, *, n_ctx: int, n_gpu_layers: int):
        """Build the runtime object consumed by ``InferenceAPI``."""


def _bundled_server() -> Optional[str]:
    """A runtime shipped beside the executable / PyInstaller bundle."""
    candidates: list[Path] = []
    for key in ("_MEIPASS",):
        base = os.environ.get(key)
        if base:
            candidates.append(Path(base) / "runtime")
    try:
        import sys

        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / "runtime")
    except Exception:
        pass
    for directory in candidates:
        for name in ("llama-server.exe", "llama-server"):
            path = directory / name
            if path.is_file():
                return str(path)
    return None


class LlamaCppProvider:
    id = "llama.cpp"
    label = "llama.cpp"

    def __init__(self, bundled: Optional[str] = None, version: str = RUNTIME_VERSION):
        self._bundled = bundled
        self.version = version

    def find_installed(self, runtime_root: Path) -> Optional[str]:
        bundled = self._bundled if self._bundled is not None else _bundled_server()
        if bundled and Path(bundled).is_file():
            return str(bundled)
        # Managed installs are version/backend directories with a marker.  The
        # newest marker wins; an unmarked directory is not trusted.
        found: list[tuple[str, str]] = []
        try:
            for marker in runtime_root.glob("*/install.json"):
                directory = marker.parent
                try:
                    relative = str(json.loads(marker.read_text(encoding="utf-8")).get("server") or "")
                except (OSError, ValueError):
                    relative = ""
                server = (directory / relative) if relative else (directory / "llama-server.exe")
                try:
                    resolved = server.resolve()
                    root = directory.resolve()
                    if root != resolved and root not in resolved.parents:
                        continue
                except OSError:
                    continue
                if resolved.is_file():
                    found.append((str(marker.stat().st_mtime_ns), str(resolved)))
        except OSError:
            return None
        if not found:
            return None
        found.sort(key=lambda item: item[0], reverse=True)
        return found[0][1]

    def create_runtime(self, server_path: str, *, n_ctx: int, n_gpu_layers: int):
        return SubprocessLlamaCppRuntime(
            server_path=server_path,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
        )

    def verify(self, server_path: str, *, timeout: float = 10.0) -> tuple[bool, str]:
        """Run ``--version`` once; a build that cannot start is not installed."""
        try:
            completed = subprocess.run(
                [server_path, "--version"],
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"could not start the runtime: {exc}"
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            first = output.strip().splitlines()[0] if output.strip() else "no output"
            return False, f"runtime exited {completed.returncode}: {first[:200]}"
        return True, output.strip().splitlines()[0] if output.strip() else "runtime is usable"


PROVIDERS: dict[str, RuntimeProvider] = {
    LlamaCppProvider.id: LlamaCppProvider(),
}


def get_provider(provider_id: str = "llama.cpp") -> RuntimeProvider:
    try:
        return PROVIDERS[provider_id]
    except KeyError as exc:
        raise KeyError(f"unknown runtime provider: {provider_id!r}") from exc
