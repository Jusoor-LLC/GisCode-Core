"""Model Manager: local providers, profiles and the two tests.

This panel is additive. Everything the Model Manager already did - discover GGUF
files, load, unload, report the backend - is untouched and stays above it.

What it adds:

* a live status line per configured provider (Ollama, LM Studio, custom
  endpoints), so "configured" is never confused with "running";
* an add-endpoint row for any OpenAI-compatible server;
* profiles per purpose (coding, reasoning, chat, vision);
* **Test connection** and **Test coding**, which report different things on
  purpose - reachable is not the same as capable.
"""
from __future__ import annotations

import threading

import tkinter as tk
from tkinter import ttk

from giscode.providers.base import DEFAULT_ENDPOINTS, LLAMA_CPP, ProviderError
from giscode.providers.hardware import detect_hardware
from giscode.providers.profiles import PURPOSES, ProfileStore, default_label
from giscode.providers.registry import ProviderRegistry
from giscode.providers.settings_adapter import as_settings_source
from giscode.providers.testing import coding_test, connection_test


def build_provider_panel(parent, window):
    """Build the panel into *parent* and return it (or None when unavailable)."""
    settings = as_settings_source(window._settings_source())
    if settings is None:
        ttk.Label(
            parent,
            text="Provider settings are unavailable in this session.",
            foreground="gray",
        ).pack(anchor=tk.W, pady=(10, 0))
        return None

    registry = ProviderRegistry(settings)
    profiles = ProfileStore(settings)
    migrated = profiles.migrate()

    state: dict = {"models": [], "provider_id": ""}

    ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=(12, 8))
    ttk.Label(parent, text="Local providers", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)

    status_var = tk.StringVar(value="Press Refresh to ask each provider whether it is running.")
    ttk.Label(parent, textvariable=status_var, wraplength=900, justify=tk.LEFT, foreground="gray").pack(
        anchor=tk.W, pady=(4, 4)
    )

    row = ttk.Frame(parent)
    row.pack(anchor=tk.W, pady=(0, 6))

    label_var = tk.StringVar()
    endpoint_var = tk.StringVar()

    def refresh_statuses() -> None:
        lines = []
        for status in registry.statuses():
            mark = "OK " if status.available else "-- "
            lines.append(mark + status.describe())
        status_var.set("\n".join(lines) if lines else "No providers are enabled.")

    ttk.Button(row, text="Refresh providers", command=refresh_statuses).pack(side=tk.LEFT, padx=(0, 6))

    add_row = ttk.Frame(parent)
    add_row.pack(anchor=tk.W, pady=(0, 4))
    ttk.Label(add_row, text="Add OpenAI-compatible endpoint:").pack(side=tk.LEFT)
    ttk.Entry(add_row, textvariable=label_var, width=16).pack(side=tk.LEFT, padx=(6, 4))
    ttk.Entry(add_row, textvariable=endpoint_var, width=28).pack(side=tk.LEFT, padx=(0, 4))

    # ----- attachments and remote endpoints -------------------------------
    # A remote endpoint answering text is one decision; uploading a picture to
    # it is another. This is the second one, and it starts off.
    consent_var = tk.BooleanVar(value=bool(settings.get("providers.remote_attachments", False)))

    def toggle_attachment_consent() -> None:
        settings.set("providers.remote_attachments", bool(consent_var.get()))
        try:
            settings.save()
        except Exception:
            pass

    consent_row = ttk.Frame(parent)
    consent_row.pack(anchor=tk.W, pady=(2, 0))
    ttk.Checkbutton(
        consent_row,
        text="Allow images to be sent to remote (non-loopback) providers",
        variable=consent_var,
        command=toggle_attachment_consent,
    ).pack(side=tk.LEFT)
    ttk.Label(
        parent,
        text=(
            "Off by default. Until this is enabled, a remote endpoint can answer "
            "text but an attached image is refused before it is sent. Local "
            "providers are never affected."
        ),
        foreground="gray",
        wraplength=900,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(0, 4))

    # ----- profiles -------------------------------------------------------
    ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=(10, 8))
    ttk.Label(parent, text="Model profiles", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)

    purpose_var = tk.StringVar(value="coding")
    provider_var = tk.StringVar()
    model_var = tk.StringVar()
    profile_status = tk.StringVar(
        value="A profile binds a purpose to a provider and model, so switching is a selection."
    )

    grid = ttk.Frame(parent)
    grid.pack(anchor=tk.W, pady=(4, 4))
    ttk.Label(grid, text="Purpose:").grid(row=0, column=0, sticky=tk.W)
    ttk.Combobox(
        grid, textvariable=purpose_var, values=list(PURPOSES), state="readonly", width=12
    ).grid(row=0, column=1, sticky=tk.W, padx=(4, 12))
    ttk.Label(grid, text="Provider:").grid(row=0, column=2, sticky=tk.W)
    provider_combo = ttk.Combobox(
        grid, textvariable=provider_var, state="readonly", width=22
    )
    provider_combo.grid(row=0, column=3, sticky=tk.W, padx=(4, 12))
    ttk.Label(grid, text="Model:").grid(row=0, column=4, sticky=tk.W)
    model_combo = ttk.Combobox(grid, textvariable=model_var, state="readonly", width=30)
    model_combo.grid(row=0, column=5, sticky=tk.W, padx=(4, 0))

    actions = ttk.Frame(parent)
    actions.pack(anchor=tk.W, pady=(2, 4))

    hardware_text = detect_hardware().summary()
    ttk.Label(
        parent,
        text=f"Detected hardware: {hardware_text}",
        foreground="gray",
        wraplength=900,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(6, 0))
    ttk.Label(
        parent,
        textvariable=profile_status,
        wraplength=900,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(4, 0))

    def load_provider_models(_event=None) -> None:
        provider_id = provider_var.get().strip()
        state["provider_id"] = provider_id
        provider = registry.get(provider_id)
        if provider is None:
            model_combo["values"] = []
            return
        try:
            models = provider.list_models()
        except ProviderError as exc:
            state["models"] = []
            model_combo["values"] = []
            profile_status.set(f"{provider.label}: {exc} {exc.action}".strip())
            return
        state["models"] = models
        values = [m.id for m in models]
        model_combo["values"] = values
        if values and model_var.get() not in values:
            model_var.set(values[0])
        profile_status.set(
            f"{provider.label}: {len(models)} model(s) available."
            if models
            else f"{provider.label} is running but reports no models."
        )

    def save_profile() -> None:
        provider_id = provider_var.get().strip()
        model = model_var.get().strip()
        purpose = purpose_var.get().strip()
        if not provider_id or not model:
            profile_status.set("Choose a provider and a model first.")
            return
        existing = next(
            (
                p
                for p in profiles.for_purpose(purpose)
                if p.provider == provider_id and p.model == model
            ),
            None,
        )
        if existing is not None:
            profiles.set_active(existing.id)
            profile = existing
        else:
            profile = profiles.create(
                purpose,
                provider=provider_id,
                model=model,
                label=f"{default_label(purpose)} - {provider_id}",
                make_active=True,
            )
        profiles.mark_good(profile.id)
        profile_status.set(
            f"Active {purpose} profile: {profile.label} ({profile.provider}: {profile.model})."
        )
        # For the built-in runtime the existing loader still owns loading, so
        # keep its selection in step with the profile.
        if provider_id == LLAMA_CPP:
            try:
                window._model_var.set(model)
            except Exception:
                pass

    def add_endpoint() -> None:
        label = (label_var.get() or "").strip() or "Local endpoint"
        endpoint = (endpoint_var.get() or "").strip()
        try:
            provider_id = registry.add_custom(label, endpoint)
        except (ValueError, ProviderError) as exc:
            profile_status.set(f"Could not add that endpoint: {exc}")
            return
        label_var.set("")
        endpoint_var.set("")
        provider_combo["values"] = registry.enabled_ids()
        provider_var.set(provider_id)
        profile_status.set(f"Added {label} at {endpoint}. Press Refresh providers to test it.")
        refresh_statuses()

    def run_test(kind: str) -> None:
        provider_id = provider_var.get().strip()
        model = model_var.get().strip()
        provider = registry.get(provider_id)
        if provider is None or not model:
            profile_status.set("Choose a provider and a model before testing.")
            return
        profile_status.set(f"Running the {kind} test against {provider.label}...")

        def worker() -> None:
            if kind == "connection":
                outcome = connection_test(provider, model)
            else:
                outcome = coding_test(provider, model)
            text = (
                f"{'PASS' if outcome.ok else 'FAIL'} - {kind} test, {provider.label} / "
                f"{model} ({outcome.seconds:.1f}s): {outcome.detail}"
            )

            def apply() -> None:
                profile_status.set(text)

            try:
                window.root.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    ttk.Button(actions, text="Save profile", command=save_profile).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(actions, text="Test connection", command=lambda: run_test("connection")).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(actions, text="Test coding", command=lambda: run_test("coding")).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(actions, text="Add endpoint", command=add_endpoint).pack(side=tk.LEFT)

    provider_combo["values"] = registry.enabled_ids()
    if registry.enabled_ids():
        provider_var.set(registry.enabled_ids()[0])
    provider_combo.bind("<<ComboboxSelected>>", load_provider_models)
    if migrated:
        profile_status.set(f"Migrated the previous model selection: {migrated}.")

    return {
        "refresh": refresh_statuses,
        "load_models": load_provider_models,
        "save_profile": save_profile,
        "add_endpoint": add_endpoint,
        "status_var": profile_status,
        "endpoint_defaults": dict(DEFAULT_ENDPOINTS),
    }
