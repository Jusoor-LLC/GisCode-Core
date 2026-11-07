"""The GisCode AdSpace panel for the free Core edition.

The panel is deliberately quiet: it is its own tab, it never overlays Chat,
Projects, model selection or attachment analysis, and it renders text only.
Every decision about *whether* an ad may appear belongs to
:class:`giscode.ads.service.AdSpace`; the panel just draws the answer, so the
edition rules cannot drift between the GUI and the service.

Pro never builds this panel.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from giscode.ads.service import AdSpace


class AdSpacePanel(ttk.Frame):
    """A small status panel: edition, consent, providers, and one creative."""

    TITLE = "GisCode AdSpace"

    def __init__(self, parent, service: AdSpace | None = None, **kwargs) -> None:
        super().__init__(parent, padding=10, **kwargs)
        self._service = service if service is not None else AdSpace()
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill=tk.X)
        ttk.Label(header, text=self.TITLE, font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        self._edition_label = ttk.Label(header, text="", foreground="gray")
        self._edition_label.pack(side=tk.RIGHT)

        self._state_label = ttk.Label(
            self, text="", foreground="#333333", wraplength=820, justify=tk.LEFT
        )
        self._state_label.pack(anchor=tk.W, pady=(6, 2))

        self._source_label = ttk.Label(
            self, text="", foreground="gray", wraplength=820, justify=tk.LEFT
        )
        self._source_label.pack(anchor=tk.W)

        # One creative, text only. Hidden unless the service says it may show.
        self._creative_frame = ttk.LabelFrame(self, text="Sponsored message", padding=8)
        self._headline_label = ttk.Label(
            self._creative_frame, text="", font=("Segoe UI", 11, "bold")
        )
        self._headline_label.pack(anchor=tk.W)
        self._body_label = ttk.Label(
            self._creative_frame, text="", wraplength=780, justify=tk.LEFT
        )
        self._body_label.pack(anchor=tk.W, pady=(4, 0))
        self._disclosure_label = ttk.Label(
            self._creative_frame, text="", foreground="gray", wraplength=780, justify=tk.LEFT
        )
        self._disclosure_label.pack(anchor=tk.W, pady=(4, 0))

        providers = ttk.LabelFrame(self, text="Providers", padding=8)
        providers.pack(fill=tk.X, pady=(10, 0))
        self._providers_frame = providers
        self._provider_rows: dict[str, ttk.Label] = {}
        for report in self._service.provider_reports():
            row = ttk.Frame(providers)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=f"{report.display_name}:", width=22).pack(side=tk.LEFT)
            label = ttk.Label(row, text="", foreground="gray", wraplength=640, justify=tk.LEFT)
            label.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._provider_rows[report.name] = label

        controls = ttk.Frame(self)
        controls.pack(fill=tk.X, pady=(10, 0))
        self._enabled_var = tk.BooleanVar(value=False)
        self._enabled_check = ttk.Checkbutton(
            controls,
            text="Allow GisCode to show advertising in this panel",
            variable=self._enabled_var,
            command=self._on_enabled_toggled,
        )
        self._enabled_check.pack(anchor=tk.W)
        self._consent_var = tk.BooleanVar(value=False)
        self._consent_check = ttk.Checkbutton(
            controls,
            text="I consent to receiving ad content",
            variable=self._consent_var,
            command=self._on_consent_toggled,
        )
        self._consent_check.pack(anchor=tk.W)

        ttk.Label(
            self,
            text=(
                "GisCode AdSpace never interrupts Chat, project work, model "
                "selection or attachment analysis. GisCode Pro is ad-free."
            ),
            foreground="#555555",
            wraplength=820,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))

    # ----------------------------------------------------------------- render
    def refresh(self) -> None:
        state = self._service.state()

        self._edition_label.configure(
            text="GisCode Pro — ad-free" if state.ad_free else "GisCode Core — free edition"
        )
        self._state_label.configure(text=state.describe())
        self._source_label.configure(
            text=f"Official documentation: {state.source}" if state.source else ""
        )

        for report in state.providers:
            label = self._provider_rows.get(report.name)
            if label is None:
                continue
            if report.availability.available:
                label.configure(
                    text="available", foreground="#1a7f37"
                )
            else:
                label.configure(
                    text=f"not available on Windows desktop — {report.availability.reason}",
                    foreground="#555555",
                )

        if state.show_ad and state.creative is not None:
            self._headline_label.configure(text=state.creative.headline)
            self._body_label.configure(text=state.creative.body)
            self._disclosure_label.configure(text=state.creative.disclosure)
            if not self._creative_frame.winfo_manager():
                self._creative_frame.pack(
                    fill=tk.X, pady=(10, 0), before=self._providers_frame
                )
        else:
            if self._creative_frame.winfo_manager():
                self._creative_frame.pack_forget()

        self._enabled_var.set(bool(state.enabled))
        self._consent_var.set(bool(state.consent))
        # A setting that cannot be saved must not look adjustable.
        self._enabled_check.configure(state=tk.DISABLED if state.ad_free else tk.NORMAL)
        self._consent_check.configure(
            state=tk.DISABLED if (state.ad_free or not state.enabled) else tk.NORMAL
        )

    # ---------------------------------------------------------------- actions
    def _on_enabled_toggled(self) -> None:
        self._service.set_enabled(bool(self._enabled_var.get()))
        self.refresh()

    def _on_consent_toggled(self) -> None:
        self._service.set_consent(bool(self._consent_var.get()))
        self.refresh()
