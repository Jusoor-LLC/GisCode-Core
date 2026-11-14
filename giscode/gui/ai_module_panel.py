"""Native AI model selector for Core and Pro.

The panel is deliberately one screen: the recommendation, the catalogue facts a
person needs before a multi-gigabyte download, and Download / Load / Test /
Remove. All files, hashes and runtime decisions remain owned by
``AIModuleService``; this module only presents them.
"""
from __future__ import annotations

import threading
import webbrowser

import tkinter as tk
from tkinter import messagebox, ttk

from giscode.ai_module import AIModuleService
from giscode.ai_module.manifest import RUNTIME_VERSION


def _gib(value) -> str:
    try:
        return f"{int(value) / (1024 ** 3):.1f} GB"
    except (TypeError, ValueError):
        return "unknown"


def _details(row: dict | None, *, recommended: bool) -> str:
    if not row:
        return "Choose a model to see its size, requirements, licence and source."
    fit = ("Yes - " if row.get("fit") else "No - ") + str(row.get("fit_note") or "")
    if row.get("qualified"):
        qualification = row.get("qualification") or "Qualified by GisCode's real-inference check."
    else:
        qualification = (
            "Not yet qualified by GisCode. Select it explicitly and run Test Model "
            "before relying on it."
        )
    lines = [
        ("RECOMMENDED FOR THIS PC: " if recommended else "") + str(row.get("name") or ""),
        f"Publisher: {row.get('publisher') or 'unknown'}    "
        f"Parameters: {row.get('parameter_count') or 'unknown'}    "
        f"Quantization: {row.get('quantization') or 'unknown'}",
        f"Download: {_gib(row.get('size_bytes'))}    "
        f"Context: {int(row.get('context_length') or 0):,} tokens",
        f"Memory: at least {_gib(row.get('min_ram_bytes'))} RAM; "
        f"{_gib(row.get('recommended_vram_bytes'))} VRAM recommended for full GPU offload.",
        f"Fits this PC: {fit}",
        f"Expected performance (estimate, not a benchmark): "
        f"{row.get('expected_performance') or 'not published'}",
        f"Licence: {row.get('license') or 'unknown'}    "
        f"Commercial use: {'allowed' if row.get('commercial_use') else 'check the licence'}",
        f"Source: {row.get('source_url') or 'not published'}    "
        f"Revision: {row.get('revision') or 'main'}",
        "Integrity: SHA-256 verified against GisCode's pinned catalogue digest.",
        "",
        qualification,
        str(row.get("description") or ""),
    ]
    return "\n".join(lines)


def build_ai_module_panel(parent, window):
    """Build the native AI module selector and return its controls."""
    manager = getattr(window, "_model_manager", None)
    inference = getattr(manager, "_inference", None) if manager is not None else None
    settings = window._settings_source() if hasattr(window, "_settings_source") else None
    if manager is None or inference is None or settings is None:
        ttk.Label(
            parent,
            text="The native AI module is unavailable because the local model manager is not ready.",
            foreground="gray",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(12, 0))
        return None

    state: dict = {"busy": False, "names": {}, "rows": {}, "recommended": ""}
    status_var = tk.StringVar(value="Checking the local AI module...")
    recommendation_var = tk.StringVar(value="")
    detail_var = tk.StringVar(value="")
    hardware_var = tk.StringVar(value="")
    storage_var = tk.StringVar(value="")
    progress_var = tk.DoubleVar(value=0.0)
    model_var = tk.StringVar(value="")

    ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=(14, 8))
    ttk.Label(parent, text="AI Model Selector", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)
    ttk.Label(
        parent,
        text=(
            "GisCode installs its own verified llama.cpp runtime and downloads models "
            "directly from their publisher. No Ollama, terminal or environment variables are needed."
        ),
        foreground="gray",
        wraplength=920,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(2, 6))
    ttk.Label(parent, textvariable=status_var, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
    ttk.Label(
        parent,
        textvariable=recommendation_var,
        foreground="#1f5f8b",
        wraplength=920,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(2, 0))
    ttk.Label(
        parent,
        textvariable=detail_var,
        foreground="#555555",
        wraplength=920,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(2, 0))

    info = ttk.Frame(parent)
    info.pack(anchor=tk.W, fill=tk.X, pady=(6, 2))
    ttk.Label(info, textvariable=hardware_var, foreground="gray", wraplength=920, justify=tk.LEFT).pack(
        anchor=tk.W
    )
    ttk.Label(info, textvariable=storage_var, foreground="gray", wraplength=920, justify=tk.LEFT).pack(
        anchor=tk.W
    )

    choice = ttk.Frame(parent)
    choice.pack(anchor=tk.W, fill=tk.X, pady=(8, 2))
    ttk.Label(choice, text="Coding model:").pack(side=tk.LEFT)
    combo = ttk.Combobox(choice, textvariable=model_var, state="readonly", width=52)
    combo.pack(side=tk.LEFT, padx=(6, 8))
    progress = ttk.Progressbar(choice, variable=progress_var, maximum=100.0, length=220)
    progress.pack(side=tk.LEFT, padx=(4, 0))

    facts = ttk.LabelFrame(parent, text="Model details", padding=(8, 6))
    facts.pack(anchor=tk.W, fill=tk.X, pady=(8, 2))
    facts_var = tk.StringVar(value=_details(None, recommended=False))
    ttk.Label(facts, textvariable=facts_var, foreground="#333333", wraplength=900, justify=tk.LEFT).pack(
        anchor=tk.W
    )

    buttons = ttk.Frame(parent)
    buttons.pack(anchor=tk.W, pady=(8, 2))
    download_btn = ttk.Button(buttons, text="Download model")
    download_btn.pack(side=tk.LEFT, padx=(0, 6))
    load_btn = ttk.Button(buttons, text="Load / switch model")
    load_btn.pack(side=tk.LEFT, padx=(0, 6))
    test_btn = ttk.Button(buttons, text="Test model")
    test_btn.pack(side=tk.LEFT, padx=(0, 6))
    cancel_btn = ttk.Button(buttons, text="Cancel")
    cancel_btn.pack(side=tk.LEFT, padx=(0, 6))
    remove_btn = ttk.Button(buttons, text="Remove model")
    remove_btn.pack(side=tk.LEFT)

    links = ttk.Frame(parent)
    links.pack(anchor=tk.W, pady=(4, 0))
    source_btn = ttk.Button(links, text="Open model source")
    source_btn.pack(side=tk.LEFT, padx=(0, 6))
    licence_btn = ttk.Button(links, text="Open licence")
    licence_btn.pack(side=tk.LEFT)

    ttk.Label(
        parent,
        text=(
            f"Runtime: llama.cpp {RUNTIME_VERSION} (MIT), downloaded only from the official "
            "release and checked against its pinned SHA-256. Model weights keep their own "
            "publisher licence. Inference stays on this PC."
        ),
        foreground="gray",
        wraplength=920,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(6, 0))

    def set_busy(busy: bool) -> None:
        state["busy"] = busy
        value = "disabled" if busy else "normal"
        for button in (download_btn, load_btn, test_btn, remove_btn):
            button.configure(state=value)
        combo.configure(state="disabled" if busy else "readonly")

    def apply_event(event: dict) -> None:
        message = str(event.get("message") or "")
        if message:
            detail_var.set(message)
        fraction = event.get("progress")
        if fraction is not None:
            try:
                progress_var.set(max(0.0, min(1.0, float(fraction))) * 100.0)
            except (TypeError, ValueError):
                pass

    def emit(event: dict) -> None:
        try:
            window.root.after(0, lambda: apply_event(dict(event)))
        except Exception:
            pass

    service = AIModuleService(settings, manager, inference, on_event=emit)
    setattr(window, "_ai_module_service", service)

    def selected_key() -> str:
        return state["names"].get(model_var.get(), "")

    def show_details(_event=None) -> None:
        key = selected_key()
        row = state["rows"].get(key)
        facts_var.set(_details(row, recommended=key == state["recommended"]))
        source_btn.configure(state="normal" if row and row.get("source_url") else "disabled")
        licence_btn.configure(state="normal" if row and row.get("license_url") else "disabled")

    def open_url(kind: str) -> None:
        row = state["rows"].get(selected_key()) or {}
        url = row.get("source_url") if kind == "source" else row.get("license_url")
        if url:
            webbrowser.open(str(url))

    def refresh() -> None:
        def work() -> None:
            try:
                status = service.scan()
                recommended = service.recommended()
            except Exception as exc:
                def failed() -> None:
                    status_var.set("AI module status unavailable")
                    detail_var.set(f"{type(exc).__name__}: {exc}")

                window.root.after(0, failed)
                return

            def apply_status() -> None:
                status_var.set(f"{status.state.replace('_', ' ').title()}: {status.message}")
                hardware = status.hardware
                gpu = hardware.get("gpu_name") or "No supported GPU detected"
                hardware_var.set(
                    f"This PC: {_gib(hardware.get('total_ram_bytes'))} RAM "
                    f"({_gib(hardware.get('available_ram_bytes'))} free), "
                    f"{gpu}, {_gib(hardware.get('total_vram_bytes'))} VRAM. "
                    f"Backend: {status.backend or 'not selected'}."
                )
                storage = status.storage
                storage_var.set(
                    f"Storage: models {_gib(storage.get('models_bytes'))} in "
                    f"{storage.get('models_dir')}; runtime {_gib(storage.get('runtime_bytes'))} in "
                    f"{storage.get('runtime_dir')}; {_gib(storage.get('disk_free_bytes'))} free."
                )
                names: dict[str, str] = {}
                rows: dict[str, dict] = {}
                values: list[str] = []
                for row in status.models:
                    mark = " [downloaded]" if row.get("installed") else ""
                    mark += " [qualified]" if row.get("qualified") else " [not qualified]"
                    label = f"{row['name']} - {_gib(row['size_bytes'])}{mark}"
                    names[label] = row["key"]
                    rows[row["key"]] = row
                    values.append(label)
                state["names"] = names
                state["rows"] = rows
                state["recommended"] = recommended.key if recommended else ""
                combo["values"] = values
                if recommended:
                    recommendation_var.set(
                        f"Recommended for this PC: {recommended.name}. It is the smallest "
                        "qualified model that fits your available memory."
                    )
                elif any(row.get("qualified") and row.get("fit") for row in status.models):
                    recommendation_var.set(
                        "A qualified model fits, but its details are still being checked."
                    )
                else:
                    recommendation_var.set(
                        "No qualified model is a safe automatic fit for this PC. You may select a "
                        "model explicitly, but GisCode will not silently activate an unqualified model."
                    )
                preferred_key = recommended.key if recommended else status.model_key
                current = next(
                    (label for label, key in names.items() if key == preferred_key),
                    values[0] if values else "",
                )
                if current and not state["busy"]:
                    model_var.set(current)
                if status.state == "ready":
                    progress_var.set(100.0)
                if status.last_status and not detail_var.get():
                    detail_var.set(f"Last test: {status.last_status}")
                show_details()

            window.root.after(0, apply_status)

        threading.Thread(target=work, name="giscode-ai-module-scan", daemon=True).start()

    def finish(result) -> None:
        set_busy(False)
        detail_var.set(result.message)
        label = {
            "DOWNLOADED": "Download complete",
            "READY": "Model ready",
            "READY - SLOW": "Model ready (slow)",
            "CANCELLED": "Cancelled",
        }.get(str(result.status).upper(), str(result.status).replace("_", " ").title())
        status_var.set(f"{label}: {'ready' if result.ok else 'needs attention'}")
        if result.ok:
            progress_var.set(100.0)
        try:
            window._status_var.set(result.message)
        except Exception:
            pass
        refresh()

    def run(action: str) -> None:
        if state["busy"]:
            return
        key = selected_key()
        if not key:
            detail_var.set("Choose a model first.")
            return
        set_busy(True)
        progress_var.set(0.0)
        detail_var.set("Starting...")
        status_var.set("Working...")

        def work() -> None:
            try:
                if action == "download":
                    result = service.download_model(key)
                elif action == "load":
                    result = service.load_module(key)
                elif action == "test":
                    result = service.test_module(key)
                else:
                    result = service.remove_model(key, confirm=True)
            except Exception as exc:
                from giscode.ai_module import FAILED, ModuleResult

                result = ModuleResult(FAILED, False, f"{type(exc).__name__}: {exc}")
            window.root.after(0, lambda: finish(result))

        threading.Thread(target=work, name=f"giscode-ai-module-{action}", daemon=True).start()

    def remove() -> None:
        key = selected_key()
        if not key:
            return
        label = model_var.get()
        if not messagebox.askyesno(
            "Remove AI model",
            f"Remove {label}? The runtime stays installed and the model can be downloaded again.",
            parent=window.root,
        ):
            return
        run("remove")

    combo.bind("<<ComboboxSelected>>", show_details)
    download_btn.configure(command=lambda: run("download"))
    load_btn.configure(command=lambda: run("load"))
    test_btn.configure(command=lambda: run("test"))
    cancel_btn.configure(command=service.cancel)
    remove_btn.configure(command=remove)
    source_btn.configure(command=lambda: open_url("source"))
    licence_btn.configure(command=lambda: open_url("licence"))
    show_details()
    window.root.after(50, refresh)
    return {
        "service": service,
        "refresh": refresh,
        "status_var": status_var,
        "detail_var": detail_var,
        "download": lambda: run("download"),
        "load": lambda: run("load"),
        "test": lambda: run("test"),
    }
