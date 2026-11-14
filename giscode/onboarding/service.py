"""The onboarding sequence as one resumable state machine.

Each step is a method that (a) does one thing, (b) records the evidence in the
persisted state, and (c) returns a plain dict the GUI or the qualification CLI
can display. Nothing here talks to Tk, so the whole sequence is testable and can
run on a clean machine without a desktop.

Order, and why it is this order:

``runtime`` -> ``hardware`` -> ``model`` -> ``download`` -> ``validate`` ->
``first_task`` -> ``complete``

Hardware is measured *before* a model is chosen so the recommendation is about
this machine; validation happens *after* the pull and before configuration, so a
model that cannot answer never becomes the configured one; and ``complete`` is
only reachable through a passed first task, because that is the product working
end to end rather than a model existing on disk.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from giscode.config.manager import ConfigManager
from giscode.config.settings import SettingsManager
from giscode.onboarding import benchmark as benchmark_module
from giscode.onboarding import diagnostics as diagnostics_module
from giscode.onboarding import download as download_module
from giscode.onboarding import first_task as first_task_module
from giscode.onboarding import hardware as hardware_module
from giscode.onboarding import recommend as recommend_module
from giscode.onboarding import runtime as runtime_module
from giscode.onboarding import validate as validate_module
from giscode.onboarding.state import (
    DOWNLOAD_DONE,
    OnboardingState,
    OnboardingStore,
    now,
)
from giscode.providers.base import DEFAULT_ENDPOINTS, OLLAMA
from giscode.providers.ollama import OllamaProvider

#: The provider id onboarding configures. Ollama is the first supported runtime;
#: nothing here is written in a way that assumes it must stay the only one.
PROVIDER_ID = OLLAMA


class OnboardingService:
    """Owns the first-run state and every action the wizard can take."""

    def __init__(
        self,
        *,
        settings=None,
        config_path=None,
        provider=None,
        puller=None,
    ) -> None:
        self._config_path = Path(config_path) if config_path else None
        self._store = OnboardingStore(self._config_path)
        self._config = ConfigManager(self._config_path)
        self._settings = settings or SettingsManager(self._config_path)
        self._provider = provider
        self._puller = puller
        self.state = self._store.load()
        # A store that had to reconcile an interrupted download must persist the
        # correction, otherwise every later read repeats the same repair.
        if self.state.download.get("status") == "interrupted":
            self._save()

    # -- persistence --------------------------------------------------------
    @property
    def config_path(self) -> str:
        return str(self._store.path)

    def _save(self) -> OnboardingState:
        self.state = self._store.save(self.state)
        return self.state

    def reload(self) -> OnboardingState:
        self.state = self._store.load()
        return self.state

    # -- helpers ------------------------------------------------------------
    def _provider_or_default(self, provider=None):
        chosen = provider or self._provider
        if chosen is not None:
            return chosen
        return OllamaProvider(endpoint=DEFAULT_ENDPOINTS[OLLAMA], timeout=3.0)

    def _puller_or_default(self) -> download_module.OllamaPuller:
        if self._puller is None:
            # The HTTP pull is Ollama's own supported model-pull API. The CLI
            # remains the fallback for an older installation that has no HTTP
            # service available; both paths keep consent and cancellation.
            ready = bool(self.state.runtime.get("service_available"))
            self._puller = download_module.OllamaPuller(
                executable=str(self.state.runtime.get("executable") or ""),
                transport="api" if ready else "cli",
                endpoint=str(self.state.runtime.get("endpoint") or ""),
            )
        return self._puller

    def _profile(self) -> hardware_module.SystemProfile:
        stored = hardware_module.SystemProfile.from_dict(self.state.hardware)
        if stored.ram_total_bytes or stored.disk_total_bytes:
            return stored
        return hardware_module.probe()

    def existing_installation(self) -> bool:
        """True when this profile already has a configured GisCode.

        Used for the upgrade path: a user who has been running GisCode must not
        be dropped into first-run setup, and nothing of theirs is rewritten.
        """
        data = self._config.load()
        model = data.get("model")
        if isinstance(model, dict) and str(model.get("selected_model") or "").strip():
            return True
        launch = data.get("launch")
        if isinstance(launch, dict) and bool(launch.get("LaunchSeen")):
            return True
        profiles = data.get("profiles")
        if isinstance(profiles, dict) and profiles.get("items"):
            return True
        return False

    # -- sequence -----------------------------------------------------------
    def begin(self) -> dict:
        """Start, or recognise an existing installation and finish immediately."""
        if not self.state.started_at:
            self.state.started_at = now()
            self.state.note("started")
        if not self.state.completed and self.existing_installation():
            self.state.legacy = True
            self.state.completed = True
            self.state.stage = "complete"
            self.state.note(
                "legacy_install",
                "existing configuration detected; first-run setup was not forced",
            )
        self._save()
        return self.status()

    def skip_for_existing_install(self) -> dict:
        """Explicit upgrade path, for a caller that decided not to prompt."""
        self.state.legacy = True
        self.state.completed = True
        self.state.stage = "complete"
        self.state.note("skipped", "existing installation kept as it was")
        self._save()
        return self.status()

    def detect_runtime(self, *, provider=None) -> dict:
        report = runtime_module.detect(self._provider_or_default(provider))
        self.state.advance("runtime", runtime=report.as_dict())
        if not report.ready:
            self.state.note("runtime_not_ready", report.detail)
        self._save()
        return report.as_dict()

    def install_instructions(self) -> dict:
        """Show the official route. Reading this installs nothing."""
        return runtime_module.install_instructions()

    def confirm_install(self) -> dict:
        """Record that the user chose to open the official installer page."""
        self.state.consents["ollama_install"] = now()
        self.state.note("install_consent")
        self._save()
        return runtime_module.install_instructions()

    def assess_hardware(self, **kwargs) -> dict:
        profile = hardware_module.probe(
            path=kwargs.pop("path", None),
            budget=kwargs.pop("budget", None),
            hardware=kwargs.pop("hardware", None),
            **kwargs,
        )
        self.state.advance("hardware", hardware=profile.as_dict())
        self._save()
        return profile.as_dict()

    def plan(self, *, catalog=None, limit: int = 4) -> dict:
        profile = self._profile()
        installed = tuple(self.state.runtime.get("models") or ())
        plan = recommend_module.recommend(
            profile, catalog=catalog, installed=installed, limit=limit
        )
        payload = plan.as_dict()
        payload["insufficient_explanation"] = (
            recommend_module.describe_insufficient(plan, profile) if not plan.recommended else ""
        )
        payload["profile"] = profile.as_dict()
        best = payload.get("best")
        if best:
            payload["storage"] = hardware_module.advise_storage(
                profile, int(best.get("size_bytes") or 0)
            ).as_dict()
        else:
            payload["storage"] = None
        return payload

    def select_model(self, model: str, *, size_bytes: int = 0) -> dict:
        name = (model or "").strip()
        if not name:
            raise ValueError("a model tag is required")
        size = int(size_bytes) or recommend_module.model_size_bytes(name)
        self.state.advance("model", chosen_model=name)
        self.state.download = {"model": name, "size_bytes": size, "status": "ready"}
        self.state.note("model_selected", name)
        self._save()
        return {"model": name, "size_bytes": size}

    def download_preflight(self, model: str = "") -> dict:
        """Disk check and large-download warning shown before a transfer."""
        name = (model or self.state.chosen_model or "").strip()
        stored = self.state.download or {}
        size = int(stored.get("size_bytes") or 0) if stored.get("model") == name else 0
        size = size or recommend_module.model_size_bytes(name)
        advice_value = hardware_module.advise_storage(self._profile(), size)
        advice = advice_value.as_dict() if hasattr(advice_value, "as_dict") else dict(advice_value)
        advice["model"] = name
        advice["size_bytes"] = int(size)
        advice["size_gb"] = round(size / hardware_module.GIB, 1) if size else 0.0
        advice["large"] = size >= 5 * hardware_module.GIB
        advice["warning"] = (
            f"{name} is a large download (approximately {advice['size_gb']:.1f} GB). "
            "It uses your connection and disk space; nothing starts until you confirm."
            if advice["large"]
            else ""
        )
        self.state.storage = advice
        return advice

    def confirm_download(self, model: str = "") -> dict:
        """Record the explicit consent a pull requires. Transfers nothing."""
        name = (model or self.state.chosen_model or "").strip()
        if not name:
            raise ValueError("choose a model before confirming a download")
        self.state.consents[f"download:{name}"] = now()
        self.state.advance("download", chosen_model=name)
        self.state.note("download_consent", name)
        preflight = self.download_preflight(name)
        self._save()
        return {
            "model": name,
            "size_bytes": int(self.state.download.get("size_bytes") or 0),
            "confirmed": True,
            "storage": preflight,
            "warning": preflight.get("warning") or "",
        }

    def start_download(self, model: str = "") -> dict:
        """Start ``ollama pull``. Refuses without a recorded confirmation."""
        name = (model or self.state.chosen_model or "").strip()
        if not name:
            raise ValueError("choose a model before downloading")
        if not self.state.consents.get(f"download:{name}"):
            raise PermissionError(
                f"Downloading {name} has not been confirmed by the user, so nothing was transferred."
            )
        if not self.state.runtime.get("service_available"):
            raise RuntimeError(
                "Ollama is not answering. Start it, press Check again, then download; "
                "nothing was transferred."
            )
        preflight = self.download_preflight(name)
        if not preflight.get("sufficient"):
            instructions = " ".join(preflight.get("instructions") or ())
            raise RuntimeError(preflight.get("detail", "Not enough disk space.") + " " + instructions)
        installed = tuple(self.state.runtime.get("models") or ())
        puller = self._puller_or_default()
        job = puller.start(name, confirm=True, installed=installed)
        self.state.advance("download", download=job.as_dict())
        self.state.note("download_started", f"{name} attempt {job.attempts}")
        self._save()
        return job.as_dict()

    def download_status(self, model: str = "") -> dict:
        name = (model or self.state.chosen_model or "").strip()
        puller = self._puller
        live = puller.job(name) if puller is not None else None
        if live is not None:
            payload = live.as_dict()
            # Verification is recorded after the pull ends. A terminal job's
            # as_dict has no slot for it, so keep the evidence from the state
            # rather than making the UI lose it on the next poll.
            if payload.get("status") == download_module.DONE and self.state.download.get("model") == name:
                for key in ("verified", "verification_detail"):
                    if key in self.state.download:
                        payload[key] = self.state.download[key]
            if payload != self.state.download:
                self.state.download = payload
                self._save()
            return payload
        return dict(self.state.download)

    def wait_download(self, model: str = "", *, timeout: float = 0.0) -> dict:
        name = (model or self.state.chosen_model or "").strip()
        puller = self._puller
        if puller is not None:
            puller.wait(name, timeout=timeout)
        payload = self.download_status(name)
        if payload.get("status") == download_module.DONE:
            present, detail = self._verify_installed(name)
            payload["verified"] = bool(present)
            payload["verification_detail"] = detail
            self.state.download = payload
            if present:
                models = [str(item) for item in (self.state.runtime.get("models") or ())]
                if not download_module.model_installed(name, models):
                    models.append(name)
                    self.state.runtime = {**self.state.runtime, "models": sorted(models)}
            self._save()
        return payload

    def _verify_installed(self, model: str) -> tuple[bool, str]:
        """Ask Ollama's model list rather than trusting the pull's last line."""
        provider = self._provider_or_default()
        try:
            models = [str(item.id) for item in provider.list_models()]
        except Exception as exc:
            return False, f"The model list could not be checked: {type(exc).__name__}: {exc}"
        present = download_module.model_installed(model, models)
        return (
            present,
            f"{model} appears in Ollama's model list"
            if present
            else f"Ollama finished the pull, but {model} is not in its model list",
        )

    def cancel_download(self, model: str = "") -> dict:
        name = (model or self.state.chosen_model or "").strip()
        puller = self._puller
        if puller is not None:
            puller.cancel(name)
        payload = self.download_status(name)
        self.state.note("download_cancelled", name)
        self._save()
        return payload

    def retry_download(self, model: str = "") -> dict:
        name = (model or self.state.chosen_model or "").strip()
        if not self.state.consents.get(f"download:{name}"):
            raise PermissionError(
                "A retry is another transfer, so it needs the same confirmation as the first attempt."
            )
        puller = self._puller_or_default()
        job = puller.retry(name, confirm=True)
        self.state.advance("download", download=job.as_dict())
        self.state.bump_attempt(f"download:{name}")
        self.state.note("download_retry", f"{name} attempt {job.attempts}")
        self._save()
        return job.as_dict()

    def validate_model(self, model: str = "", *, provider=None, chat=None) -> dict:
        """Test My AI: a real request, then the verdict, persisted."""
        name = (model or self.state.chosen_model or "").strip()
        if not name:
            raise ValueError("choose a model before testing it")
        provider = self._provider_or_default(provider)
        available = None
        try:
            available = [item.id for item in provider.list_models()]
        except Exception:
            available = tuple(self.state.runtime.get("models") or ()) or None

        self.state.bump_attempt(f"validate:{name}")
        result = validate_module.validate(provider, name, available_models=available, chat=chat)
        self.state.advance("validate", validation=result.as_dict())
        if result.ok:
            self.state.advance("first_task")
            self.state.clear_error()
            self.state.note("validated", name)
        else:
            self.state.fail(result.detail)
        self._save()
        return result.as_dict()

    def benchmark_model(self, model: str = "", *, provider=None, stream=None, runner=None) -> dict:
        """Test This Model: measured latency, speed and correctness."""
        name = (model or self.state.chosen_model or "").strip()
        if not name:
            raise ValueError("choose a model before benchmarking it")
        provider = self._provider_or_default(provider)
        available = None
        try:
            available = [item.id for item in provider.list_models()]
        except Exception:
            available = tuple(self.state.runtime.get("models") or ()) or None
        self.state.bump_attempt(f"benchmark:{name}")
        result = benchmark_module.run_benchmark(
            provider,
            name,
            available_models=available,
            stream=stream,
            runner=runner,
        )
        payload = result.as_dict()
        self.state.advance("validate", benchmark=payload)
        self.state.note("benchmark", f"{name}: {payload.get('verdict')}")
        self._save()
        return payload

    def diagnostics_report(self) -> dict:
        """Build the exact allowlisted report the diagnostics ZIP will contain."""
        provider = self._provider_or_default()
        try:
            provider_status = provider.status()
            models = provider.list_models()
        except Exception as exc:
            provider_status = {"available": False, "detail": f"{type(exc).__name__}: {exc}"}
            models = ()
        try:
            from giscode import __version__ as product_version
        except Exception:
            product_version = "unknown"
        return diagnostics_module.build_report(
            product_version=str(product_version),
            status=self.status(),
            hardware=self.state.hardware,
            runtime=self.state.runtime,
            models=models,
            provider_status=provider_status,
            benchmark=self.state.benchmark,
            home=str(Path.home()),
        )

    def export_diagnostics(self, path: str = "") -> dict:
        """Write the privacy-reviewed ZIP; no source or conversation is read."""
        report = self.diagnostics_report()
        target = Path(path).expanduser() if path else diagnostics_module.default_path(
            home=hardware_module.giscode_home()
        )
        written = diagnostics_module.write_zip(target, report)
        self.state.note("diagnostics_exported", str(written))
        self._save()
        return {"path": str(written), "report": report, "preview": diagnostics_module.render_preview(report)}

    def configure_model(self, model: str = "") -> dict:
        """Make the validated model the coding model, and mark its profile good."""
        name = (model or self.state.chosen_model or "").strip()
        if not name:
            raise ValueError("no model to configure")
        if not self.state.validation.get("ok"):
            raise PermissionError(
                "The model has not passed Test My AI yet, so it will not be configured."
            )

        self._settings.set("model.selected_model", name)
        providers = self._settings.get("providers", {}) or {}
        if not isinstance(providers, dict):
            providers = {}
        config = dict(providers.get(PROVIDER_ID) or {})
        config.update(
            {
                "kind": OLLAMA,
                "label": config.get("label") or "Ollama",
                "endpoint": config.get("endpoint") or DEFAULT_ENDPOINTS[OLLAMA],
                "enabled": True,
                "allow_remote": bool(config.get("allow_remote", False)),
            }
        )
        providers[PROVIDER_ID] = config
        self._settings.set("providers", providers)
        self._settings.save()

        profile_id = ""
        profile_error = ""
        try:
            from giscode.providers.profiles import ProfileStore

            store = ProfileStore(self._settings)
            active = store.active_for("coding")
            if active is not None and active.provider == PROVIDER_ID:
                store.update(active.id, model=name, label=f"Onboarding: {name}")
                profile_id = active.id
            else:
                created = store.create(
                    "coding",
                    provider=PROVIDER_ID,
                    model=name,
                    label=f"Onboarding: {name}",
                    make_active=True,
                )
                profile_id = created.id
            store.mark_good(profile_id)
        except Exception as exc:  # a profile is helpful, never required
            profile_error = f"{type(exc).__name__}: {exc}"

        self.state.note("configured", f"{name} profile={profile_id or 'none'}")
        self._save()
        return {
            "model": name,
            "selected_model": name,
            "provider": PROVIDER_ID,
            "profile_id": profile_id,
            "profile_error": profile_error,
        }

    def run_first_task(self, model: str = "", *, chat=None, verify=None, root=None) -> dict:
        """The gate: a real one-file coding task that has to pass its check."""
        name = (model or self.state.chosen_model or "").strip()
        if not self.state.validation.get("ok"):
            return {
                "ok": False,
                "detail": "Test My AI has not passed, so the first task was not attempted.",
                "checks": [],
            }
        if not name:
            return {"ok": False, "detail": "No model is configured.", "checks": []}

        self.state.bump_attempt(f"first_task:{name}")
        result = first_task_module.run_first_task(
            self._provider_or_default(), name, chat=chat, verify=verify, root=root
        )
        payload = result.as_dict()
        self.state.advance("first_task", first_task=payload)
        if result.ok:
            self.state.clear_error()
            self.state.completed = True
            self.state.advance("complete")
            self.state.note("first_task_passed", result.workspace)
        else:
            self.state.fail(result.detail)
        self._save()
        return payload

    def complete(self) -> dict:
        self.state.completed = True
        self.state.advance("complete")
        self.state.clear_error()
        self._save()
        return self.status()

    # -- reporting ----------------------------------------------------------
    def recovery_hint(self) -> str:
        """One concrete next action for whatever state the user is in."""
        state = self.state
        if state.completed:
            return "Setup is complete. Open a project to start using GisCode."
        # An interrupted transfer is the most actionable fact, even when the
        # runtime was never recorded (a restart after the pull started).
        download = state.download or {}
        status = str(download.get("status") or "")
        if status in ("failed", "cancelled", "interrupted"):
            return (
                f"The download of {download.get('model') or 'the model'} did not finish "
                f"({status}). Press Retry to resume it; nothing is sent until you do."
            )
        runtime = state.runtime or {}
        if not runtime:
            return "Press Check to look for Ollama on this machine."
        if not runtime.get("service_available"):
            if not runtime.get("installed"):
                return "Install Ollama from the official page, start it, then press Check again."
            return runtime.get("action") or runtime_module.service_start_hint()
        if state.validation and not state.validation.get("ok"):
            return "Test My AI failed. Fix the reason shown above, then press Test My AI again."
        if state.first_task and not state.first_task.get("ok"):
            return "The first coding task did not pass. Press Run again to retry it."
        if state.stage == "model":
            return "Choose a model from the list, then confirm the download."
        if state.stage == "download":
            return "Confirm the download, then watch its progress."
        if state.stage == "validate":
            return "Press Test My AI to prove the model answers on this machine."
        if state.stage == "first_task":
            return "Run the first coding task; setup finishes when it passes."
        return f"Continue setup from the '{state.stage}' step."

    def status(self) -> dict:
        state = self.state
        return {
            "schema": state.schema,
            "stage": state.stage,
            "completed": state.completed,
            "legacy": state.legacy,
            "chosen_model": state.chosen_model,
            "runtime": dict(state.runtime),
            "hardware": dict(state.hardware),
            "download": dict(state.download),
            "storage": dict(state.storage),
            "validation": dict(state.validation),
            "benchmark": dict(state.benchmark),
            "first_task": dict(state.first_task),
            "consents": dict(state.consents),
            "attempts": dict(state.attempts),
            "last_error": state.last_error,
            "recovery_hint": self.recovery_hint(),
            "history": list(state.history[-12:]),
            "config_path": self.config_path,
        }
