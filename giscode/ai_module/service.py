"""The native AI module: one user action, an application-managed engine.

``AIModuleService`` deliberately composes the existing pieces instead of
replacing them: the llama.cpp runtime, ``InferenceAPI``, ``ModelManager`` and
the resource planner remain the only inference path.  This module owns the
parts a normal user should never have to: obtaining a verified runtime,
obtaining a verified model, wiring them together and proving the result with a
real request.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from giscode.ai_module.artifacts import (
    Cancelled,
    DownloadError,
    HashMismatch,
    download_file,
    sha256_file,
)
from giscode.ai_module.manifest import (
    MODEL_CANDIDATES,
    RUNTIME_BUILDS,
    RUNTIME_VERSION,
    ModelCandidate,
    candidate,
    candidate_for_name,
)
from giscode.ai_module.providers import LlamaCppProvider
from giscode.ai_module.readiness import (
    FAILED,
    READY,
    READY_SLOW,
    TOO_LARGE,
    BenchmarkResult,
    run_readiness,
)
from giscode.ai_module.runtime_install import (
    ManagedRuntimeInstaller,
    RuntimeInstallError,
    RuntimeInstallation,
)
from giscode.config.paths import giscode_home

logger = logging.getLogger("giscode.ai_module")

GIB = 1024 ** 3
Event = Callable[[dict], None]


class AIModuleError(RuntimeError):
    """A setup failure whose message is safe and useful to show to a person."""


@dataclass(frozen=True)
class ModuleStatus:
    state: str
    message: str
    runtime_ready: bool = False
    runtime_path: str = ""
    backend: str = ""
    model_ready: bool = False
    model_key: str = ""
    model_name: str = ""
    model_path: str = ""
    runtime_version: str = RUNTIME_VERSION
    last_status: str = ""
    last_tested: float = 0.0
    hardware: dict = field(default_factory=dict)
    storage: dict = field(default_factory=dict)
    models: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "message": self.message,
            "runtime_ready": self.runtime_ready,
            "runtime_path": self.runtime_path,
            "backend": self.backend,
            "model_ready": self.model_ready,
            "model_key": self.model_key,
            "model_name": self.model_name,
            "model_path": self.model_path,
            "runtime_version": self.runtime_version,
            "last_status": self.last_status,
            "last_tested": self.last_tested,
            "hardware": dict(self.hardware),
            "storage": dict(self.storage),
            "models": list(self.models),
        }


@dataclass(frozen=True)
class ModuleResult:
    status: str
    ok: bool
    message: str
    benchmark: Optional[BenchmarkResult] = None
    runtime: Optional[RuntimeInstallation] = None
    model: Optional[ModelCandidate] = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "message": self.message,
            "benchmark": self.benchmark.as_dict() if self.benchmark else None,
            "runtime": self.runtime.__dict__ if self.runtime else None,
            "model": self.model.key if self.model else "",
        }


def _bytes(value: int) -> str:
    return f"{value / GIB:.1f} GB"


def _settings_get(settings, key: str, default=None):
    try:
        return settings.get(key, default)
    except TypeError:
        return settings.get(key)


def _settings_set(settings, key: str, value) -> None:
    try:
        settings.set(key, value)
    except Exception:
        logger.warning("Could not save AI module setting %s", key, exc_info=True)


def _disk_free(path: Path) -> int:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _edition_name() -> str:
    """Edition that owns this AI runtime installation.

    Core and Pro are separate products.  They may share the immutable model
    cache, but never share an executable runtime directory or lifecycle state:
    a Core repair must not replace the CUDA DLLs a running Pro copy is using.
    """
    try:
        from giscode import edition

        return edition.edition()
    except Exception:
        return "core"


def _read_driver_major() -> int:
    try:
        import subprocess

        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first = (completed.stdout or "").strip().splitlines()[0]
        return int(first.split(".")[0])
    except Exception:
        return 0


def _detect_gpu_vendor(name: str) -> str:
    lower = (name or "").lower()
    if "nvidia" in lower:
        return "nvidia"
    if "amd" in lower or "radeon" in lower:
        return "amd"
    if "intel" in lower or "arc" in lower:
        return "intel"
    return ""


def _windows_video_name() -> str:
    """Best-effort AMD/Intel name when nvidia-smi is not the detector.

    ``wmic`` is used only as a read-only fallback; it is not part of setup and
    its absence is harmless.  A vendor name is enough to select the Vulkan
    build, which does not need a second VRAM estimator.
    """
    if os.name != "nt":
        return ""
    try:
        completed = subprocess.run(
            ["wmic", "path", "Win32_VideoController", "get", "name"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        names = [
            line.strip()
            for line in (completed.stdout or "").splitlines()
            if line.strip() and line.strip().lower() != "name"
        ]
        preferred = [name for name in names if _detect_gpu_vendor(name)]
        return (preferred or names or [""])[0]
    except Exception:
        return ""


class AIModuleService:
    def __init__(
        self,
        settings,
        model_manager,
        inference,
        *,
        runtime_root: Optional[str | Path] = None,
        models_dir: Optional[str | Path] = None,
        on_event: Optional[Event] = None,
        runtime_installer: Optional[ManagedRuntimeInstaller] = None,
        download_model: Optional[Callable] = None,
    ) -> None:
        self.settings = settings
        self.model_manager = model_manager
        self.inference = inference
        self._on_event = on_event
        self._cancel = threading.Event()
        self._busy = threading.Lock()
        self._provider = LlamaCppProvider()
        self.edition = _edition_name()
        ai_home = Path(giscode_home()) / "ai" / self.edition
        default_runtime = ai_home / "runtime"
        self.runtime_root = Path(
            runtime_root
            or _settings_get(settings, "ai_module.runtime_dir", str(default_runtime))
            or default_runtime
        ).expanduser()
        # The model cache is deliberately shared by the two installed editions.
        # It is immutable, verified by digest, and removal is guarded below so
        # one edition cannot delete a model the other still has selected.
        self.models_dir = Path(
            models_dir
            or _settings_get(settings, "paths.models_dir", "~/.giscode/models")
            or "~/.giscode/models"
        ).expanduser()
        self._installer = runtime_installer or ManagedRuntimeInstaller(
            self.runtime_root, provider=self._provider
        )
        self._download_model_file = download_model or download_file
        self._state_path = ai_home / "ai_module.json"
        self._state = self._load_state()
        self._hardware_cache: tuple[float, dict] | None = None

    # -- events / state ----------------------------------------------------
    def _emit(self, phase: str, message: str, progress: Optional[float] = None, **extra) -> None:
        if self._on_event is None:
            return
        payload = {"phase": phase, "message": message, "progress": progress}
        payload.update(extra)
        try:
            self._on_event(payload)
        except Exception:
            logger.debug("AI module event hook failed", exc_info=True)

    def _load_state(self) -> dict:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _benchmark_for_state(benchmark: BenchmarkResult) -> dict:
        """Persist measurements, never the generated source itself."""
        payload = benchmark.as_dict()
        payload.pop("code", None)
        return payload

    def _save_state(self, **updates) -> None:
        self._state.update(updates)
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            os.replace(temporary, self._state_path)
        except OSError:
            logger.warning("Could not write AI module state", exc_info=True)
        _settings_set(self.settings, "ai_module.last_state", dict(self._state))

    # -- hardware / storage ------------------------------------------------
    def hardware(self, *, fresh: bool = False) -> dict:
        now = time.monotonic()
        if not fresh and self._hardware_cache is not None:
            if now - self._hardware_cache[0] < 30:
                return dict(self._hardware_cache[1])
        total_ram = available_ram = total_vram = available_vram = 0
        gpu_name = ""
        try:
            from giscode.inference.resource_budget import SystemResourceDetector

            resources = SystemResourceDetector().get_system_resources()
            total_ram = int(resources.total_ram_bytes or 0)
            available_ram = int(resources.available_ram_bytes or 0)
            total_vram = int(resources.total_vram_bytes or 0)
            available_vram = int(resources.available_vram_bytes or 0)
            gpu_name = str(resources.gpu_name or "")
        except Exception:
            logger.debug("Hardware detection failed", exc_info=True)
        if not gpu_name:
            gpu_name = _windows_video_name()
        vendor = _detect_gpu_vendor(gpu_name)
        data = {
            "total_ram_bytes": total_ram,
            "available_ram_bytes": available_ram,
            "total_vram_bytes": total_vram,
            "available_vram_bytes": available_vram,
            "gpu_name": gpu_name,
            "gpu_vendor": vendor,
            "driver_major": _read_driver_major() if vendor == "nvidia" else 0,
            "disk_free_bytes": _disk_free(self.runtime_root),
            "runtime_dir": str(self.runtime_root),
            "models_dir": str(self.models_dir),
            "platform": os.name,
        }
        self._hardware_cache = (now, data)
        return dict(data)

    def storage(self) -> dict:
        models = 0
        try:
            if self.models_dir.is_dir():
                models = sum(
                    p.stat().st_size for p in self.models_dir.glob("*.gguf") if p.is_file()
                )
        except OSError:
            pass
        runtime = 0
        try:
            if self.runtime_root.is_dir():
                runtime = sum(
                    p.stat().st_size
                    for p in self.runtime_root.rglob("*")
                    if p.is_file() and p.suffix.lower() not in (".part", ".zip")
                )
        except OSError:
            pass
        return {
            "models_bytes": models,
            "runtime_bytes": runtime,
            "disk_free_bytes": _disk_free(self.runtime_root),
            "models_dir": str(self.models_dir),
            "runtime_dir": str(self.runtime_root),
        }

    # -- discovery / recommendations --------------------------------------
    def installed_runtime(self) -> str:
        return self._installer.installed() or ""

    def _model_path(self, item: ModelCandidate) -> Path:
        return self.models_dir / item.filename

    def _verified_map(self) -> dict:
        value = self._state.get("verified_models")
        return value if isinstance(value, dict) else {}

    def _is_model_present(self, item: ModelCandidate) -> bool:
        path = self._model_path(item)
        try:
            return path.is_file() and path.stat().st_size == item.size_bytes
        except OSError:
            return False

    def _is_model_verified(self, item: ModelCandidate) -> bool:
        path = self._model_path(item)
        if not self._is_model_present(item):
            return False
        return self._verified_map().get(item.filename) == item.sha256

    def _mark_verified(self, item: ModelCandidate) -> None:
        verified = dict(self._verified_map())
        verified[item.filename] = item.sha256
        self._save_state(verified_models=verified)

    def _verify_model(self, item: ModelCandidate) -> bool:
        path = self._model_path(item)
        if not path.is_file():
            return False
        # A completed download records its pinned digest.  Re-hashing a
        # multi-gigabyte model on every Test would add minutes for no new
        # information; a changed size invalidates the record first.
        if self._verified_map().get(item.filename) == item.sha256 and self._is_model_present(item):
            return True
        self._emit("verify", f"Checking {item.filename}...", None)
        try:
            digest = sha256_file(path, cancel=self._cancel.is_set)
        except Cancelled:
            raise
        except OSError as exc:
            raise AIModuleError(f"The model file could not be read: {exc}") from exc
        if digest.lower() != item.sha256.lower():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            verified = dict(self._verified_map())
            verified.pop(item.filename, None)
            self._save_state(verified_models=verified)
            raise AIModuleError(
                "The installed model failed its integrity check and was removed. "
                "Load the AI module again to download a clean copy."
            )
        self._mark_verified(item)
        return True

    def recommendations(self) -> list[dict]:
        hardware = self.hardware()
        rows = []
        for item in MODEL_CANDIDATES:
            fits, fit_note = item.fits(
                total_ram=int(hardware["total_ram_bytes"]),
                total_vram=int(hardware["total_vram_bytes"]),
                disk_free=int(hardware["disk_free_bytes"]),
                available_ram=int(hardware["available_ram_bytes"]),
                available_vram=int(hardware["available_vram_bytes"]),
            )
            rows.append(
                {
                    "key": item.key,
                    "name": item.name,
                    "description": item.description,
                    "size_bytes": item.size_bytes,
                    "quantization": item.quantization,
                    "license": item.license,
                    "commercial_use": item.commercial_use,
                    "qualified": item.qualified,
                    "qualification": item.qualification,
                    "publisher": item.publisher,
                    "source_url": item.source_url,
                    "revision": item.revision,
                    "parameter_count": item.parameter_count,
                    "expected_performance": item.expected_performance,
                    "license_url": item.license_url,
                    "redistribution": item.redistribution,
                    "context_length": item.context_length,
                    "min_ram_bytes": item.min_ram_bytes,
                    "recommended_vram_bytes": item.recommended_vram_bytes,
                    "fit": fits,
                    "fit_note": fit_note,
                    "installed": self._is_model_present(item),
                    "verified": self._is_model_verified(item),
                    "filename": item.filename,
                }
            )
        rows.sort(key=lambda row: (not row["installed"], not row["qualified"], row["size_bytes"]))
        return rows

    def recommended(self) -> Optional[ModelCandidate]:
        rows = {row["key"]: row for row in self.recommendations()}
        qualified = [
            item
            for item in MODEL_CANDIDATES
            if item.qualified and rows[item.key]["fit"]
        ]
        if qualified:
            # Prefer the installed qualified module, then the smallest one that
            # actually fits.  A larger model is never selected just for size.
            qualified.sort(key=lambda item: (not self._is_model_present(item), item.size_bytes))
            return qualified[0]
        # An unqualified candidate is never a silent default, even when it
        # happens to fit.  The user can still choose it explicitly from the
        # model list, but the one-click action refuses rather than pretending
        # an unbenchmarked model is ready.
        return None

    def scan(self) -> ModuleStatus:
        runtime_path = self.installed_runtime()
        hardware = self.hardware()
        model = candidate(self._state.get("model_key", "")) or self.recommended()
        model_path = str(self._model_path(model)) if model and self._is_model_present(model) else ""
        last_status = str(self._state.get("last_status") or "")
        if runtime_path and model_path and last_status in (READY, READY_SLOW):
            state, message = "ready", f"{model.name} is ready for local coding."
        elif runtime_path and model_path:
            state, message = "installed", "The AI module is installed but has not been tested."
        elif runtime_path:
            state, message = "needs_model", "The runtime is ready. Choose a model to finish."
        else:
            state, message = "not_installed", "No AI module is installed yet."
        return ModuleStatus(
            state=state,
            message=message,
            runtime_ready=bool(runtime_path),
            runtime_path=runtime_path,
            backend=str(self._state.get("backend") or ""),
            model_ready=bool(model_path),
            model_key=model.key if model else "",
            model_name=model.name if model else "",
            model_path=model_path,
            last_status=last_status,
            last_tested=float(self._state.get("last_tested") or 0),
            hardware=hardware,
            storage=self.storage(),
            models=self.recommendations(),
        )

    # -- runtime selection -------------------------------------------------
    def choose_backend(self) -> str:
        hardware = self.hardware()
        # Only an explicit user override is pinned.  The backend recorded by a
        # previous load is informational: a later launch with a working GPU
        # must be allowed to upgrade from the CPU build automatically.
        forced = str(_settings_get(self.settings, "ai_module.backend_override", "") or "").lower()
        if forced in RUNTIME_BUILDS:
            return forced
        vendor = str(hardware.get("gpu_vendor") or "")
        driver = int(hardware.get("driver_major") or 0)
        try:
            from giscode.inference.backend_selection import vulkan_available

            has_vulkan = vulkan_available()
        except Exception:
            has_vulkan = False
        if vendor == "nvidia" and driver >= 580:
            return "cuda"
        if vendor in ("nvidia", "amd", "intel") and has_vulkan:
            return "vulkan"
        return "cpu"

    def _activate_runtime(self, installation: RuntimeInstallation) -> None:
        try:
            n_ctx = int(_settings_get(self.settings, "ai_module.context_length", 8192) or 8192)
        except (TypeError, ValueError):
            n_ctx = 8192
        gpu_layers = 0 if installation.backend == "cpu" else 99
        runtime = self._provider.create_runtime(
            installation.path, n_ctx=n_ctx, n_gpu_layers=gpu_layers
        )
        setter = getattr(self.inference, "set_runtime", None)
        if callable(setter):
            setter(runtime)
        else:  # pragma: no cover - compatibility with an older host
            stop = getattr(getattr(self.inference, "_runtime", None), "stop_server", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
            self.inference._runtime = runtime
            self.inference._model = None
        _settings_set(self.settings, "inference.llama_server_path", installation.path)
        _settings_set(self.settings, "ai_module.runtime_version", RUNTIME_VERSION)
        _settings_set(self.settings, "ai_module.last_backend", installation.backend)

    # -- model acquisition -------------------------------------------------
    def _download_model(self, item: ModelCandidate) -> Path:
        target = self._model_path(item)
        if self._is_model_verified(item):
            return target
        if target.is_file() and not self._is_model_present(item):
            target.unlink(missing_ok=True)
        if target.is_file():
            try:
                if self._verify_model(item):
                    return target
            except AIModuleError:
                # A corrupt installed copy is discarded and fetched again; the
                # user pressed the action that owns the artifact.
                target.unlink(missing_ok=True)
        disk = _disk_free(self.models_dir)
        if disk and disk < int(item.size_bytes * 1.10):
            raise AIModuleError(
                f"{item.name} needs about {_bytes(item.size_bytes)} of free disk space, "
                f"but only {_bytes(disk)} is available."
            )
        self._emit(
            "download",
            f"Downloading {item.name} ({_bytes(item.size_bytes)}). "
            f"It will be stored in {self.models_dir}.",
            0.0,
        )

        def progress(done: int, total: int) -> None:
            fraction = done / total if total else None
            self._emit(
                "download",
                f"Downloading {item.name}: {_bytes(done)} of {_bytes(total or item.size_bytes)}",
                fraction,
            )

        try:
            self._download_model_file(
                item.url,
                target,
                expected_size=item.size_bytes,
                expected_sha256=item.sha256,
                cancel=self._cancel.is_set,
                progress=progress,
            )
        except Cancelled:
            raise
        except HashMismatch as exc:
            raise AIModuleError(str(exc)) from exc
        except DownloadError as exc:
            raise AIModuleError(
                f"The model download did not finish: {exc} Press Load AI Module again to resume."
            ) from exc
        self._mark_verified(item)
        return target

    def _ensure_runtime(self, backend: str, hardware: dict) -> RuntimeInstallation:
        build = RUNTIME_BUILDS[backend]
        try:
            return self._installer.ensure(
                build,
                free_disk=int(hardware.get("disk_free_bytes") or 0),
                cancel=self._cancel.is_set,
                progress=lambda phase, message, fraction: self._emit(phase, message, fraction),
            )
        except Cancelled:
            raise
        except RuntimeInstallError as exc:
            raise AIModuleError(str(exc)) from exc

    def _configure_selection(self, item: ModelCandidate, installation: RuntimeInstallation) -> None:
        _settings_set(self.settings, "model.selected_model", item.name)
        _settings_set(self.settings, "ai_module.enabled", True)
        _settings_set(self.settings, "ai_module.model_key", item.key)
        _settings_set(self.settings, "ai_module.model_name", item.name)
        _settings_set(self.settings, "ai_module.model_path", str(self._model_path(item)))
        _settings_set(
            self.settings,
            "ai_module.license",
            {
                "model": item.license,
                "model_url": item.license_url,
                "runtime": "MIT",
                "runtime_url": "https://github.com/ggml-org/llama.cpp",
            },
        )

    # -- public actions ----------------------------------------------------
    def download_model(self, model_key: str) -> ModuleResult:
        """Download and verify one model without loading it.

        The UI keeps Download, Load and Test separate so a slow network cannot
        look like a failed model load, and so an interrupted download can be
        retried without starting the runtime.
        """
        item = candidate(model_key)
        if item is None:
            return ModuleResult(FAILED, False, "That model is not in the native AI catalog.")
        if not self._busy.acquire(blocking=False):
            return ModuleResult(FAILED, False, "An AI module operation is already in progress.")
        self._cancel.clear()
        try:
            if self._is_model_verified(item):
                return ModuleResult("DOWNLOADED", True, f"{item.name} is already downloaded and verified.", model=item)
            path = self._download_model(item)
            self._save_state(last_status="DOWNLOADED", last_error="", last_tested=time.time())
            return ModuleResult(
                "DOWNLOADED",
                True,
                f"{item.name} downloaded and verified. Select Load Model to activate it.",
                model=item,
            )
        except Cancelled:
            return ModuleResult("CANCELLED", False, "The model download was cancelled. Press Download again to resume.")
        except AIModuleError as exc:
            self._save_state(last_status=FAILED, last_error=str(exc), last_tested=time.time())
            return ModuleResult(FAILED, False, str(exc), model=item)
        except Exception as exc:
            logger.exception("AI model download failed")
            return ModuleResult(FAILED, False, f"The model download failed: {type(exc).__name__}: {exc}", model=item)
        finally:
            self._busy.release()

    def load_module(
        self,
        model_key: Optional[str] = None,
    ) -> ModuleResult:
        if not self._busy.acquire(blocking=False):
            return ModuleResult(FAILED, False, "AI module setup is already in progress.")
        self._cancel.clear()
        try:
            hardware = self.hardware(fresh=True)
            self._emit("hardware", "Checking this PC's memory, graphics and free disk space.", None)
            backend = self.choose_backend()
            self._emit(
                "runtime",
                f"Preparing the GisCode llama.cpp runtime ({RUNTIME_BUILDS[backend].label}).",
                None,
            )
            installation = self._ensure_runtime(backend, hardware)
            self._activate_runtime(installation)

            item = candidate(model_key) if model_key else self.recommended()
            if item is None:
                raise AIModuleError(
                    "No model is suitable for this PC. Free some disk space or memory, then try again."
                )
            self._emit("model", f"Preparing {item.name}.", None)
            path = self._download_model(item)
            self.model_manager.register_model(item.name, str(path))
            self._configure_selection(item, installation)
            self._emit("load", f"Starting {item.name}. The first load can take a few minutes.", None)
            try:
                self.model_manager.load_model(item.name)
            except Exception as exc:
                text = str(exc) or type(exc).__name__
                too_large = "insufficient" in text.lower() or "resource" in text.lower()
                status = TOO_LARGE if too_large else FAILED
                self._save_state(
                    last_status=status,
                    last_error=text,
                    last_tested=time.time(),
                    model_key=item.key,
                    backend=installation.backend,
                )
                return ModuleResult(
                    status,
                    False,
                    (
                        f"{item.name} does not fit in this PC's available memory. "
                        "Choose the smaller model or close other applications."
                        if too_large
                        else f"{item.name} could not be loaded: {text}"
                    ),
                    runtime=installation,
                    model=item,
                )

            self._emit("test", "Testing the local model with a real coding request.", 0.9)
            benchmark = run_readiness(
                self.inference,
                item.name,
                on_event=lambda phase, message: self._emit(phase, message, None),
            )
            self._save_state(
                last_status=benchmark.status,
                last_tested=time.time(),
                model_key=item.key,
                model_name=item.name,
                backend=installation.backend,
                runtime_version=RUNTIME_VERSION,
                benchmark=self._benchmark_for_state(benchmark),
            )
            self._emit("ready", benchmark.detail, 1.0)
            return ModuleResult(
                benchmark.status,
                benchmark.ok,
                benchmark.detail,
                benchmark=benchmark,
                runtime=installation,
                model=item,
            )
        except Cancelled:
            self._save_state(last_status="cancelled", last_error="", last_tested=time.time())
            return ModuleResult("CANCELLED", False, "AI module setup was cancelled. Partial downloads can be resumed.")
        except AIModuleError as exc:
            self._save_state(last_status=FAILED, last_error=str(exc), last_tested=time.time())
            return ModuleResult(FAILED, False, str(exc))
        except Exception as exc:
            logger.exception("AI module setup failed")
            message = f"AI module setup failed: {type(exc).__name__}: {exc}"
            self._save_state(last_status=FAILED, last_error=message, last_tested=time.time())
            return ModuleResult(FAILED, False, message)
        finally:
            self._busy.release()

    def test_module(self, model_key: Optional[str] = None) -> ModuleResult:
        if not self._busy.acquire(blocking=False):
            return ModuleResult(FAILED, False, "An AI module operation is already in progress.")
        self._cancel.clear()
        try:
            item = candidate(model_key) if model_key else candidate(self._state.get("model_key", ""))
            if item is None:
                item = self.recommended()
            if item is None or not self._is_model_present(item):
                return ModuleResult(FAILED, False, "Load AI Module first, then run the test.")
            runtime_path = self.installed_runtime()
            if not runtime_path:
                return ModuleResult(FAILED, False, "The AI runtime is missing. Load AI Module again.")
            marker = self._installer.marker_for(runtime_path) or {}
            installation = RuntimeInstallation(
                path=runtime_path,
                backend=str(marker.get("backend") or self._state.get("backend") or self.choose_backend()),
                version=str(marker.get("version") or RUNTIME_VERSION),
                reused=True,
            )
            self._activate_runtime(installation)
            if not self._verify_model(item):
                return ModuleResult(FAILED, False, "The model file is incomplete. Load AI Module again.")
            self.model_manager.register_model(item.name, str(self._model_path(item)))
            try:
                self.model_manager.load_model(item.name)
            except Exception as exc:
                text = str(exc) or type(exc).__name__
                too_large = "insufficient" in text.lower() or "resource" in text.lower()
                return ModuleResult(
                    TOO_LARGE if too_large else FAILED,
                    False,
                    (
                        f"{item.name} does not fit in this PC's available memory."
                        if too_large
                        else f"{item.name} could not be loaded: {text}"
                    ),
                    runtime=installation,
                    model=item,
                )
            benchmark = run_readiness(
                self.inference,
                item.name,
                on_event=lambda phase, message: self._emit(phase, message, None),
            )
            self._save_state(
                last_status=benchmark.status,
                last_tested=time.time(),
                model_key=item.key,
                model_name=item.name,
                backend=installation.backend,
                runtime_version=RUNTIME_VERSION,
                benchmark=self._benchmark_for_state(benchmark),
            )
            self._emit("ready", benchmark.detail, 1.0)
            return ModuleResult(
                benchmark.status,
                benchmark.ok,
                benchmark.detail,
                benchmark=benchmark,
                runtime=installation,
                model=item,
            )
        except Cancelled:
            return ModuleResult("CANCELLED", False, "The AI module test was cancelled.")
        except Exception as exc:
            logger.exception("AI module test failed")
            return ModuleResult(FAILED, False, f"AI module test failed: {type(exc).__name__}: {exc}")
        finally:
            self._busy.release()

    def switch_model(self, model_key: str) -> ModuleResult:
        item = candidate(model_key)
        if item is None:
            return ModuleResult(FAILED, False, "That model is not in the native AI catalog.")
        if not self._is_model_present(item):
            return ModuleResult(FAILED, False, f"{item.name} is not installed. Download it first.")
        runtime_path = self.installed_runtime()
        if not runtime_path:
            return ModuleResult(FAILED, False, "The AI runtime is missing. Load AI Module again.")
        if not self._busy.acquire(blocking=False):
            return ModuleResult(FAILED, False, "An AI module operation is already in progress.")
        self._cancel.clear()
        try:
            marker = self._installer.marker_for(runtime_path) or {}
            installation = RuntimeInstallation(
                path=runtime_path,
                backend=str(marker.get("backend") or self._state.get("backend") or self.choose_backend()),
                version=str(marker.get("version") or RUNTIME_VERSION),
                reused=True,
            )
            self._activate_runtime(installation)
            if not self._verify_model(item):
                return ModuleResult(FAILED, False, "The model file is incomplete. Download it again.")
            self.model_manager.register_model(item.name, str(self._model_path(item)))
            self.model_manager.load_model(item.name)
            self._configure_selection(item, installation)
            self._save_state(model_key=item.key, model_name=item.name, last_status="installed")
            return ModuleResult("SWITCHED", True, f"Switched to {item.name}. Run Test AI Module to verify it.", runtime=installation, model=item)
        except Exception as exc:
            return ModuleResult(FAILED, False, f"Could not switch models: {exc}")
        finally:
            self._busy.release()

    def _other_edition_uses_model(self, item: ModelCandidate) -> bool:
        other = "pro" if self.edition == "core" else "core"
        state_path = Path(giscode_home()) / "ai" / other / "ai_module.json"
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        verified = payload.get("verified_models")
        return (
            isinstance(verified, dict)
            and verified.get(item.filename) == item.sha256
        ) or str(payload.get("model_key") or "") == item.key

    def remove_model(self, model_key: str, *, confirm: bool = False) -> ModuleResult:
        if not confirm:
            return ModuleResult(FAILED, False, "Removing a model requires confirmation.")
        item = candidate(model_key)
        if item is None:
            return ModuleResult(FAILED, False, "That model is not in the native AI catalog.")
        if self._other_edition_uses_model(item):
            return ModuleResult(
                FAILED,
                False,
                f"{item.name} is still selected by the other GisCode edition. "
                "Switch or remove it there first; the shared model cache was left untouched.",
            )
        if self.model_manager is not None:
            current = self.model_manager.get_current_model()
            if current is not None and Path(str(current.path)).name == item.filename:
                try:
                    self.model_manager.unload_model(current.name)
                except Exception as exc:
                    return ModuleResult(FAILED, False, f"Unload the model before removing it: {exc}")
        removed = False
        for path in (self._model_path(item), self._model_path(item).with_suffix(".gguf.part")):
            try:
                if path.exists():
                    path.unlink()
                    removed = True
            except OSError as exc:
                return ModuleResult(FAILED, False, f"Could not remove {path.name}: {exc}")
        verified = dict(self._verified_map())
        verified.pop(item.filename, None)
        self._save_state(verified_models=verified)
        return ModuleResult("REMOVED", True, f"{item.name} {'was removed' if removed else 'was not installed'}.")

    def cancel(self) -> None:
        self._cancel.set()
        try:
            self.inference.cancel()
        except Exception:
            pass
        self._emit("cancel", "Cancelling the AI module operation...", None)

    def is_busy(self) -> bool:
        return self._busy.locked()
