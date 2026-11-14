"""Is Ollama installed, and is it actually answering?

Two different questions, deliberately reported separately:

* **installed** - an ``ollama`` executable exists. That is a fact about the
  disk, not about the service.
* **service_available** - ``GET /api/version`` answered. That is the only
  evidence that a model can be run right now.

GisCode never installs Ollama for the user. Installing a system service needs
administrator rights and a decision, so this module supplies the official page
and concrete steps, and the service layer requires a recorded confirmation
before it will even open the page from inside the wizard.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from giscode.providers.base import DEFAULT_ENDPOINTS, OLLAMA, ProviderError
from giscode.providers.ollama import OllamaProvider

#: The official download page. No mirror, no bundler, no third-party build.
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"

#: Shown verbatim; the wizard does not paraphrase a security-relevant step.
INSTALL_STEPS = (
    "Download the installer from the official Ollama page.",
    "Run it and let it finish; Ollama installs for the current user and adds itself to the Start menu.",
    "Ollama starts in the tray. If it is not running, start 'Ollama' from the Start menu.",
    "Come back to GisCode and press Check again.",
)


def _windows_candidates() -> list[Path]:
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
    program_files = os.environ.get("ProgramFiles", "").strip()
    if program_files:
        candidates.append(Path(program_files) / "Ollama" / "ollama.exe")
    return candidates


#: Linux and macOS locations plus the Windows per-user install.
KNOWN_EXECUTABLES = (
    Path("/usr/local/bin/ollama"),
    Path("/usr/bin/ollama"),
    Path("/opt/homebrew/bin/ollama"),
)


@dataclass(frozen=True)
class RuntimeReport:
    installed: bool
    executable: str
    service_available: bool
    version: str
    endpoint: str
    models: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""
    action: str = ""

    @property
    def ready(self) -> bool:
        # The service answering is the fact that makes inference possible. A CLI
        # binary that is not on PATH must not turn a working Ollama into
        # "not ready" - it only matters later, when a pull needs the command.
        return self.service_available

    def as_dict(self) -> dict:
        return {
            "installed": self.installed,
            "executable": self.executable,
            "service_available": self.service_available,
            "ready": self.ready,
            "version": self.version,
            "endpoint": self.endpoint,
            "models": list(self.models),
            "detail": self.detail,
            "action": self.action,
        }

    def describe(self) -> str:
        if self.ready:
            suffix = f", {len(self.models)} model(s) installed" if self.models else ", no models yet"
            return f"Ollama {self.version or ''} is running at {self.endpoint}{suffix}".replace("  ", " ")
        if not self.installed:
            return "Ollama is not installed on this machine."
        return f"Ollama is installed but not answering at {self.endpoint}: {self.detail or 'no response'}"


def find_executable(which=None) -> str:
    """Locate the Ollama CLI without running it."""
    lookup = which or shutil.which
    try:
        found = lookup("ollama")
    except Exception:
        found = None
    if found:
        return str(found)
    for candidate in list(_windows_candidates()) + list(KNOWN_EXECUTABLES):
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return ""


def detect(provider=None, *, which=None, timeout: float = 3.0) -> RuntimeReport:
    """Ask the disk and the service, in that order, and report both answers."""
    executable = find_executable(which)
    endpoint = DEFAULT_ENDPOINTS[OLLAMA]
    if provider is None:
        try:
            provider = OllamaProvider(endpoint=endpoint, timeout=timeout)
        except Exception as exc:  # a broken endpoint value is a report, not a crash
            return RuntimeReport(
                installed=bool(executable),
                executable=executable,
                service_available=False,
                version="",
                endpoint=endpoint,
                detail=f"Ollama could not be queried: {exc}",
                action="Check that nothing has changed the loopback endpoint.",
            )

    try:
        status = provider.status()
    except Exception as exc:
        return RuntimeReport(
            installed=bool(executable),
            executable=executable,
            service_available=False,
            version="",
            endpoint=endpoint,
            detail=f"{type(exc).__name__}: {exc}",
            action=_action(bool(executable)),
        )

    models: tuple[str, ...] = ()
    if status.available:
        try:
            models = tuple(sorted(model.id for model in provider.list_models()))
        except (ProviderError, Exception):
            models = ()

    detail = "Ollama is running." if status.available else (status.detail or "no response")
    return RuntimeReport(
        installed=bool(executable),
        executable=executable,
        service_available=bool(status.available),
        version=str(status.version or ""),
        endpoint=str(status.endpoint or endpoint),
        models=models,
        detail=detail,
        action="" if status.available else _action(bool(executable)),
    )


def _action(installed: bool) -> str:
    if installed:
        return (
            "Ollama is installed but not answering. Start it from the Start menu "
            "(or run 'ollama serve'), then press Check again."
        )
    return (
        "Install Ollama from the official page, start it, then press Check again. "
        "GisCode does not install it for you."
    )


def install_instructions() -> dict:
    """The official route, with no automatic action attached."""
    return {
        "url": OLLAMA_DOWNLOAD_URL,
        "steps": list(INSTALL_STEPS),
        "automatic": False,
        "note": (
            "GisCode never downloads or runs an installer on your behalf. "
            "The installer comes from Ollama, and you run it yourself."
        ),
    }


def service_start_hint(*, os_name: Optional[str] = None) -> str:
    platform = os_name or os.name
    if platform == "nt":
        return "Start 'Ollama' from the Start menu, or run 'ollama serve' in a terminal."
    return "Run 'ollama serve' in a terminal, or start the ollama service."
