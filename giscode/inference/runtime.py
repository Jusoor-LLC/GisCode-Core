import json
import logging
import math
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

from giscode.inference import backend_selection
from giscode.inference.errors import (
    InferenceError,
    LoadFailureError,
    RequestCancelledError,
    UnloadFailureError,
)
from giscode.inference.interfaces import ModelDiscovery
from giscode.inference.lifecycle import ModelInfo, ModelState


#: Fixed install folders. LM Studio's own backends folder is *not* here: it
#: lives under a user profile and is resolved per call (see
#: ``_known_llama_server_paths``) instead of being snapshotted at import time.
_KNOWN_LLAMA_SERVER_PATHS = [
    Path("C:/Program Files/llama.cpp/bin"),
    Path("C:/llama.cpp/bin"),
]


def _user_profile_roots() -> list[Path]:
    """Every plausible user-profile folder, de-duplicated, best first.

    ``Path.home()`` is only as good as ``USERPROFILE``/``HOME``/``HOMEDRIVE``.
    An isolated harness that redirects those to a temp folder makes
    ``~/.lmstudio`` point at an empty directory, and LM Studio's CUDA build -
    the only runtime that can serve chat completions here - disappears with
    it. ``%LOCALAPPDATA%``/``%APPDATA%`` give a second, independent view of the
    same profile (they are ``<profile>\\AppData\\...``), so a redirected
    environment can no longer hide an installed backend or model folder.
    """
    roots: list[Path] = []
    seen: set[str] = set()

    def add(value) -> None:
        if not value:
            return
        try:
            path = Path(str(value)).expanduser()
        except (OSError, ValueError):
            return
        key = os.path.normcase(str(path))
        if key in seen:
            return
        seen.add(key)
        roots.append(path)

    add(os.environ.get("USERPROFILE"))
    add(Path.home())
    drive = os.environ.get("HOMEDRIVE", "").strip()
    home_path = os.environ.get("HOMEPATH", "").strip()
    if drive and home_path:
        add(drive + home_path)
    for variable in ("LOCALAPPDATA", "APPDATA"):
        raw = os.environ.get(variable, "").strip()
        if raw:
            # <profile>\AppData\<Local|Roaming> -> <profile>
            add(Path(raw).parent.parent)
    return roots


def _known_llama_server_paths() -> list[tuple[Path, str]]:
    """``(folder, source label)`` for every folder that may hold a build."""
    found: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for root in _user_profile_roots():
        candidate = root / ".lmstudio" / "extensions" / "backends"
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            found.append((candidate, "lmstudio"))
    for base in _KNOWN_LLAMA_SERVER_PATHS:
        key = os.path.normcase(str(base))
        if key not in seen:
            seen.add(key)
            found.append((Path(base), "fixed"))
    return found

LLAMA_SERVER_PATH_ENV = "GISCODE_LLAMA_SERVER_PATH"
LLAMA_READY_TIMEOUT_ENV = "GISCODE_LLAMA_READY_TIMEOUT"
DEFAULT_LLAMA_READY_TIMEOUT = 60.0
MAX_LLAMA_READY_TIMEOUT = 600.0
LLAMA_READY_POLL_INTERVAL = 0.25
LLAMA_COMPLETION_PROBE_PROMPT = "hi"
LLAMA_COMPLETION_PROBE_MAX_TOKENS = 1

logger = logging.getLogger(__name__)

_OWNED_SERVER_PIDS: set[int] = set()

#: Resolved once per session; see ``_resolve_backend_choice``.
_LAST_BACKEND_CHOICE: Optional["backend_selection.BackendChoice"] = None


def _kill_process_tree(pid: int, timeout: float = 10.0) -> bool:
    """Kill a process tree on Windows via taskkill /T. Only for PIDs the app owns."""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except Exception:
        return False


def report_foreign_servers() -> list:
    """Report llama-server processes this app does NOT own. Never kills them."""
    import psutil
    foreign = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            name = proc.info.get("name") or ""
            if "llama-server" not in name.lower():
                continue
            pid = int(proc.info["pid"])
            if pid in _OWNED_SERVER_PIDS:
                continue
            memory = proc.info.get("memory_info")
            foreign.append({
                "pid": pid,
                "name": name,
                "cmdline": " ".join(proc.info.get("cmdline") or []),
                "rss_bytes": int(getattr(memory, "rss", 0) or 0),
                "owned": False,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return foreign


def resolve_llama_ready_timeout(value: Optional[Union[str, float]] = None) -> float:
    raw = os.environ.get(LLAMA_READY_TIMEOUT_ENV) if value is None else value
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LLAMA_READY_TIMEOUT
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %.1fs", LLAMA_READY_TIMEOUT_ENV, raw, DEFAULT_LLAMA_READY_TIMEOUT)
        return DEFAULT_LLAMA_READY_TIMEOUT
    if not math.isfinite(timeout) or timeout <= 0:
        logger.warning("Invalid %s=%r; using %.1fs", LLAMA_READY_TIMEOUT_ENV, raw, DEFAULT_LLAMA_READY_TIMEOUT)
        return DEFAULT_LLAMA_READY_TIMEOUT
    if timeout > MAX_LLAMA_READY_TIMEOUT:
        logger.warning("Capping %s=%.1fs at %.1fs", LLAMA_READY_TIMEOUT_ENV, timeout, MAX_LLAMA_READY_TIMEOUT)
        return MAX_LLAMA_READY_TIMEOUT
    return timeout


def estimate_ready_timeout(model_path: str) -> float:
    """Model-size-dependent ready timeout: 120s + 40s per GiB, floor 180s."""
    try:
        size_bytes = Path(model_path).stat().st_size
    except OSError:
        return 180.0
    gib = size_bytes / (1024 ** 3)
    return max(180.0, 120.0 + 40.0 * gib)


def _validate_llama_server_path(value: str) -> Optional[str]:
    candidate = Path(value).expanduser()
    if not candidate.is_file():
        return None
    if os.name == "nt" and candidate.suffix.lower() != ".exe":
        return None
    return str(candidate.resolve())


def _settings_llama_server_path() -> str:
    """Optional pinned build from config.json (``inference.llama_server_path``)."""
    try:
        from giscode.config.manager import ConfigManager

        value = ConfigManager().load().get("inference", {}).get("llama_server_path", "")
        return str(value or "").strip()
    except Exception:
        return ""


def _bundled_llama_dirs() -> list[Path]:
    """Folders next to (or inside) the packaged EXE that may carry a build."""
    dirs: list[Path] = []
    try:
        dirs.append(Path(sys.executable).parent / "llama")
    except Exception:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "llama")
    return dirs


def _sorted_exes(base: Path) -> list[Path]:
    """All ``llama-server.exe`` under ``base``, in a stable order."""
    try:
        if not base.exists() or not base.is_dir():
            return []
        return sorted(base.rglob("llama-server.exe"), key=lambda p: str(p).lower())
    except OSError:
        return []


def _probe_version(path: str) -> "backend_selection.ProbeResult":
    """Run ``--version`` once per session; only exit 0 counts as usable."""
    return backend_selection.probe_version_cached(path)


def _probe_devices(path: str) -> "backend_selection.ProbeResult":
    """Confirm the chosen CUDA/Vulkan build can actually see a device."""
    return backend_selection.probe_devices_cached(path)


def _iter_backend_candidates() -> list["backend_selection.Candidate"]:
    """Every candidate, in the documented order (pinned sources first)."""
    candidates: list[backend_selection.Candidate] = []

    configured = os.environ.get(LLAMA_SERVER_PATH_ENV, "").strip()
    if configured:
        resolved = _validate_llama_server_path(configured)
        if resolved:
            candidates.append(backend_selection.Candidate(resolved, "env", True))
        else:
            logger.warning("Ignoring %s=%r: not an existing .exe", LLAMA_SERVER_PATH_ENV, configured)

    setting = _settings_llama_server_path()
    if setting:
        resolved = _validate_llama_server_path(setting)
        if resolved:
            candidates.append(backend_selection.Candidate(resolved, "setting", True))
        else:
            logger.warning("Ignoring inference.llama_server_path=%r: not an existing .exe", setting)

    for base in _bundled_llama_dirs():
        for found in _sorted_exes(base):
            candidates.append(backend_selection.Candidate(str(found), "bundled", False))

    found = shutil.which("llama-server")
    if found:
        candidates.append(backend_selection.Candidate(found, "path", False))

    for base, source in _known_llama_server_paths():
        for found in _sorted_exes(base):
            candidates.append(backend_selection.Candidate(str(found), source, False))
    return candidates


def _detect_hardware() -> tuple[bool, bool]:
    """(has NVIDIA GPU, has a Vulkan loader) - read-only probes."""
    has_nvidia = False
    try:
        from giscode.inference.resource_budget import SystemResourceDetector

        has_nvidia = bool(SystemResourceDetector().get_system_resources().gpu_name)
    except Exception:
        has_nvidia = False
    return has_nvidia, backend_selection.vulkan_available()


def gpu_required() -> bool:
    """``inference.require_gpu``: refuse CPU-only execution instead of degrading.

    Default is False, so nothing about a normal install changes. When True a
    CPU build is not used and a model that would have to load with zero GPU
    layers is refused rather than silently run on the CPU.
    """
    try:
        from giscode.config.manager import ConfigManager

        return bool(ConfigManager().load().get("inference", {}).get("require_gpu", False))
    except Exception:
        return False


def _resolve_backend_choice(force: bool = False) -> "backend_selection.BackendChoice":
    """Resolve (once per session) and log the build GisCode will run."""
    global _LAST_BACKEND_CHOICE
    if _LAST_BACKEND_CHOICE is not None and not force:
        return _LAST_BACKEND_CHOICE

    candidates = _iter_backend_candidates()
    has_nvidia = has_vulkan = False
    if any(not candidate.pinned for candidate in candidates):
        has_nvidia, has_vulkan = _detect_hardware()

    choice = backend_selection.select_backend(
        candidates,
        probe=_probe_version,
        has_nvidia=has_nvidia,
        has_vulkan=has_vulkan,
        confirm_devices=True,
        device_probe=_probe_devices,
    )
    if choice.path and choice.family == "cpu" and gpu_required():
        # Never degrade a GPU-required machine to the CPU build silently.
        reason = (
            "GPU-required mode (inference.require_gpu) is on and no CUDA or Vulkan "
            f"build could be started; the only working build is the CPU build at "
            f"{choice.path}"
        )
        choice = backend_selection.BackendChoice(
            path=None, rejected=choice.rejected, reason=reason
        )

    for rejected_path, rejected_reason in choice.rejected:
        logger.info("llama-server candidate rejected: %s (%s)", rejected_path, rejected_reason)
    if choice.path:
        logger.info("%s", backend_selection.describe_choice(choice))
        if choice.devices:
            logger.info("llama-server devices: %s", "; ".join(choice.devices))
            if choice.family == "vulkan" and len(choice.devices) > 1:
                logger.info(
                    "llama-server Vulkan device: %s",
                    backend_selection.vulkan_device_note(choice.devices),
                )
    else:
        logger.warning("%s", backend_selection.describe_choice(choice))
    _LAST_BACKEND_CHOICE = choice
    return choice


def describe_backend_choice() -> str:
    """One plain sentence about the chosen build ('' until it is resolved)."""
    if _LAST_BACKEND_CHOICE is None:
        return ""
    return backend_selection.describe_choice(_LAST_BACKEND_CHOICE)


def explain_cpu_only_load(gpu_layers: int, model_name: str = "") -> str:
    """Plain-words note when a GPU exists but the model loaded on the CPU.

    Never signals, stops or restarts another process: it only reads memory
    numbers and the foreign-server list.
    """
    try:
        layers = int(gpu_layers or 0)
    except (TypeError, ValueError):
        layers = 0
    if layers > 0:
        return ""
    try:
        from giscode.inference.resource_budget import SystemResourceDetector

        resources = SystemResourceDetector().get_system_resources()
    except Exception:
        return ""
    return backend_selection.explain_cpu_only_load(
        layers,
        resources.gpu_name,
        int(resources.available_vram_bytes or 0),
        int(resources.total_vram_bytes or 0),
        report_foreign_servers(),
    )


def _find_llama_server_exe() -> Optional[str]:
    """The build GisCode will run, or None when nothing usable was found."""
    return _resolve_backend_choice().path


def _pick_free_port(preferred: int = 8080) -> int:
    for port in [preferred] + list(range(8081, 8181)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


@dataclass
class GenerationRequest:
    prompt: str
    max_tokens: int = 512
    temperature: float = 0.7
    stop: Optional[list[str]] = None
    request_id: Optional[str] = None


@dataclass
class GenerationResponse:
    text: str
    request_id: Optional[str] = None
    tokens_generated: int = 0
    finish_reason: str = "stop"
    # Populated by provider-backed backends so a caller can attribute the
    # answer without consulting global state. The built-in runtime leaves
    # these empty, which is honest: its provider is llama.cpp.
    provider: str = ""
    model: str = ""


@dataclass
class ChatMessage:
    role: str
    content: Union[str, list[dict]]

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatRequest:
    messages: Sequence[ChatMessage]
    max_tokens: int = 512
    temperature: float = 0.7
    stop: Optional[list[str]] = None
    request_id: Optional[str] = None
    images: Optional[list[str]] = None
    # "off" | "low" | "default" - mirrors llama-server's --reasoning /
    # --reasoning-budget options; None sends no override.
    reasoning_budget: Optional[str] = None

    def to_payload(self) -> dict:
        payload: dict = {
            "messages": [m.to_dict() for m in self.messages],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
            "stop": list(self.stop) if self.stop else [],
        }
        if self.reasoning_budget:
            if self.reasoning_budget == "off":
                # `"reasoning": "off"` alone proved ineffective for Qwen3 (the
                # model still emitted reasoning and returned empty content).
                # The template flag is the reliable switch; send both.
                payload["reasoning"] = "off"
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            elif self.reasoning_budget == "low":
                payload["reasoning_budget"] = 512
            # "default" sends no override: the template's own reasoning applies.
        if self.images:
            image_data = []
            for path in self.images:
                encoded = self._image_as_data_uri(path)
                if encoded:
                    image_data.append(encoded)
            if image_data:
                payload["images"] = image_data
        return payload

    @staticmethod
    def _image_as_data_uri(path: str) -> Optional[str]:
        import base64

        from pathlib import Path

        try:
            p = Path(path)
            if not p.exists() or not p.is_file():
                return None
            suffix = p.suffix.lower().lstrip(".")
            mime = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
                "bmp": "image/bmp",
                "gif": "image/gif",
            }.get(suffix)
            if mime is None:
                return None
            with open(p, "rb") as fh:
                raw = fh.read()
            return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        except Exception:
            return None


@dataclass
class ChatResponse:
    text: str
    request_id: Optional[str] = None
    tokens_generated: int = 0
    finish_reason: str = "stop"
    # See GenerationResponse: provider-backed answers name their source.
    provider: str = ""
    model: str = ""


class LlamaCppRuntime(ABC):
    #: True only for runtimes that actually serve ``/v1/chat/completions``.
    #: The inherited ``chat_generate`` below exists but raises, so ``hasattr``
    #: answers "yes" for every runtime and cannot be used to choose one.
    supports_chat_generate = False

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    @abstractmethod
    def start_server(self, model_path: str, port: int = 8080, n_ctx: Optional[int] = None, n_threads: Optional[int] = None, n_gpu_layers: Optional[int] = None, mmproj_path: Optional[str] = None, cpu_moe: bool = False) -> bool:
        pass

    @abstractmethod
    def stop_server(self) -> None:
        pass

    @abstractmethod
    def generate(self, request: GenerationRequest, port: int = 8080, cancel_event: Optional[threading.Event] = None) -> GenerationResponse:
        pass

    @abstractmethod
    def is_running(self, port: int = 8080) -> bool:
        pass

    def chat_generate(self, request: ChatRequest, port: int = 8080, cancel_event: Optional[threading.Event] = None) -> ChatResponse:
        raise InferenceError(
            f"{type(self).__name__} does not implement chat_generate (/v1/chat/completions)"
        )


class SubprocessLlamaCppRuntime(LlamaCppRuntime):
    #: llama-server speaks the chat-completions API the coding agent needs.
    supports_chat_generate = True
    DEFAULT_READY_TIMEOUT = DEFAULT_LLAMA_READY_TIMEOUT
    DEFAULT_N_CTX = 16384

    def __init__(self, server_path: Optional[str] = None, n_ctx: int = DEFAULT_N_CTX, n_threads: int = 4, n_gpu_layers: int = 0, ready_timeout: Optional[float] = None):
        self._process: Optional[subprocess.Popen] = None
        self._server_port = 8080
        self._server_path = server_path or _find_llama_server_exe()
        self._backend_family = (
            backend_selection.family_of(self._server_path) if self._server_path else ""
        )
        self._n_ctx = n_ctx
        self._n_threads = n_threads
        self._n_gpu_layers = n_gpu_layers
        self._ready_timeout = resolve_llama_ready_timeout(ready_timeout)
        self._model_path: Optional[str] = None
        self._model_ready = False

    def is_available(self) -> bool:
        return self._server_path is not None and Path(self._server_path).exists()

    def get_server_path(self) -> Optional[str]:
        return self._server_path

    def get_backend_family(self) -> str:
        """``cuda12`` | ``cuda`` | ``vulkan`` | ``cpu`` | ``unknown`` | ``""``."""
        if not self._server_path:
            return ""
        return backend_selection.family_of(self._server_path)

    def get_model_status(self) -> Optional[ModelInfo]:
        """Query the server for currently loaded model."""
        try:
            import urllib.request
            import json
            from giscode.inference.lifecycle import ModelInfo, ModelState
            # Fast path: when the subprocess we own is not running (or was
            # never started) there is no model to probe. Avoids a multi-second
            # HTTP connect timeout on every status poll during startup while
            # llama-server is still being autoloaded on its background thread.
            if self._process is None or self._process.poll() is not None:
                return None
            url = f"http://127.0.0.1:{self._server_port}/v1/models"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                models = data.get("models", [])
                if models:
                    model_data = models[0]
                    model_path = model_data.get("name", "")
                    if model_path:
                        return ModelInfo(
                            name=Path(model_path).stem,
                            path=model_path,
                            size_bytes=0,  # Unknown from API
                            state=ModelState.READY,
                        )
        except Exception:
            pass
        return None

    def _kill_port(self, port: int) -> None:
        """Kill any process listening on the given port."""
        try:
            import subprocess
            # Find PID using netstat
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        pid = parts[-1]
                        try:
                            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5)
                        except Exception:
                            pass
        except Exception:
            pass

    def start_server(self, model_path: str, port: int = 8080, n_ctx: Optional[int] = None, n_threads: Optional[int] = None, n_gpu_layers: Optional[int] = None, mmproj_path: Optional[str] = None, cpu_moe: bool = False) -> bool:
        if self._server_path is None:
            return False
        if self._process is not None and self._process.poll() is None:
            self.stop_server()

        self._server_port = _pick_free_port(port)
        ctx = n_ctx if n_ctx else self._n_ctx
        threads = n_threads if n_threads else self._n_threads
        gpu_layers = n_gpu_layers if n_gpu_layers is not None else self._n_gpu_layers
        cmd = [
            self._server_path,
            "--model", model_path,
            "--port", str(self._server_port),
            "--host", "127.0.0.1",
            "--ctx-size", str(ctx),
            "--threads", str(threads),
            "--n-gpu-layers", str(gpu_layers),
        ]
        if cpu_moe:
            cmd.append("--cpu-moe")
        if mmproj_path is not None:
            cmd.extend(["--mmproj", mmproj_path])

        # The build's own folder and its sibling ``vendor`` DLL folders must
        # be on PATH: LM Studio keeps cublas/cudart/vulkan-1 there, not inside
        # the build directory (which is where this used to look).
        env = backend_selection.server_env(self._server_path, self._backend_family)

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                env=env,
            )
            _OWNED_SERVER_PIDS.add(self._process.pid)
        except Exception:
            self._process = None
            return False

        timeout = self._ready_timeout_for(model_path)
        if not self._wait_ready(timeout):
            self.stop_server()
            return False
        self._model_path = model_path
        self._model_ready = True
        return True

    def _ready_timeout_for(self, model_path: str) -> float:
        """Env override wins; otherwise model-size-dependent with 180s floor."""
        env = resolve_llama_ready_timeout(None)
        if env != DEFAULT_LLAMA_READY_TIMEOUT:
            return env
        return estimate_ready_timeout(model_path)

    def _health_url(self) -> str:
        return f"http://127.0.0.1:{self._server_port}/health"

    def _completions_url(self) -> str:
        return f"http://127.0.0.1:{self._server_port}/v1/completions"

    @staticmethod
    def _is_connection_refused(exc: BaseException) -> bool:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (ConnectionRefusedError, ConnectionResetError)):
            return True
        text = str(reason).lower()
        return "refused" in text or "forcibly closed" in text or "not established" in text

    @staticmethod
    def _is_read_timeout(exc: BaseException) -> bool:
        """True for a socket read timeout, including Windows' OSError form.

        On Windows a timed-out ``read1`` raises a plain ``OSError`` with the
        message "cannot read from timed out object" rather than
        ``socket.timeout``; both mean the server had nothing to send within the
        read window, which the idle reader treats as a wake-up, not a failure.
        """
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return True
        text = str(reason).lower()
        return "timed out" in text or "timeout" in text

    @staticmethod
    def _set_blocking(resp) -> None:
        """Put the response's socket into blocking mode.

        The streamed body is consumed on a background thread with no socket
        timeout so an idle gap never poisons the socket (see
        ``_is_read_timeout``); the caller paces itself on the event queue.
        """
        fp = getattr(resp, "fp", None)
        sock = None
        for candidate in (getattr(fp, "raw", None), fp):
            found = getattr(candidate, "_sock", None)
            if found is not None:
                sock = found
                break
        if sock is not None:
            try:
                sock.settimeout(None)
            except OSError:
                pass

    def _poll_health(self) -> bool:
        healthy, _refused = self._probe_health()
        return healthy

    def _probe_health(self) -> tuple[bool, bool]:
        """Return ``(healthy, connection_refused)``.

        A refused connection means nothing is listening on the port at all.
        That is a different situation from a server that answers but is not
        ready yet: llama-server binds its socket before it loads the weights,
        so a reachable-but-loading server still answers ``/health`` with 200
        while a refused connection means the server is gone.
        """
        try:
            with urllib.request.urlopen(self._health_url(), timeout=1.0) as resp:
                return resp.status == 200, False
        except urllib.error.HTTPError:
            # The server answered, so it is reachable; it is just not ready.
            return False, False
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as exc:
            return False, self._is_connection_refused(exc)

    def _probe_completion(self, timeout: float) -> bool:
        payload = json.dumps({
            "prompt": LLAMA_COMPLETION_PROBE_PROMPT,
            "max_tokens": LLAMA_COMPLETION_PROBE_MAX_TOKENS,
            "temperature": 0.0,
        }).encode("utf-8")
        req = urllib.request.Request(
            self._completions_url(),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as exc:
            logger.debug("completion probe got HTTP %s: %s", exc.code, exc.reason)
            return False
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError):
            return False

    def _wait_ready(self, timeout: float, *, fail_fast_on_refused: bool = False) -> bool:
        """Wait until llama-server both answers ``/health`` and serves a real
        one-token completion.

        llama-server binds its socket and answers ``/health`` with 200 while
        the weights are still loading, so a request sent at that moment fails
        with HTTP 503 ``{"error":{"message":"Loading model"}}``. Readiness
        therefore requires a successful completion probe, not just a healthy
        endpoint. Both the health poll and the probe honour ``timeout`` and
        retry on 503 and on connection refused.

        With ``fail_fast_on_refused`` the wait gives up as soon as the
        connection is refused: nothing is listening, so the server is gone
        rather than loading and waiting cannot help. The load path leaves it
        off, because there the socket is not bound yet for the first moments
        after the process is started.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._process is not None and self._process.poll() is not None:
                return False
            healthy, refused = self._probe_health()
            if not healthy:
                if fail_fast_on_refused and refused:
                    raise InferenceError(self._connection_refused_message())
                time.sleep(LLAMA_READY_POLL_INTERVAL)
                continue
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if self._probe_completion(remaining):
                return True
            time.sleep(LLAMA_READY_POLL_INTERVAL)
        return False

    def _connection_refused_message(self) -> str:
        """The error reported when nothing listens on the server port."""
        return (
            "llama-server chat request failed: connection refused "
            f"(nothing is listening on 127.0.0.1:{self._server_port})"
        )

    def _ensure_model_ready(self) -> None:
        """Gate the first request until a real completion has succeeded once.

        A request only reaches this gate once the server was seen running, so
        a refused connection here means the server is gone rather than still
        loading. That is reported like a failed chat request instead of
        waiting out the whole readiness timeout.
        """
        if self._model_ready:
            return
        if not self._wait_ready(self._ready_timeout, fail_fast_on_refused=True):
            raise InferenceError(
                f"llama-server model not ready after {self._ready_timeout:.0f}s: "
                "completion probe never returned a successful response"
            )
        self._model_ready = True

    def stop_server(self) -> None:
        proc = self._process
        if proc is not None:
            pid = proc.pid
            _OWNED_SERVER_PIDS.discard(pid)
            try:
                _kill_process_tree(pid)
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            finally:
                self._process = None
                self._model_path = None
        self._model_ready = False
        self._last_generation: dict = {}

    def _is_server_running(self) -> bool:
        """Check if server is running via health endpoint, fallback to process check."""
        # First try health endpoint
        try:
            with urllib.request.urlopen(self._health_url(), timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        # Fallback to process check
        return self._process is not None and self._process.poll() is None

    def generate(self, request: GenerationRequest, port: int = 8080, cancel_event: Optional[threading.Event] = None) -> GenerationResponse:
        if not self._is_server_running():
            raise InferenceError("llama-server is not running")
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Generation cancelled by user")
        use_port = self._server_port if port == 8080 else port
        self._ensure_model_ready()
        if cancel_event is None:
            return self._generate_sync(request, use_port)
        body = json.dumps({
            "prompt": request.prompt,
            "n_predict": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
            "stop": request.stop or [],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{use_port}/completion",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        pieces: list[str] = []
        stop_reason = "stop"
        tokens = 0
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw in resp:
                    if cancel_event.is_set():
                        raise RequestCancelledError("Generation cancelled by user")
                    line = raw.decode("utf-8", "replace").rstrip("\n").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    piece = chunk.get("content")
                    if isinstance(piece, str) and piece:
                        pieces.append(piece)
                    if chunk.get("stop"):
                        stop_reason = chunk.get("stop_type") or "stop"
                    tokens = int(chunk.get("tokens_predicted", 0) or tokens)
        except RequestCancelledError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if cancel_event.is_set():
                raise RequestCancelledError("Generation cancelled by user")
            raise InferenceError(f"llama-server request failed: {e}")
        return GenerationResponse(text="".join(pieces), request_id=request.request_id, tokens_generated=tokens, finish_reason=str(stop_reason))

    def _generate_sync(self, request: GenerationRequest, use_port: int) -> GenerationResponse:
        body = json.dumps({
            "prompt": request.prompt,
            "n_predict": request.max_tokens,
            "temperature": request.temperature,
            "stream": False,
            "stop": request.stop or [],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{use_port}/completion",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise InferenceError(f"llama-server request failed: {e}")
        text = payload.get("content") or payload.get("content", "") or ""
        if not text and "choices" in payload and payload["choices"]:
            text = payload["choices"][0].get("text", "")
        tokens = int(payload.get("tokens_evaluated", 0) or 0) + int(payload.get("tokens_predicted", 0) or 0)
        if not tokens and "usage" in payload:
            tokens = int(payload["usage"].get("completion_tokens", 0) or 0)
        stop_reason = payload.get("stop_type") or payload.get("finish_reason") or "stop"
        return GenerationResponse(text=text or "", request_id=request.request_id, tokens_generated=tokens, finish_reason=str(stop_reason))

    def chat_generate(self, request: ChatRequest, port: int = 8080, cancel_event: Optional[threading.Event] = None) -> ChatResponse:
        if not self._is_server_running():
            raise InferenceError("llama-server is not running")
        if not request.messages:
            raise InferenceError("chat_generate requires at least one message")
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Generation cancelled by user")
        use_port = self._server_port if port == 8080 else port
        self._ensure_model_ready()
        if cancel_event is None:
            return self._chat_generate_sync(request, use_port)
        pieces: list[str] = []
        stop_reason = "stop"
        tokens = 0
        for piece, reason, toks in self.chat_generate_stream(request, port=port, cancel_event=cancel_event):
            if piece:
                pieces.append(piece)
            elif reason:
                stop_reason = reason
                tokens = int(toks or 0)
        return ChatResponse(
            text="".join(pieces),
            request_id=request.request_id,
            tokens_generated=tokens,
            finish_reason=stop_reason,
        )

    def chat_generate_stream(
        self,
        request: ChatRequest,
        port: int = 8080,
        cancel_event: Optional[threading.Event] = None,
        read_timeout: float = 5.0,
    ):
        """Stream a chat completion as (piece, finish_reason, tokens) events.

        Every content delta is yielded as soon as it arrives and the final
        event carries the finish reason and completion token count (both
        None until then). Reads are chunk-paced at ``read_timeout`` so the
        caller can watch for inactivity and cancel via ``cancel_event``
        while the server is busy. Transport failures raise InferenceError;
        a set cancel_event raises RequestCancelledError.
        """
        if not request.messages:
            raise InferenceError("chat_generate requires at least one message")
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Generation cancelled by user")
        use_port = self._server_port if port == 8080 else port
        self._ensure_model_ready()
        payload_dict = request.to_payload()
        payload_dict["stream"] = True
        if os.environ.get("GISCODE_DEBUG_PAYLOAD"):
            logger.warning(
                "payload_debug stream reason_keys=%s msg_chars=%d max_tokens=%s",
                {k: payload_dict.get(k) for k in ("reasoning", "reasoning_budget", "chat_template_kwargs")},
                sum(len(str(m.get("content", ""))) for m in payload_dict.get("messages", [])),
                payload_dict.get("max_tokens"),
            )
        # Ask llama-server to include the usage block in the final stream chunk
        # so tokens_generated is read from the server, not guessed.
        payload_dict["stream_options"] = {"include_usage": True}
        body = json.dumps(payload_dict).encode("utf-8")
        chat_url = f"http://127.0.0.1:{use_port}/v1/chat/completions"
        retry_deadline = time.time() + self._ready_timeout
        started = time.time()
        content_len = 0
        reasoning_parts: list[str] = []
        try:
            while True:
                http_req = urllib.request.Request(
                    chat_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                stop_reason = "stop"
                tokens = 0
                try:
                    resp = urllib.request.urlopen(http_req, timeout=read_timeout)
                except urllib.error.HTTPError as e:
                    error_body = e.read().decode("utf-8", errors="replace") if e.fp else "no response body"
                    if cancel_event is not None and cancel_event.is_set():
                        raise RequestCancelledError("Generation cancelled by user")
                    if e.code == 503 and time.time() < retry_deadline:
                        logger.debug("streaming chat got HTTP 503; retrying until ready deadline")
                        time.sleep(LLAMA_READY_POLL_INTERVAL)
                        continue
                    raise InferenceError(f"llama-server chat request failed: HTTP {e.code} {e.reason}: {error_body}")
                except (urllib.error.URLError, OSError) as e:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RequestCancelledError("Generation cancelled by user")
                    if self._is_connection_refused(e) and time.time() < retry_deadline:
                        logger.debug("streaming chat got connection refused; retrying until ready deadline")
                        time.sleep(LLAMA_READY_POLL_INTERVAL)
                        continue
                    raise InferenceError(f"llama-server chat request failed: {e}")
                # Consume the body on a background thread with blocking reads.
                # Pacing the reader with a socket timeout is unsafe on CPython:
                # the first timeout poisons the socket permanently ("cannot
                # read from timed out object"), so an idle gap during prompt
                # processing would abort a healthy generation. The queue turns
                # an idle gap into a heartbeat for the caller instead.
                self._set_blocking(resp)
                events: queue.Queue = queue.Queue()
                sentinel = object()

                def _reader() -> None:
                    try:
                        while True:
                            raw = resp.read1(65536)
                            if not raw:
                                break
                            events.put(raw)
                    except Exception as exc:  # noqa: BLE001 - relayed to caller
                        events.put(exc)
                    finally:
                        events.put(sentinel)

                reader = threading.Thread(
                    target=_reader, name="giscode-llama-stream", daemon=True
                )
                reader.start()
                try:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise RequestCancelledError("Generation cancelled by user")
                        try:
                            item = events.get(timeout=read_timeout)
                        except queue.Empty:
                            # No chunk for read_timeout seconds: wake the
                            # caller's inactivity watch, keep reading.
                            yield ("", None, None)
                            continue
                        if item is sentinel:
                            break
                        if isinstance(item, BaseException):
                            if self._is_read_timeout(item):
                                yield ("", None, None)
                                continue
                            raise InferenceError(f"llama-server chat request failed: {item}")
                        for line in item.decode("utf-8", "replace").splitlines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(data)
                            except ValueError:
                                continue
                            choices = chunk.get("choices") or []
                            if choices and isinstance(choices[0], dict):
                                delta = choices[0].get("delta") or {}
                                if isinstance(delta, dict) and delta.get("content"):
                                    content = str(delta["content"])
                                    content_len += len(content)
                                    yield (content, None, None)
                                elif choices[0].get("text"):
                                    piece = str(choices[0].get("text"))
                                    content_len += len(piece)
                                    yield (piece, None, None)
                                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                                if reasoning:
                                    reasoning_parts.append(str(reasoning))
                                reason = choices[0].get("finish_reason")
                                if reason:
                                    stop_reason = str(reason)
                            usage = chunk.get("usage") or {}
                            if isinstance(usage, dict):
                                tokens = int(usage.get("completion_tokens", 0) or tokens)
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
                self._record_generation(
                    stop_reason, tokens, started,
                    text_len=content_len,
                    reasoning_len=len("".join(reasoning_parts)),
                    reasoning_budget=request.reasoning_budget,
                )
                yield ("", stop_reason, tokens)
                break
        except GeneratorExit:
            raise

    def _record_generation(self, stop_reason, tokens, started, text_len=0, reasoning_len=0, reasoning_budget=None):
        """Store and log one finished generation for diagnostics and tokens/s."""
        elapsed = time.time() - started
        info = {
            "finish_reason": stop_reason,
            "completion_tokens": tokens,
            "elapsed_s": round(elapsed, 3),
            "tokens_per_sec": round(tokens / elapsed, 2) if elapsed > 0 else 0.0,
            "text_len": text_len,
            "reasoning_len": reasoning_len,
            "reasoning_budget": reasoning_budget or "off",
        }
        self._last_generation = info
        logger.info(
            "chat_generate: finish_reason=%s completion_tokens=%d elapsed_s=%.2f "
            "tokens_per_sec=%.1f text_len=%d reasoning_len=%d reasoning_budget=%s",
            stop_reason, tokens, elapsed, info["tokens_per_sec"], text_len, reasoning_len,
            reasoning_budget or "off",
        )

    def _chat_generate_sync(self, request: ChatRequest, use_port: int) -> ChatResponse:
        body = json.dumps(request.to_payload()).encode("utf-8")
        if os.environ.get("GISCODE_DEBUG_PAYLOAD"):
            _p = request.to_payload()
            logger.warning(
                "payload_debug sync reason_keys=%s msg_chars=%d max_tokens=%s",
                {k: _p.get(k) for k in ("reasoning", "reasoning_budget", "chat_template_kwargs")},
                sum(len(str(m.get("content", ""))) for m in _p.get("messages", [])),
                _p.get("max_tokens"),
            )
        chat_url = f"http://127.0.0.1:{use_port}/v1/chat/completions"
        retry_deadline = time.time() + self._ready_timeout
        started = time.time()
        payload: dict = {}
        while True:
            http_req = urllib.request.Request(
                chat_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(http_req, timeout=300) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                error_body = e.read().decode("utf-8", errors="replace") if e.fp else "no response body"
                if e.code == 503 and time.time() < retry_deadline:
                    logger.debug("chat request got HTTP 503; retrying until ready deadline")
                    time.sleep(LLAMA_READY_POLL_INTERVAL)
                    continue
                raise InferenceError(f"llama-server chat request failed: HTTP {e.code} {e.reason}: {error_body}")
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if self._is_connection_refused(e) and time.time() < retry_deadline:
                    logger.debug("chat request got connection refused; retrying until ready deadline")
                    time.sleep(LLAMA_READY_POLL_INTERVAL)
                    continue
                raise InferenceError(f"llama-server chat request failed: {e}")

        text = ""
        choices = payload.get("choices") or []
        if choices:
            first = choices[0]
            if isinstance(first, dict):
                msg = first.get("message") or {}
                if isinstance(msg, dict) and msg.get("content") is not None:
                    text = str(msg.get("content") or "")
                if not text:
                    text = first.get("text", "") or ""
        if not text:
            text = payload.get("content", "") or ""

        tokens = 0
        usage = payload.get("usage") or {}
        if isinstance(usage, dict):
            tokens = int(usage.get("completion_tokens", 0) or 0)

        stop_reason = "stop"
        if choices and isinstance(choices[0], dict):
            stop_reason = str(choices[0].get("finish_reason") or stop_reason)
        if stop_reason == "stop" and isinstance(payload, dict):
            stop_reason = str(payload.get("finish_reason") or stop_reason)

        reasoning_text = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            if isinstance(msg, dict):
                reasoning_text = str(msg.get("reasoning_content") or "")
        self._record_generation(
            stop_reason, tokens, started,
            text_len=len(text or ""),
            reasoning_len=len(reasoning_text),
            reasoning_budget=request.reasoning_budget,
        )
        return ChatResponse(
            text=text or "",
            request_id=request.request_id,
            tokens_generated=tokens,
            finish_reason=stop_reason,
        )

    def is_running(self, port: int = 8080) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    LIVENESS_PROBE_TIMEOUT_S = 5.0

    def is_model_alive(self, probe_timeout: float = LIVENESS_PROBE_TIMEOUT_S) -> bool:
        """Live = owned process alive AND its port accepts a connection.

        A TCP connect (not an HTTP health GET) is used on purpose: a server
        that is busy generating still accepts connections, so liveness does
        not depend on the server being idle. The loaded-model state is
        cleared only when the owned process is dead.
        """
        proc = self._process
        if proc is None or proc.poll() is not None:
            return False
        try:
            port = int(self._server_port or 8080)
            with socket.create_connection(("127.0.0.1", port), timeout=probe_timeout):
                return True
        except OSError:
            return False


class LlamaCppPythonRuntime(LlamaCppRuntime):
    DEFAULT_N_CTX = 16384

    def __init__(self, n_ctx: int = DEFAULT_N_CTX, n_threads: int = 4, n_gpu_layers: int = 0):
        self._model: Optional[Any] = None
        self._model_path: Optional[str] = None
        self._n_ctx = n_ctx if n_ctx and n_ctx > 0 else self.DEFAULT_N_CTX
        self._n_threads = n_threads
        self._n_gpu_layers = n_gpu_layers
        self._available = False
        self._import_error: Optional[str] = None
        try:
            from llama_cpp import Llama as _Llama
            self._Llama = _Llama
            self._available = True
        except Exception as e:
            self._Llama = None
            self._import_error = f"{type(e).__name__}: {e}"

    def is_available(self) -> bool:
        return self._available

    def get_import_error(self) -> Optional[str]:
        return self._import_error

    def start_server(self, model_path: str, port: int = 8080, n_ctx: Optional[int] = None, n_threads: Optional[int] = None, n_gpu_layers: Optional[int] = None, mmproj_path: Optional[str] = None, cpu_moe: bool = False) -> bool:
        if not self._available or self._Llama is None:
            return False
        try:
            if self._model is not None:
                self.stop_server()
            self._model = self._Llama(
                model_path,
                n_ctx=n_ctx if n_ctx else self._n_ctx,
                n_threads=n_threads if n_threads else self._n_threads,
                n_gpu_layers=n_gpu_layers if n_gpu_layers is not None else self._n_gpu_layers,
            )
            self._model_path = model_path
            return True
        except Exception:
            self._model = None
            self._model_path = None
            return False

    def stop_server(self) -> None:
        if self._model is not None:
            try:
                self._model.close()
            except Exception:
                pass
            finally:
                self._model = None
                self._model_path = None

    def generate(self, request: GenerationRequest, port: int = 8080, cancel_event: Optional[threading.Event] = None) -> GenerationResponse:
        if self._model is None:
            raise InferenceError("No model loaded")
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Generation cancelled by user")
        try:
            if cancel_event is not None and hasattr(self._model, "create_completion"):
                pieces: list[str] = []
                tokens = 0
                stop_reason = "stop"
                for result in self._model.create_completion(
                    prompt=request.prompt,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    stop=request.stop or [],
                    stream=True,
                ):
                    if cancel_event.is_set():
                        raise RequestCancelledError("Generation cancelled by user")
                    choices = result.get("choices") or []
                    if choices:
                        piece = choices[0].get("text") or ""
                        if piece:
                            pieces.append(piece)
                        reason = choices[0].get("finish_reason")
                        if reason:
                            stop_reason = str(reason)
                    usage = result.get("usage") or {}
                    if isinstance(usage, dict):
                        tokens = int(usage.get("completion_tokens", 0) or tokens)
                return GenerationResponse(text="".join(pieces), tokens_generated=tokens, finish_reason=stop_reason)
            result = self._model(
                prompt=request.prompt,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                stop=request.stop or [],
            )
            text = result["choices"][0]["text"] if result.get("choices") else ""
            tokens = result.get("usage", {}).get("completion_tokens", 0)
            return GenerationResponse(text=text or "", tokens_generated=tokens, finish_reason="stop")
        except RequestCancelledError:
            raise
        except Exception as e:
            if cancel_event is not None and cancel_event.is_set():
                raise RequestCancelledError("Generation cancelled by user")
            raise InferenceError(f"Generation failed: {e}")

    def is_running(self, port: int = 8080) -> bool:
        return self._model is not None


class FileSystemModelDiscovery(ModelDiscovery):
    def __init__(self, search_dirs: Sequence[str]):
        self._search_dirs = list(search_dirs)

    def discover(self) -> Sequence[str]:
        found = []
        for directory in self._search_dirs:
            path = Path(directory)
            if path.exists() and path.is_dir():
                for ext in ["*.gguf", "*.GGUF"]:
                    found.extend(str(p) for p in path.rglob(ext) if p.is_file())
        return sorted(set(found))
