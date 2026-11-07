import logging
import os
from pathlib import Path
from typing import Optional

from giscode.config.settings import SettingsManager
from giscode.inference.api import InferenceAPI
from giscode.inference.errors import (
    InferenceUnavailableError,
    InsufficientResourceBudgetError,
    LoadFailureError,
    ModelAlreadyLoadingError,
    ModelNotFoundError,
    UnloadFailureError,
)
from giscode.inference.lifecycle import ModelInfo, ModelState
from giscode.inference.resource_budget import (
    DEFAULT_STEPDOWN_SEQUENCE,
    SystemResourceDetector,
    select_context_size,
)
from giscode.models.capability import evidence_for, summary
from giscode.models.capability_store import CapabilityStore
from giscode.models.catalog import register_default_models
from giscode.models.types import ModelMetadata, ModelRegistry, ModelStatus


logger = logging.getLogger("giscode.models")


def _format_size_gib(n: int) -> str:
    return f"{n / (1024 * 1024 * 1024):.2f} GiB"


def select_context_size_for_path(
    *,
    requested: int,
    model_path: str,
    filesize_bytes: int,
    budget: Optional["SystemResourceDetector"] = None,
) -> dict:
    """Wrap :func:`select_context_size` so the result carries a ready-to-print
    message for the GUI / log and a list of presentation-ready rows."""
    detector = budget or SystemResourceDetector()
    try:
        resources = detector.get_system_resources()
        free_vram = resources.available_vram_bytes
        free_ram = resources.available_ram_bytes
        has_gpu = bool(resources.gpu_name and resources.available_vram_bytes > 0)
    except Exception as exc:
        logger.warning("Could not query system resources for budget: %s", exc)
        free_vram = 0
        free_ram = 0
        has_gpu = False

    selection = select_context_size(
        requested=requested,
        filesize_bytes=filesize_bytes,
        free_vram_bytes=free_vram,
        free_ram_bytes=free_ram,
        has_gpu=has_gpu,
        path=model_path if model_path else None,
    )

    chosen = int(selection["chosen"])
    estimate_bytes = int(selection["estimate_bytes"])
    reason = selection["reason"]
    rows = selection["stepdown_log"]

    if chosen <= 0:
        blocks = ", ".join(f"{r['size']}: {_format_size_gib(r['estimated_bytes'])}" for r in rows)
        gpu_line = f" free VRAM={_format_size_gib(free_vram)}" if has_gpu else f" (CPU-only) free RAM={_format_size_gib(free_ram)}"
        message = (
            f"Insufficient resources for model at n_ctx={requested}. Tried: "
            f"{blocks}. Available{gpu_line}."
        )
    elif reason == "fits-as-requested":
        message = (
            f"Model fits at n_ctx={chosen} (estimate {_format_size_gib(estimate_bytes)})"
        )
    else:
        message = (
            f"Model did not fit at n_ctx={requested}; "
            f"stepped down to n_ctx={chosen} (estimate {_format_size_gib(estimate_bytes)})"
        )

    logger.info(
        "Auto-load context selection: requested=%d chosen=%d reason=%s estimate=%s",
        requested,
        chosen,
        reason,
        _format_size_gib(estimate_bytes) if estimate_bytes else "n/a",
    )
    logger.info(
        "  selection resources: free_vram=%s free_ram=%s has_gpu=%s",
        _format_size_gib(free_vram),
        _format_size_gib(free_ram),
        has_gpu,
    )
    logger.info("  model_path=%s filesize_bytes=%d", model_path, filesize_bytes)
    for row in rows:
        logger.info(
            "  candidate n_ctx=%d estimated=%s fits=%s",
            row["size"],
            _format_size_gib(row["estimated_bytes"]),
            row["fits"],
        )

    return {
        "requested": requested,
        "chosen": chosen,
        "reason": reason,
        "estimate_bytes": estimate_bytes,
        "stepdown_log": rows,
        "reason_text": message,
    }


def _default_model_search_dirs() -> list[str]:
    """Extra folders a normal run scans, resolved per platform.

    The machine-wide folder comes from ``paths.public_models_dir()`` so shared
    discovery holds no Windows-only path and picks up a relocated ``%PUBLIC%``.
    """
    from giscode.config.paths import public_models_dir

    dirs = ["~/.giscode/models", "~/.lmstudio/models"]
    public = public_models_dir()
    if public is not None:
        dirs.append(str(public))
    return dirs


def _search_dirs() -> list[str]:
    """The list above, honouring an explicit override (used by tests)."""
    override = globals().get("_DEFAULT_MODEL_SEARCH_DIRS")
    if override is not None:
        return list(override)
    return _default_model_search_dirs()


def __getattr__(name: str):
    # Resolved on access, never at import: the value depends on %PUBLIC% and on
    # the platform, and an import-time constant is what made discovery wrong.
    if name == "_DEFAULT_MODEL_SEARCH_DIRS":
        return _default_model_search_dirs()
    raise AttributeError(name)


def compute_capabilities_for_discovered(name: str, path: str) -> list[str]:
    """Best-effort capability tags for a discovered GGUF file.

    Text/chat are the default for chat GGUFs. Coding is inferred from the
    name. Vision is inferred from a VL/VLM name hint or a sibling *.mmproj
    projector file (see giscode/vision/capabilities).
    """
    from giscode.vision.capabilities import compute_capabilities  # lazy to avoid import cycle

    return compute_capabilities(
        ModelMetadata(name=name, family="discovered", path=path, tags=[])
    )


def _candidate_model_dirs(settings: SettingsManager) -> list[Path]:
    from giscode.config.paths import test_mode

    raw = settings.get("paths.models_dir", "~/.giscode/models")
    candidates: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        try:
            key = str(p.expanduser().resolve(strict=False))
        except Exception:
            key = str(p)
        if key not in seen:
            seen.add(key)
            candidates.append(p.expanduser())

    if isinstance(raw, str):
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if part:
                _add(Path(part))
    elif isinstance(raw, list):
        for part in raw:
            if isinstance(part, str) and part.strip():
                _add(Path(part.strip()))

    # The extra folders are LM Studio's installed weights and the public
    # share. A test run must not discover (or autoload) the developer's
    # personal models, so under ``GISCODE_TEST_MODE`` only the configured
    # models folder - which an isolated profile relocates - is scanned.
    if not test_mode():
        for d in _search_dirs():
            _add(Path(d))

    return candidates


def _scan_gguf_files(dirs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for d in dirs:
        try:
            if not d.exists() or not d.is_dir():
                continue
        except OSError:
            continue
        for pattern in ("*.gguf", "*.GGUF"):
            try:
                found.extend(p for p in d.rglob(pattern) if p.is_file())
            except OSError:
                continue
    return found


class ModelManager:
    def __init__(self, settings: SettingsManager, inference: InferenceAPI):
        self._settings = settings
        self._inference = inference
        self._registry = ModelRegistry()
        #: Capability decisions a person or a test established, keyed by the
        #: model's content identity (see giscode.models.capability_store).
        self._capability_store = CapabilityStore()
        #: Where the last successful load actually ran (see
        #: giscode.inference.device); ``None`` until a model has been loaded.
        self.last_execution_report = None
        register_default_models(self._registry)

    def discover_models(self) -> list[str]:
        found = _scan_gguf_files(_candidate_model_dirs(self._settings))
        return sorted({str(p.resolve()) for p in found})

    def discover_model_metadata(self) -> list[ModelMetadata]:
        """Discover GGUF files and synthesize metadata using the registered name as a hint."""
        files = _scan_gguf_files(_candidate_model_dirs(self._settings))
        out: list[ModelMetadata] = []
        for path in files:
            stem = path.stem
            if "mmproj" in path.name.lower() or "mmproj" in stem.lower():
                # Vision projector files are never the chat model itself. Keep
                # them under their own name so they cannot hijack the chat
                # model's registry slot: their stems often contain the chat
                # model name (e.g. mmproj-Qwen3-VL-4B-Instruct-F16.gguf), which
                # would otherwise bind the VLM's registry entry to the mmproj
                # path and make it un-loadable as the main model.
                matched = stem
            else:
                matched = self._match_registry(stem) or stem
            try:
                size = path.stat().st_size
            except OSError:
                continue
            resolved = str(path.resolve())
            metadata = ModelMetadata(
                name=matched,
                family="discovered",
                path=resolved,
                size_bytes=size,
                status=ModelStatus.REGISTERED,
                description=str(path),
                capabilities=compute_capabilities_for_discovered(matched, resolved),
                source="discovered",
            )
            self._attach_capability_evidence(metadata)
            out.append(metadata)
        return out

    def _match_registry(self, stem: str) -> Optional[str]:
        metadata = self._registry.resolve_canonical_name(stem)
        return metadata.name if metadata is not None else None

    def auto_register_discovered(self) -> list[ModelMetadata]:
        """Populate empty path fields in the registry with discovered GGUF files.

        Existing entries with a valid path are kept. This is non-destructive.
        """
        discovered = self.discover_model_metadata()
        registered: list[ModelMetadata] = []
        for meta in discovered:
            existing = self._registry.get(meta.name)
            if existing is None:
                self._registry.register(meta)
                registered.append(meta)
                continue
            if not existing.path or not Path(existing.path).exists():
                existing.path = meta.path
                existing.size_bytes = meta.size_bytes
                existing.status = ModelStatus.REGISTERED
                # A catalog entry only becomes loadable when a file is bound to
                # it, so this is where its identity and evidence appear.
                self._attach_capability_evidence(existing)
            registered.append(existing)
        return registered

    def register_model(self, name: str, path: str) -> ModelMetadata:
        path_obj = Path(path)
        if not path_obj.exists():
            raise ModelNotFoundError(f"Model file not found: {path}")
        if not path_obj.is_file():
            raise ModelNotFoundError(f"Model path is not a file: {path}")

        existing = self._registry.get(name)
        if existing is None:
            metadata = ModelMetadata(
                name=name,
                family="unknown",
                path=str(path_obj.resolve()),
                size_bytes=path_obj.stat().st_size,
                status=ModelStatus.REGISTERED,
            )
            self._registry.register(metadata)
            return metadata
        else:
            existing.path = str(path_obj.resolve())
            existing.size_bytes = path_obj.stat().st_size
            existing.status = ModelStatus.REGISTERED
            existing.error_message = None
            return existing

    def validate_model_path(self, path: str) -> bool:
        path_obj = Path(path)
        return path_obj.exists() and path_obj.is_file() and path_obj.suffix.lower() == ".gguf"

    def estimate_resource_requirements(self, path: str) -> dict:
        path_obj = Path(path)
        if not path_obj.exists():
            return {"error": "Model file not found"}
        size = path_obj.stat().st_size
        context_length = self._settings.get("inference.context_length", 4096)
        estimated = self._inference._budget.estimate_model_memory(size, context_length)
        return {
            "model_size_bytes": size,
            "estimated_memory_bytes": estimated,
            "context_length": context_length,
            "can_load": self._inference.can_load(path),
        }

    def check_budget(self, path: str) -> bool:
        return self._inference.can_load(path)

    def feasibility(self, name: str, n_ctx: Optional[int] = None):
        """Whether ``name`` can really be loaded now (planner-backed).

        One entry point for routing and startup autoload, so neither can offer
        a model that the loader would refuse. The verdict carries the planner's
        own plan, context and layer count.
        """
        from giscode.models.feasibility import evaluate

        metadata = self._registry.get(name)
        path = getattr(metadata, "path", "") if metadata is not None else ""
        if metadata is not None and not self.validate_model_path(path):
            resolved = self._resolve_path_for_name(name)
            path = str(resolved) if resolved is not None else ""
        if n_ctx is None:
            try:
                n_ctx = int(self._settings.get("inference.context_length", 0) or 0) or None
            except Exception:
                n_ctx = None
        return evaluate(self._inference, name=name, path=path, n_ctx=n_ctx)

    def get_model_status(self, name: str) -> Optional[ModelMetadata]:
        return self._registry.get(name)

    def get_current_model(self) -> Optional[ModelInfo]:
        return self._inference.get_model_status()

    def load_model(self, name: str) -> ModelMetadata:
        metadata = self._registry.get(name)
        if metadata is None:
            raise ModelNotFoundError(f"Model not registered: {name}")

        if not self.validate_model_path(metadata.path):
            resolved = self._resolve_path_for_name(name)
            if resolved is None:
                metadata.status = ModelStatus.ERROR
                metadata.error_message = f"No GGUF file found for '{name}'."
                raise ModelNotFoundError(metadata.error_message)
            metadata.path = str(resolved.resolve())
            try:
                metadata.size_bytes = resolved.stat().st_size
            except OSError:
                pass

        # Check if model is already loaded on server to avoid redundant budget check
        try:
            current = self._inference.get_model_status()
            if current is not None and current.path == str(Path(metadata.path).resolve()):
                # Model already loaded on server
                metadata.status = ModelStatus.READY
                metadata.size_bytes = current.size_bytes or metadata.size_bytes
                if current.context_length:
                    metadata.context_length = current.context_length
                metadata.error_message = None
                return metadata
        except Exception:
            pass  # Fall through to normal load if check fails

        # Automatic step-down: choose the largest n_ctx that fits.
        # Settings > metadata.context_length > 16384 default.
        requested_n_ctx = 0
        try:
            requested_n_ctx = int(self._settings.get("inference.context_length", 0))
        except Exception:
            requested_n_ctx = 0
        if requested_n_ctx <= 0:
            requested_n_ctx = int(getattr(self._inference, "_n_ctx", 0) or 0)
        if requested_n_ctx <= 0:
            requested_n_ctx = int(metadata.context_length) if metadata.context_length > 0 else 16384

        size_bytes = 0
        try:
            size_bytes = Path(metadata.path).stat().st_size
        except OSError:
            pass

        selection = select_context_size_for_path(
            requested=requested_n_ctx,
            model_path=metadata.path,
            filesize_bytes=size_bytes,
            budget=self._inference._budget,
        )

        if selection["chosen"] <= 0:
            metadata.status = ModelStatus.ERROR
            metadata.error_message = selection["reason_text"]
            raise InsufficientResourceBudgetError(selection["reason_text"])

        metadata.context_length = selection["chosen"]
        if selection["reason"] == "fits-as-requested":
            metadata.error_message = None
        else:
            metadata.error_message = selection["reason_text"]

        # Find mmproj file if this is a vision model
        mmproj_path = self._find_mmproj_for_model(metadata.path)
        if "vision" in {tag.lower() for tag in metadata.capabilities} and mmproj_path is None:
            metadata.status = ModelStatus.ERROR
            metadata.error_message = (
                f"Vision model '{name}' requires a companion mmproj GGUF file beside the model"
            )
            self._inference._log_load_failure(name, metadata.error_message)
            raise ModelNotFoundError(metadata.error_message)

        resources = self._inference._budget.get_system_resources()
        free_vram = int(resources.available_vram_bytes or 0)
        full_offload = bool(free_vram and selection["estimate_bytes"] <= int(free_vram * 0.85))
        gpu_required = self._gpu_required()
        if gpu_required:
            # GPU-required mode never degrades: report the real limit instead of
            # quietly loading on the CPU (where a 30B model is unusable) and
            # never retry a failed GPU load with zero layers.
            family = self._backend_family()
            if family == "cpu":
                message = (
                    "GPU-required mode (inference.require_gpu) is on, but the only "
                    "working llama-server build on this machine is the CPU build. "
                    f"Refusing to load '{name}' on the CPU."
                )
                metadata.status = ModelStatus.ERROR
                metadata.error_message = message
                self._inference._log_load_failure(name, message)
                raise InferenceUnavailableError(message)
            if not full_offload:
                message = (
                    "GPU-required mode (inference.require_gpu) is on, but this model "
                    f"needs about {selection['estimate_bytes'] / (1024 ** 3):.2f} GiB at "
                    f"{selection['chosen']} context and only "
                    f"{free_vram / (1024 ** 3):.2f} GiB of graphics memory is free. "
                    "Refusing to load it on the CPU."
                )
                metadata.status = ModelStatus.ERROR
                metadata.error_message = message
                self._inference._log_load_failure(name, message)
                raise InsufficientResourceBudgetError(message)
        layer_candidates = [-1] if gpu_required else ([-1, 0] if full_offload else [0])
        load_error = None
        model_info = None
        for gpu_layers in layer_candidates:
            before_vram = int(self._inference._budget.get_system_resources().available_vram_bytes or 0)
            try:
                model_info = self._inference.load_model(
                    metadata.path,
                    mmproj_path=mmproj_path,
                    n_ctx=selection["chosen"],
                    n_gpu_layers=gpu_layers,
                )
                after_vram = int(self._inference._budget.get_system_resources().available_vram_bytes or 0)
                logger.info(
                    "Model load succeeded: name=%s n_ctx=%d gpu_layers=%d vram_free_before=%d vram_free_after=%d",
                    name, selection["chosen"], gpu_layers, before_vram, after_vram,
                )
                metadata.gpu_layers = gpu_layers
                break
            except (LoadFailureError, InsufficientResourceBudgetError, InferenceUnavailableError) as exc:
                load_error = exc
                logger.warning(
                    "Model load attempt failed: name=%s n_ctx=%d gpu_layers=%d: %s",
                    name, selection["chosen"], gpu_layers, exc,
                )
                continue
            except ModelAlreadyLoadingError:
                metadata.status = ModelStatus.ERROR
                metadata.error_message = "Another model is currently loading."
                raise

        if model_info is None:
            metadata.status = ModelStatus.ERROR
            metadata.error_message = f"{type(load_error).__name__}: {load_error}" if load_error else "Model load failed"
            self._inference._log_load_failure(name, metadata.error_message)
            raise load_error or LoadFailureError(metadata.error_message)

        metadata.status = ModelStatus.READY
        metadata.size_bytes = model_info.size_bytes or metadata.size_bytes
        metadata.error_message = None
        self.last_execution_report = self._execution_report(gpu_layers, selection["chosen"])
        logger.info("Model '%s' %s", name, self.last_execution_report.describe())
        return metadata

    # ------------------------------------------------- capability records

    def _store(self) -> CapabilityStore:
        """The capability store, created on demand for lightweight instances."""
        store = getattr(self, "_capability_store", None)
        if store is None:
            store = CapabilityStore()
            self._capability_store = store
        return store

    def _attach_capability_evidence(self, metadata: ModelMetadata) -> ModelMetadata:
        """Stamp identity and per-capability evidence on *metadata*.

        ``metadata.identity`` is the header digest: a content-derived index key
        that follows a rename and changes with the file's size or header. It is
        not by itself proof of the weights, so the store is asked through
        ``for_path``, which hashes the whole file and drops the record when it
        does not match what was verified. Evidence can only be raised by a
        stored record, never lowered, so a stale file cannot weaken what the
        model itself proves.
        """
        from giscode.models.capability import identity_for

        path = getattr(metadata, "path", "") or ""
        identity = identity_for(path)
        metadata.identity = identity.digest
        stored = self._store().for_path(path) if identity.known else {}
        records = evidence_for(metadata, stored)
        metadata.capability_evidence = summary(records)
        return metadata

    def capability_evidence(self, name: str) -> dict:
        """``{capability: {level, source, detail, verified}}`` for one model."""
        metadata = self._registry.get(name)
        if metadata is None:
            return {}
        if not getattr(metadata, "capability_evidence", None):
            self._attach_capability_evidence(metadata)
        return dict(metadata.capability_evidence)

    def capability_report(self, name: str) -> list[dict]:
        """Every capability of *name* with the strength of its evidence."""
        return [
            dict(value, capability=capability)
            for capability, value in self.capability_evidence(name).items()
        ]

    def record_capability(
        self,
        name: str,
        capability: str,
        level: str,
        *,
        source: str = "user",
        detail: str = "",
    ) -> bool:
        """Persist a user-confirmed or verified capability for *name*.

        Refused (``False``) when the model's identity cannot be computed, so a
        decision is never attached to a file GisCode cannot recognise again.
        """
        metadata = self._registry.get(name)
        if metadata is None:
            return False
        if not getattr(metadata, "identity", ""):
            self._attach_capability_evidence(metadata)
        if not metadata.identity:
            return False
        # Recording through the path hashes the whole file and stores that
        # content identity, so the decision is dropped if the weights are
        # replaced later.
        saved = self._store().record_for_path(
            getattr(metadata, "path", "") or "", capability, level, source=source, detail=detail
        )
        if saved:
            self._attach_capability_evidence(metadata)
        return saved

    def _execution_report(self, gpu_layers: int, n_ctx: int):
        """Classify where that load actually ran, from backend + layers."""
        from giscode.inference.device import execution_report

        try:
            resources = self._inference._budget.get_system_resources()
        except Exception:
            resources = None
        return execution_report(
            backend_family=self._backend_family(),
            gpu_layers=int(gpu_layers),
            n_ctx=int(n_ctx or 0),
            resources=resources,
        )

    @property
    def execution_report(self):
        """The last successful load's device report, or ``None``."""
        return getattr(self, "last_execution_report", None)

    def _gpu_required(self) -> bool:
        """``inference.require_gpu`` (default False: automatic behaviour)."""
        try:
            return bool(self._settings.get("inference.require_gpu", False))
        except Exception:
            return False

    def _backend_family(self) -> str:
        """Family of the resolved llama-server build ('' when unknown)."""
        runtime = getattr(self._inference, "_runtime", None)
        getter = getattr(runtime, "get_backend_family", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:
                return ""
        return ""

    def _find_mmproj_for_model(self, model_path: str) -> Optional[str]:
        """Find matching mmproj file for a vision model."""
        model_dir = Path(model_path).parent
        model_stem = Path(model_path).stem
        # Look for mmproj files in the same directory
        for f in model_dir.iterdir():
            if f.is_file() and "mmproj" in f.name.lower():
                return str(f.resolve())
        return None

    def _resolve_path_for_name(self, name: str) -> Optional[Path]:
        """Try to find a GGUF on disk that matches the registered name."""
        name_lower = name.lower()
        for meta in self.discover_model_metadata():
            if "mmproj" in Path(meta.path).name.lower():
                continue
            if meta.name.lower() == name_lower or name_lower in Path(meta.path).stem.lower():
                return Path(meta.path)
        return None

    def unload_model(self, name: str) -> None:
        metadata = self._registry.get(name)
        if metadata is None:
            raise ModelNotFoundError(f"Model not registered: {name}")

        self._inference.unload_model()
        metadata.status = ModelStatus.UNKNOWN

    def get_registry(self) -> ModelRegistry:
        return self._registry

    def list_registered(self) -> list[ModelMetadata]:
        return self._registry.list_all()
