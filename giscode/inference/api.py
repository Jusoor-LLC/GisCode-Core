import os
import threading
from pathlib import Path
from typing import Optional

import logging

import giscode.logging.logger as _giscode_logger  # noqa: F401  (sanitizer registration)


def _get_logger_for_inference() -> logging.Logger:
    return logging.getLogger("giscode.inference")

from giscode.inference.errors import (
    InferenceUnavailableError,
    InsufficientResourceBudgetError,
    LoadFailureError,
    ModelAlreadyLoadingError,
    ModelNotFoundError,
    RequestCancelledError,
    UnloadFailureError,
)
from giscode.inference.gguf import read_metadata
from giscode.inference.interfaces import ModelDiscovery, ResourceBudget
from giscode.inference.lifecycle import ModelInfo, ModelState
from giscode.inference.resource_budget import (
    DefaultResourceBudget,
    SystemResourceDetector,
    plan_model_load,
)
from giscode.inference.runtime import (
    ChatRequest,
    ChatResponse,
    FileSystemModelDiscovery,
    GenerationRequest,
    GenerationResponse,
    LlamaCppPythonRuntime,
    LlamaCppRuntime,
    SubprocessLlamaCppRuntime,
)


def _default_model_dirs() -> list[str]:
    """The folders an implicit discovery scans on any platform.

    The machine-wide folder is resolved by :func:`giscode.config.paths.
    public_models_dir` instead of being hard-coded here, so shared discovery
    carries no Windows-only path. The order is unchanged on Windows.
    """
    from giscode.config.paths import public_models_dir

    dirs = ["~/.giscode/models", "~/.lmstudio/models"]
    public = public_models_dir()
    if public is not None:
        dirs.append(str(public))
    return dirs


#: Kept for callers that read the historical constant; resolved on access.
def __getattr__(name: str):
    if name == "_DEFAULT_MODEL_DIRS":
        return _default_model_dirs()
    raise AttributeError(name)


def _default_discovery_dirs() -> list[str]:
    """The folders an implicit discovery may scan.

    Under ``GISCODE_TEST_MODE`` only the profile's own models folder is used,
    so an isolated run cannot see the developer's LM Studio weights. Without
    that explicit switch the list is exactly what it always was.
    """
    from giscode.config.paths import giscode_home, test_mode

    if test_mode():
        return [str(giscode_home() / "models")]
    return _default_model_dirs()


def _model_search_dirs() -> list[str]:
    """Folders a normal run scans, with LM Studio resolved per profile.

    ``_DEFAULT_MODEL_DIRS`` hard-codes ``~/.lmstudio/models``, which is only
    right while ``Path.home()`` is the real profile. A redirected
    ``USERPROFILE``/``HOME`` (an isolated harness) used to hide every installed
    weight; the weights live in the profile ``%LOCALAPPDATA%`` still reports.
    """
    from giscode.config.paths import giscode_home, test_mode
    from giscode.inference.runtime import _user_profile_roots

    if test_mode():
        return [str(giscode_home() / "models")]

    dirs: list[str] = []
    seen: set[str] = set()

    def add(value: Path | str) -> None:
        key = os.path.normcase(str(value))
        if key in seen:
            return
        seen.add(key)
        dirs.append(str(value))

    from giscode.config.paths import public_models_dir

    add(giscode_home() / "models")
    for root in _user_profile_roots():
        add(root / ".lmstudio" / "models")
    public = public_models_dir()
    if public is not None:
        add(public)
    return dirs


def _serves_chat(runtime) -> bool:
    """True only for a runtime that really serves chat completions.

    ``hasattr(runtime, "chat_generate")`` is always True - the base class
    defines the method and raises - so it cannot be used to choose a runtime.
    A runtime qualifies when it declares ``supports_chat_generate`` or when its
    class overrides the base ``chat_generate`` (hosted and test runtimes do the
    latter without setting the flag).
    """
    if bool(getattr(runtime, "supports_chat_generate", False)):
        return True
    from giscode.inference.runtime import LlamaCppRuntime

    implementation = getattr(type(runtime), "chat_generate", None)
    return implementation is not None and implementation is not LlamaCppRuntime.chat_generate


def _no_chat_runtime_note(python_runtime) -> str:
    """Why chat is unavailable when no chat-capable runtime exists."""
    from giscode.inference import runtime as _runtime_module

    build = _runtime_module.describe_backend_choice()
    suffix = f" {build}" if build else ""
    if getattr(python_runtime, "is_available", lambda: False)():
        return (
            "No llama-server build was found, and the in-process llama_cpp runtime "
            "cannot serve /v1/chat/completions, which GisCode's planner and coding "
            "agent require. Chat requests are refused until a working "
            "CUDA/Vulkan/CPU llama-server build is available." + suffix
        )
    return (
        "No llama-server build was found and the in-process llama_cpp runtime is "
        "not installed. Chat requests are refused." + suffix
    )


class InferenceAPI:
    def __init__(
        self,
        runtime: Optional[LlamaCppRuntime] = None,
        discovery: Optional[ModelDiscovery] = None,
        budget: Optional[ResourceBudget] = None,
        n_ctx: int = 4096,
        n_threads: int = 4,
        n_gpu_layers: int = 0,
    ):
        self._runtime_note = ""
        if runtime is None:
            subprocess_runtime = SubprocessLlamaCppRuntime(n_ctx=n_ctx, n_threads=n_threads, n_gpu_layers=n_gpu_layers)
            python_runtime = LlamaCppPythonRuntime(
                n_ctx=n_ctx, n_threads=n_threads, n_gpu_layers=n_gpu_layers
            )
            if subprocess_runtime.is_available():
                self._runtime = subprocess_runtime
            elif _serves_chat(python_runtime):
                self._runtime = python_runtime
            else:
                # Keep the llama-server runtime (even unavailable) so failures
                # name the real cause instead of surfacing later as a chat-API
                # gap on a runtime that could never have served chat.
                self._runtime = subprocess_runtime
                self._runtime_note = _no_chat_runtime_note(python_runtime)
                if self._runtime_note:
                    _get_logger_for_inference().warning("%s", self._runtime_note)
        else:
            self._runtime = runtime
        self._discovery = discovery or FileSystemModelDiscovery(
            [str(Path(d).expanduser()) for d in _model_search_dirs()]
        )
        self._budget = budget or DefaultResourceBudget()
        self._n_ctx = n_ctx
        self._model: Optional[ModelInfo] = None
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()

    @property
    def available(self) -> bool:
        return self._runtime.is_available()

    @property
    def runtime_selection_note(self) -> str:
        """Why the runtime is a degraded choice ('' for a healthy selection)."""
        return self._runtime_note

    def _probe_runtime_liveness(self) -> bool:
        """Authoritative runtime liveness probe. Never trusts cached model state.

        Liveness means the owned server process is alive and its port accepts
        a connection (``is_model_alive``). A busy server still accepts
        connections, so a mid-generation probe no longer clears the
        loaded-model state; the state is cleared only when the owned process
        is dead.
        """
        probe = getattr(self._runtime, "is_model_alive", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:
                return False
        for probe_name in ("_is_server_running", "is_running"):
            probe = getattr(self._runtime, probe_name, None)
            if callable(probe):
                try:
                    return bool(probe())
                except Exception:
                    return False
        return bool(self.available)

    def _resolved_model_state_locked(self) -> Optional[ModelInfo]:
        """Resolve the currently loaded model using runtime liveness, not stale cache.

        A cached ERROR record (last load failed) is never reported as loaded.
        A cached READY/BUSY/CANCELLED record is trusted only while the runtime is
        actually alive (owned process up and its port accepting a connection);
        otherwise the stale cache is cleared and None returned. A liveness
        failure caused by a busy server does NOT clear the state - only a dead
        process does.
        """
        if self._model is not None and self._model.state in (ModelState.ERROR, ModelState.UNLOADED):
            return None
        if self._model is not None:
            if self._model.state in (ModelState.READY, ModelState.BUSY, ModelState.CANCELLED):
                if self._probe_runtime_liveness():
                    return self._model
                # Runtime died while we believed a model was loaded -> stale.
                self._model = None
                return None
            # LOADING / UNLOADING: model not confirmed ready yet.
            return None
        # No internal state: ask the runtime (live probe if supported).
        getter = getattr(self._runtime, "get_model_status", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return None

    def get_health(self) -> dict:
        """Current inference health, with the four states told apart.

        ``model_loaded`` stays the single truth callers already branch on, and
        ``model_state`` stays ``None`` unless a model is genuinely ready (a
        dead server or a failed load is never reported as one). ``load_state``
        adds what those two keys cannot say by themselves: whether the process
        simply has no model (``unloaded``), is still starting one
        (``loading``), refused a load (``failed``, with ``load_error``), or has
        no usable runtime at all (``unavailable``). Without it, "nothing was
        ever loaded" and "the load failed" look identical to a caller.
        """
        with self._lock:
            resolved = self._resolved_model_state_locked()
            if resolved is not None:
                return {
                    "available": self.available,
                    "model_loaded": True,
                    "model_state": resolved.state.value,
                    "model_name": resolved.name,
                    "load_state": "loaded",
                    "load_error": None,
                }
            tracked = self._model
            if tracked is not None and tracked.state == ModelState.ERROR:
                return {
                    "available": self.available,
                    "model_loaded": False,
                    "model_state": None,
                    "model_name": tracked.name,
                    "load_state": "failed",
                    "load_error": tracked.error_message or "model load failed",
                }
            if tracked is not None and tracked.state == ModelState.LOADING:
                return {
                    "available": self.available,
                    "model_loaded": False,
                    "model_state": None,
                    "model_name": tracked.name,
                    "load_state": "loading",
                    "load_error": None,
                }
            available = self.available
            return {
                "available": available,
                "model_loaded": False,
                "model_state": None,
                "model_name": None if tracked is None else tracked.name,
                "load_state": "unloaded" if available else "unavailable",
                "load_error": None,
            }

    def discover_models(self) -> list[str]:
        return list(self._discovery.discover())

    def get_model_status(self) -> Optional[ModelInfo]:
        with self._lock:
            return self._resolved_model_state_locked()

    def _plan_load(self, model_path: str, n_ctx: int, max_gpu_layers: Optional[int] = None) -> dict:
        """Plan a load configuration from GGUF metadata + free memory."""
        meta = read_metadata(model_path)
        getter = getattr(self._budget, "get_system_resources", None)
        if callable(getter):
            resources = getter()
        else:
            resources = SystemResourceDetector().get_system_resources()
        has_gpu = bool(resources.gpu_name) and (resources.available_vram_bytes or 0) > 0
        return plan_model_load(
            meta,
            free_vram_bytes=int(resources.available_vram_bytes or 0),
            free_ram_bytes=int(resources.available_ram_bytes or 0),
            requested_ctx=int(n_ctx),
            has_gpu=has_gpu,
            max_gpu_layers=max_gpu_layers,
        )

    def plan_load(
        self,
        model_path: str,
        n_ctx: Optional[int] = None,
        max_gpu_layers: Optional[int] = None,
    ) -> dict:
        """Public planner entry point - the one feasibility source of truth.

        Selection (routing, startup autoload) and loading must never disagree
        about what can run: both ask this method, and it re-reads the current
        resource readings on every call, so a machine that lost memory between
        selection and load is planned again rather than trusted.
        """
        effective = int(n_ctx) if n_ctx and int(n_ctx) > 0 else int(self._n_ctx)
        return self._plan_load(model_path, effective, max_gpu_layers)

    def can_load(self, model_path: str) -> bool:
        path = Path(model_path)
        if not path.exists():
            return False
        size = path.stat().st_size
        try:
            return bool(self._plan_load(str(path), self._n_ctx)["fits"])
        except Exception:
            return self._budget.can_load(size, self._n_ctx)

    def load_model(
        self,
        model_path: str,
        mmproj_path: Optional[str] = None,
        n_ctx: Optional[int] = None,
        n_gpu_layers: Optional[int] = None,
    ) -> ModelInfo:
        """Load `model_path`. When `n_ctx` is provided it overrides the default
        configured context size for this single load (used by the automatic
        step-down logic). The runtime's persistent setting is left untouched
        so subsequent loads without an override still use the configured
        default.
        """
        effective_n_ctx = int(n_ctx) if n_ctx and n_ctx > 0 else int(self._n_ctx)
        effective_gpu_layers = (
            int(n_gpu_layers) if n_gpu_layers is not None else int(getattr(self._runtime, "_n_gpu_layers", 0))
        )
        cpu_moe = False
        # A different model is currently resident and running: unload it first
        # so switching models never raises ModelAlreadyLoadingError.
        with self._lock:
            resolved_current = None
            try:
                resolved_current = self._resolved_model_state_locked()
            except Exception:
                resolved_current = None
        if resolved_current is not None:
            try:
                resolved_current_path = str(Path(resolved_current.path).resolve())
            except Exception:
                resolved_current_path = ""
            try:
                requested_path = str(Path(model_path).resolve())
            except Exception:
                requested_path = model_path
            if requested_path != resolved_current_path:
                self.unload_model()

        with self._lock:
            # Allow reload after a failed load (ERROR); only a live resident model blocks.
            if self._model is not None and self._model.state not in (ModelState.UNLOADED, ModelState.ERROR):
                raise ModelAlreadyLoadingError("A model is already loaded")
            if not Path(model_path).exists():
                raise ModelNotFoundError(f"Model not found: {model_path}")
            if not self.available:
                import_error = getattr(self._runtime, "get_import_error", lambda: None)() or "runtime reports not available"
                raise InferenceUnavailableError(
                    f"Inference runtime is not available ({type(self._runtime).__name__}): {import_error}"
                )

            # Validate mmproj_path if provided
            if mmproj_path is not None and not Path(mmproj_path).exists():
                raise ModelNotFoundError(f"mmproj file not found: {mmproj_path}")

            # Check if model is already loaded on the server to avoid redundant budget check
            try:
                current = self._runtime.get_model_status() if hasattr(self._runtime, 'get_model_status') else None
                if current is not None and current.path == str(Path(model_path).resolve()):
                    # Model already loaded on server - just update internal tracking
                    self._model = ModelInfo(
                        name=Path(model_path).stem,
                        path=str(Path(model_path).resolve()),
                        size_bytes=Path(model_path).stat().st_size,
                        state=ModelState.READY,
                        context_length=effective_n_ctx,
                        gpu_layers=effective_gpu_layers,
                    )
                    return self._model
            except Exception:
                pass  # Fall through to normal load if check fails

            try:
                plan = self._plan_load(
                    model_path,
                    effective_n_ctx,
                    effective_gpu_layers if effective_gpu_layers > 0 else None,
                )
            except Exception as exc:
                # Unreadable/truncated GGUF (e.g. mock or corrupt file): fall
                # back to the legacy size-based budget heuristic.
                _get_logger_for_inference().warning(
                    "GGUF metadata unreadable for %s (%s); using legacy size heuristic",
                    Path(model_path).name, exc,
                )
                size = Path(model_path).stat().st_size
                if not self._budget.can_load(size, effective_n_ctx):
                    raise InsufficientResourceBudgetError(
                        f"Insufficient resources to load model at n_ctx={effective_n_ctx}: "
                        f"cannot read GGUF metadata ({exc})"
                    )
                plan = {
                    "fits": True,
                    "reason": f"legacy-size-heuristic (GGUF unreadable: {exc})",
                    "step": "legacy",
                    "n_ctx": effective_n_ctx,
                    "gpu_layers": effective_gpu_layers,
                    "cpu_moe": False,
                    "layer_count": 0,
                    "requested_ctx": effective_n_ctx,
                    "estimate": {},
                }
            if not plan["fits"]:
                raise InsufficientResourceBudgetError(
                    f"Insufficient resources to load model at n_ctx={effective_n_ctx}: {plan['reason']}"
                )
            _get_logger_for_inference().info(
                "load plan %s: step=%s reason=%s gpu_layers=%s/%s cpu_moe=%s n_ctx=%s "
                "est_vram=%.2f GiB est_ram=%.2f GiB",
                Path(model_path).name,
                plan["step"],
                plan["reason"],
                plan["gpu_layers"],
                plan["layer_count"],
                plan["cpu_moe"],
                plan["n_ctx"],
                plan["estimate"].get("vram_total_gib", 0.0),
                plan["estimate"].get("ram_total_gib", 0.0),
            )
            effective_n_ctx = int(plan["n_ctx"]) or effective_n_ctx
            effective_gpu_layers = int(plan["gpu_layers"])
            cpu_moe = bool(plan["cpu_moe"])

            self._cancel_event.clear()
            model = ModelInfo(
                name=Path(model_path).stem,
                path=model_path,
                size_bytes=Path(model_path).stat().st_size,
                state=ModelState.LOADING,
                context_length=effective_n_ctx,
                gpu_layers=effective_gpu_layers,
            )
            self._model = model

        try:
            try:
                success = self._runtime.start_server(
                    model_path,
                    mmproj_path=mmproj_path,
                    n_ctx=effective_n_ctx,
                    n_gpu_layers=effective_gpu_layers,
                    cpu_moe=cpu_moe,
                )
            except TypeError:
                # Older/custom runtimes without the cpu_moe kwarg.
                success = self._runtime.start_server(
                    model_path,
                    mmproj_path=mmproj_path,
                    n_ctx=effective_n_ctx,
                    n_gpu_layers=effective_gpu_layers,
                )
            if not success:
                import_error = getattr(self._runtime, "get_import_error", lambda: None)() or "native runtime failed to load model"
                raise LoadFailureError(
                    f"Failed to start runtime for model: {model_path}. "
                    f"({type(self._runtime).__name__}): {import_error}"
                )

            with self._lock:
                self._model.state = ModelState.READY
                self._model.load_time = 0.0
                self._model.context_length = effective_n_ctx
                self._model.gpu_layers = effective_gpu_layers

            return self._model
        except Exception as e:
            with self._lock:
                self._model.state = ModelState.ERROR
                self._model.error_message = str(e)
            if isinstance(e, LoadFailureError):
                raise
            raise LoadFailureError(str(e))

    def unload_model(self) -> None:
        with self._lock:
            if self._model is None or self._model.state == ModelState.UNLOADED:
                return
            model = self._model
            model.state = ModelState.UNLOADING

        try:
            self._runtime.stop_server()
            with self._lock:
                model.state = ModelState.UNLOADED
                self._model = None
        except Exception as e:
            with self._lock:
                model.state = ModelState.ERROR
                model.error_message = str(e)
            raise UnloadFailureError(str(e))

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        with self._lock:
            # Resolve the loaded model against runtime liveness, never stale cache.
            resolved = self._resolved_model_state_locked()
            if resolved is None or resolved.state != ModelState.READY:
                raise InferenceUnavailableError("No model loaded")
            self._model = resolved
            self._model.state = ModelState.BUSY
        self._cancel_event.clear()
        cancel_event = self._cancel_event

        try:
            response = self._runtime.generate(request, cancel_event=cancel_event)
        except TypeError:
            response = self._runtime.generate(request)
            with self._lock:
                if self._model is not None:
                    self._model.state = ModelState.READY
            return response
        except RequestCancelledError:
            with self._lock:
                if self._model is not None:
                    self._model.state = ModelState.READY
            raise
        except Exception as e:
            with self._lock:
                if self._model is not None:
                    self._model.state = ModelState.READY
            raise e
        with self._lock:
            if self._model is not None:
                self._model.state = ModelState.READY
        return response

    def chat_generate(self, request: ChatRequest) -> ChatResponse:
        with self._lock:
            # Resolve the loaded model against runtime liveness, never stale cache.
            resolved = self._resolved_model_state_locked()
            if resolved is None or resolved.state != ModelState.READY:
                raise InferenceUnavailableError("No model loaded")
            if not _serves_chat(self._runtime):
                detail = self._runtime_note or (
                    f"{type(self._runtime).__name__} does not serve /v1/chat/completions"
                )
                raise InferenceUnavailableError(
                    f"No chat-capable inference runtime: {detail}"
                )
            self._model = resolved
            self._model.state = ModelState.BUSY
        self._cancel_event.clear()
        cancel_event = self._cancel_event

        try:
            response = self._runtime.chat_generate(request, cancel_event=cancel_event)
        except TypeError:
            response = self._runtime.chat_generate(request)
            with self._lock:
                if self._model is not None:
                    self._model.state = ModelState.READY
            return response
        except RequestCancelledError:
            with self._lock:
                if self._model is not None:
                    self._model.state = ModelState.READY
            raise
        except Exception as e:
            with self._lock:
                if self._model is not None:
                    self._model.state = ModelState.READY
            raise e
        with self._lock:
            if self._model is not None:
                self._model.state = ModelState.READY
        return response

    def chat_generate_stream(self, request: ChatRequest, cancel_event: Optional[threading.Event] = None, read_timeout: float = 5.0):
        """Streaming variant of chat_generate.

        Yields (piece, finish_reason, tokens) events from the runtime's
        streamed chat completion: one event per content delta, then a final
        event with the finish reason and completion token count. A busy
        server still counts as loaded (see _probe_runtime_liveness), so a
        cancelled or inactivity-timed-out call never breaks the next one.
        ``read_timeout`` paces the underlying reads so an inactivity watch
        wakes at least that often while the server is busy.
        """
        with self._lock:
            resolved = self._resolved_model_state_locked()
            if resolved is None or resolved.state != ModelState.READY:
                raise InferenceUnavailableError("No model loaded")
            if not hasattr(self._runtime, "chat_generate_stream"):
                response = self.chat_generate(request, cancel_event=cancel_event)
                yield (response.text, response.finish_reason, response.tokens_generated)
                return
            self._model = resolved
            self._model.state = ModelState.BUSY
        self._cancel_event.clear()
        if cancel_event is None:
            cancel_event = self._cancel_event
        try:
            yield from self._runtime.chat_generate_stream(request, cancel_event=cancel_event, read_timeout=read_timeout)
        finally:
            with self._lock:
                if self._model is not None:
                    self._model.state = ModelState.READY

    def cancel(self) -> None:
        """Cancel the in-flight generation; the model stays loaded.

        The state goes back to READY (not a separate CANCELLED state): a
        cancel must never make the next request fail - only the cancel event
        records that the current call was abandoned.
        """
        self._cancel_event.set()
        with self._lock:
            if self._model is not None and self._model.state == ModelState.BUSY:
                self._model.state = ModelState.READY

    def _log_load_failure(self, model_name: str, detail: str) -> None:
        """Surface a load failure to the rotating log so it is never silent."""
        log = _get_logger_for_inference()
        log.warning("Auto-load failed for model '%s': %s", model_name, detail)
