"""Provider-neutral AI Web Chat surface for GisCode Core.

The view owns three things only: the provider selector, the user-reviewed
context handoff, and the import of an answer as explicitly unverified
guidance.  Every provider-specific string, URL and capability comes from the
provider definition in :mod:`giscode.webchat.providers`, so adding a qualified
adapter does not change this module.

Nothing here types into a provider's page, reads its DOM, or extracts cookies.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from giscode.gui.text_interaction import add_text_context_menu
from giscode.webchat.context import (
    ContextBuildError,
    WebChatContext,
    build_web_chat_context,
)
from giscode.webchat.guidance import (
    GuidanceError,
    WebChatGuidance,
    build_web_chat_guidance,
)
from giscode.webchat.host import BrowserStartError, BrowserUnavailableError
from giscode.webchat.providers import EXTERNAL, WebChatProvider
from giscode.webchat.registry import (
    DEFAULT_REGISTRY,
    ProviderSelection,
    WebChatRegistry,
)
from giscode.webchat.session import WebChatSessionManager


class SettingsBridge:
    """Adapts the window's lazy settings helpers to ``get``/``set``.

    The window creates its ``SettingsManager`` on first use, so the view must
    not hold a settings object at construction time.  ``set`` raises when the
    store refused the write, which :class:`ProviderSelection` reports instead
    of pretending the provider choice stuck.
    """

    def __init__(
        self,
        getter: Callable[..., object],
        setter: Callable[[str, object], bool],
    ) -> None:
        self._getter = getter
        self._setter = setter

    def get(self, key: str, default=None):
        try:
            return self._getter(key, default)
        except Exception:
            return default

    def set(self, key: str, value) -> None:
        if not self._setter(key, value):
            raise RuntimeError("the profile settings are not writable")


class AIWebChatView(ttk.Frame):
    """Provider-neutral browser, context handoff and guidance import."""

    #: Tests and embedding code can replace this with a host double.  It
    #: receives a :class:`~giscode.webchat.adapters.WebChatAdapter`.
    host_factory: Optional[Callable[..., object]] = None

    def __init__(
        self,
        parent,
        *,
        project_root_provider: Optional[Callable[[], Optional[str]]] = None,
        on_guidance: Optional[Callable[[str], None]] = None,
        registry: Optional[WebChatRegistry] = None,
        settings=None,
        session_manager: Optional[WebChatSessionManager] = None,
        host=None,
        provider_id: Optional[str] = None,
    ) -> None:
        super().__init__(parent, padding=10)
        self._project_root_provider = project_root_provider
        self._on_guidance = on_guidance
        self._registry = registry or DEFAULT_REGISTRY
        self._selection = (
            ProviderSelection(settings, self._registry) if settings is not None else None
        )
        self._fixed_host = host
        self._sessions = session_manager or WebChatSessionManager(
            self._registry, host_factory=self.host_factory
        )
        self._host = host
        self._provider_obj = self._initial_provider(provider_id)
        self._context: Optional[WebChatContext] = None
        self._guidance: Optional[WebChatGuidance] = None
        self._poll_job: Optional[str] = None
        self._closing = False
        self._browser_was_running = False

        self.status_var = tk.StringVar(value="Checking browser runtime…")
        self.project_var = tk.StringVar(value="No project selected")
        self.context_status_var = tk.StringVar(value="No context prepared")
        self.guidance_status_var = tk.StringVar(value="No guidance imported")
        self.provider_name_var = tk.StringVar()
        self.provider_status_var = tk.StringVar()
        self.auth_var = tk.StringVar()
        self.method_var = tk.StringVar()
        self.session_var = tk.StringVar()
        self.api_var = tk.StringVar()
        self.automation_var = tk.StringVar()
        self.local_var = tk.StringVar(
            value=(
                "Local models: GisCode's own backends (Ollama, LM Studio, "
                "llama.cpp) are selected in Model Manager, not here. Choosing a "
                "web-chat provider never changes your local model."
            )
        )

        self._build_ui()
        self._apply_provider(self._provider_obj, persist=False)
        self._schedule_poll()

    # ------------------------------------------------------------- provider
    def _initial_provider(self, provider_id: Optional[str]) -> WebChatProvider:
        if provider_id is not None:
            return self._registry.get(provider_id) or self._registry.default()
        if self._selection is not None:
            try:
                return self._selection.selected()
            except Exception:
                pass
        return self._registry.default()

    def _provider(self) -> WebChatProvider:
        """The provider currently shown, whether or not it is qualified."""
        return self._provider_obj

    def _provider_ids(self) -> list[str]:
        return [provider.id for provider in self._registry.all()]

    @staticmethod
    def _choice_label(provider: WebChatProvider) -> str:
        return f"{provider.display_name} — {provider.status_label}"

    def _provider_index(self, provider_id: str) -> int:
        ids = self._provider_ids()
        try:
            return ids.index(provider_id)
        except ValueError:
            return 0

    def _on_provider_changed(self, _event=None) -> None:
        index = self._provider_combo.current()
        ids = self._provider_ids()
        if 0 <= index < len(ids):
            self._select_provider(ids[index])

    def _select_provider(self, provider_id: str, *, persist: bool = True):
        """Switch provider without touching the project or the Chat tab."""
        provider = self._registry.get(provider_id)
        if provider is None:
            self.status_var.set(f"Unknown AI Web Chat provider: {provider_id}")
            return None
        self._apply_provider(provider, persist=persist)
        return provider

    def _apply_provider(self, provider: WebChatProvider, *, persist: bool) -> None:
        self._provider_obj = provider
        if persist and self._selection is not None:
            try:
                self._selection.select(provider.id)
            except Exception as exc:  # noqa: BLE001 - a failed save must not block
                self.status_var.set(f"Provider choice not saved: {type(exc).__name__}")
        self._provider_combo.current(self._provider_index(provider.id))
        # A prepared prompt names one provider, so it is never reused for
        # another provider.  The active project and the selected files stay.
        self._context = None
        self._guidance = None
        self._set_text(self._prompt_preview, "")
        self._set_text(self._guidance_preview, "")
        self.context_status_var.set("No context prepared")
        self.guidance_status_var.set("No guidance imported")
        self._host = self._fixed_host
        self._update_provider_labels()
        self._refresh_project()
        self._refresh_runtime()
        self._set_context_actions(False)

    def _update_provider_labels(self) -> None:
        provider = self._provider()
        name = provider.display_name
        self.provider_name_var.set(name)
        self.provider_status_var.set(provider.status_label)
        self.auth_var.set(provider.auth_guidance)
        self.method_var.set(f"Access method: {provider.method_label}")
        self.session_var.set(
            f"{provider.session_note} {provider.recovery_guidance}"
        )
        self.api_var.set(provider.api_label)
        self.automation_var.set(provider.automation_note)
        self._open_btn.configure(text=provider.open_label)
        self._reload_btn.configure(text=f"Reload {name}")
        self._open_with_prompt_btn.configure(text=f"Copy Prompt & Open {name}")
        self._question_label.configure(text=f"Question for {name}:")
        self._guidance_frame.configure(text=f"2. Import {name} guidance (unverified)")
        self._guidance_help.configure(
            text=(
                f"After {name} answers, copy its answer in the {name} window, then "
                "press Import. The block inserted into Chat is labelled advisory "
                "only; GisCode has not executed or verified it."
            )
        )

    # -------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill=tk.X)
        ttk.Label(
            header, text="AI Web Chat", font=("Segoe UI", 13, "bold")
        ).pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.status_var).pack(side=tk.RIGHT)

        ttk.Label(
            self,
            text=(
                "Open a provider-owned browser window, review a bounded context, "
                "and import the answer as unverified guidance. GisCode never "
                "scrapes the provider, reads browser cookies, or sends context "
                "automatically."
            ),
            wraplength=980,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(6, 6))

        provider_row = ttk.Frame(self)
        provider_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(provider_row, text="Provider:").pack(side=tk.LEFT)
        self._provider_combo = ttk.Combobox(provider_row, state="readonly", width=36)
        self._provider_combo["values"] = [
            self._choice_label(provider) for provider in self._registry.all()
        ]
        self._provider_combo.pack(side=tk.LEFT, padx=(6, 0))
        self._provider_combo.bind("<<ComboboxSelected>>", self._on_provider_changed)
        ttk.Label(provider_row, textvariable=self.provider_status_var).pack(
            side=tk.LEFT, padx=(10, 0)
        )
        ttk.Label(
            self,
            textvariable=self.auth_var,
            wraplength=980,
            justify=tk.LEFT,
            foreground="gray",
        ).pack(fill=tk.X)
        ttk.Label(
            self,
            textvariable=self.method_var,
            wraplength=980,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(2, 0))
        ttk.Label(
            self,
            textvariable=self.session_var,
            wraplength=980,
            justify=tk.LEFT,
            foreground="gray",
        ).pack(fill=tk.X)
        ttk.Label(
            self,
            textvariable=self.api_var,
            wraplength=980,
            justify=tk.LEFT,
            foreground="gray",
        ).pack(fill=tk.X)
        ttk.Label(
            self,
            textvariable=self.automation_var,
            wraplength=980,
            justify=tk.LEFT,
            foreground="gray",
        ).pack(fill=tk.X)
        ttk.Label(
            self,
            textvariable=self.local_var,
            wraplength=980,
            justify=tk.LEFT,
            foreground="gray",
        ).pack(fill=tk.X, pady=(0, 8))

        browser_row = ttk.Frame(self)
        browser_row.pack(fill=tk.X, pady=(0, 8))
        self._open_btn = ttk.Button(browser_row, text="Open", command=self._open_browser)
        self._open_btn.pack(side=tk.LEFT)
        self._reload_btn = ttk.Button(
            browser_row, text="Reload", command=self._reload_browser
        )
        self._reload_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._close_btn = ttk.Button(
            browser_row, text="Close Browser", command=self._close_browser
        )
        self._close_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._forget_btn = ttk.Button(
            browser_row, text="Forget Browser Session", command=self._forget_session
        )
        self._forget_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(
            browser_row,
            text="Each provider has its own isolated profile under .giscode.",
        ).pack(side=tk.RIGHT)

        context_frame = ttk.LabelFrame(self, text="1. Review context to send", padding=8)
        context_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        ttk.Label(context_frame, text="Active project:").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(
            context_frame, textvariable=self.project_var, wraplength=900
        ).grid(row=0, column=1, columnspan=3, sticky=tk.W, padx=(6, 0))
        ttk.Label(
            context_frame,
            text=(
                "Only files you add are sent. The absolute project path is not "
                "included. Without a project, only your question is prepared."
            ),
        ).grid(row=1, column=1, columnspan=3, sticky=tk.W, pady=(2, 6))

        ttk.Label(context_frame, text="Context files:").grid(row=2, column=0, sticky=tk.NW)
        self._files_list = tk.Listbox(context_frame, height=4, selectmode=tk.EXTENDED)
        self._files_list.grid(row=2, column=1, sticky=tk.NSEW, padx=(6, 4))
        file_buttons = ttk.Frame(context_frame)
        file_buttons.grid(row=2, column=2, sticky=tk.NW)
        ttk.Button(file_buttons, text="Add File…", command=self._add_files).pack(fill=tk.X)
        ttk.Button(file_buttons, text="Remove", command=self._remove_files).pack(
            fill=tk.X, pady=(4, 0)
        )
        ttk.Button(file_buttons, text="Clear", command=self._clear_files).pack(
            fill=tk.X, pady=(4, 0)
        )

        self._question_label = ttk.Label(context_frame, text="Question:")
        self._question_label.grid(row=3, column=0, sticky=tk.NW, pady=(8, 0))
        self._question_text = tk.Text(context_frame, height=4, wrap=tk.WORD)
        self._question_text.grid(
            row=3, column=1, columnspan=2, sticky=tk.NSEW, padx=(6, 4), pady=(8, 0)
        )
        add_text_context_menu(self._question_text)

        prompt_row = ttk.Frame(context_frame)
        prompt_row.grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
        self._prepare_btn = ttk.Button(
            prompt_row, text="Prepare Context", command=self._prepare_context
        )
        self._prepare_btn.pack(side=tk.LEFT)
        self._copy_btn = ttk.Button(
            prompt_row, text="Copy Prompt", command=self._copy_prompt, state=tk.DISABLED
        )
        self._copy_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._open_with_prompt_btn = ttk.Button(
            prompt_row,
            text="Copy Prompt & Open",
            command=self._copy_prompt_and_open,
            state=tk.DISABLED,
        )
        self._open_with_prompt_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(prompt_row, textvariable=self.context_status_var).pack(
            side=tk.LEFT, padx=(10, 0)
        )

        ttk.Label(context_frame, text="Prompt preview:").grid(
            row=5, column=0, sticky=tk.NW, pady=(8, 0)
        )
        self._prompt_preview = tk.Text(
            context_frame, height=9, wrap=tk.WORD, state=tk.DISABLED
        )
        self._prompt_preview.grid(
            row=5, column=1, columnspan=2, sticky=tk.NSEW, padx=(6, 4), pady=(8, 0)
        )
        context_frame.columnconfigure(1, weight=1)
        context_frame.rowconfigure(5, weight=1)

        self._guidance_frame = ttk.LabelFrame(
            self, text="2. Import guidance (unverified)", padding=8
        )
        self._guidance_frame.pack(fill=tk.BOTH, expand=True)
        guidance_actions = ttk.Frame(self._guidance_frame)
        guidance_actions.pack(fill=tk.X)
        self._import_btn = ttk.Button(
            guidance_actions,
            text="Import Response from Clipboard",
            command=self._import_clipboard,
        )
        self._import_btn.pack(side=tk.LEFT)
        self._use_guidance_btn = ttk.Button(
            guidance_actions,
            text="Insert into Chat as Unverified Guidance",
            command=self._use_guidance,
            state=tk.DISABLED,
        )
        self._use_guidance_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._clear_guidance_btn = ttk.Button(
            guidance_actions, text="Clear Guidance", command=self._clear_guidance
        )
        self._clear_guidance_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(guidance_actions, textvariable=self.guidance_status_var).pack(
            side=tk.LEFT, padx=(10, 0)
        )
        self._guidance_preview = tk.Text(
            self._guidance_frame, height=9, wrap=tk.WORD, state=tk.DISABLED
        )
        self._guidance_preview.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._guidance_help = ttk.Label(
            self._guidance_frame, text="", wraplength=980, justify=tk.LEFT
        )
        self._guidance_help.pack(fill=tk.X, pady=(6, 0))

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        state = widget.cget("state")
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        if value:
            widget.insert("1.0", value)
        widget.configure(state=state)

    # ------------------------------------------------------------------ state
    def _project_root(self) -> Optional[Path]:
        if self._project_root_provider is None:
            return None
        try:
            raw = self._project_root_provider()
        except Exception:
            return None
        if not raw:
            return None
        try:
            path = Path(raw).expanduser().resolve()
        except OSError:
            return None
        return path if path.is_dir() else None

    def _refresh_project(self) -> None:
        root = self._project_root()
        self.project_var.set(
            str(root) if root is not None else "No project selected — you can still ask a question."
        )

    def _active_host(self):
        """The host for the selected provider, or None when it cannot run."""
        if self._fixed_host is not None:
            return self._fixed_host
        provider = self._provider()
        if not provider.available:
            return None
        host = self._sessions.host_for(provider.id)
        self._host = host
        return host

    def _refresh_runtime(self) -> None:
        provider = self._provider()
        if provider.browser_mode == EXTERNAL:
            reason = f" {provider.embedding_note}" if provider.embedding_note else ""
            self.provider_status_var.set(provider.status_label)
            self.status_var.set(
                f"{provider.display_name} opens in your browser.{reason}"
            )
            self._set_browser_buttons(True, external=True)
            return
        if not provider.available:
            reason = (
                provider.qualification_note
                or f"the {provider.display_name} browser adapter is not qualified"
            )
            self.provider_status_var.set(provider.status_label)
            self.status_var.set(
                f"{provider.display_name} is not yet qualified — {reason}"
            )
            self._set_browser_buttons(False)
            return
        host = self._active_host()
        if host is None:
            self.status_var.set(f"Browser unavailable for {provider.display_name}")
            self._set_browser_buttons(False)
            return
        try:
            runtime = host.runtime_status()
        except Exception as exc:  # noqa: BLE001 - a probe must never break Core
            self.status_var.set(f"Browser unavailable: {type(exc).__name__}")
            self._set_browser_buttons(False)
            return
        if runtime.available:
            self.provider_status_var.set("Ready")
            self.status_var.set(
                f"Browser ready — sign in inside the {provider.display_name} window."
            )
            self._set_browser_buttons(True)
        else:
            self.status_var.set(f"Browser unavailable: {runtime.reason}")
            self._set_browser_buttons(False)

    def _set_browser_buttons(
        self, browser_available: bool, *, external: bool | None = None
    ) -> None:
        if external is None:
            external = self._provider().browser_mode == EXTERNAL
        state = tk.NORMAL if browser_available else tk.DISABLED
        for button in (
            getattr(self, "_open_btn", None),
            getattr(self, "_reload_btn", None),
            getattr(self, "_close_btn", None),
        ):
            if button is not None:
                button.configure(state=tk.DISABLED if external and button is not self._open_btn else state)

    def _schedule_poll(self) -> None:
        if self._closing:
            return
        try:
            self._poll_job = self.after(750, self._poll_browser)
        except tk.TclError:
            self._poll_job = None

    def _poll_browser(self) -> None:
        self._poll_job = None
        if self._closing:
            return
        provider = self._provider()
        if provider.browser_mode == EXTERNAL:
            # GisCode does not own the user's browser, so there is nothing to poll.
            self._schedule_poll()
            return
        host = self._active_host() if provider.available else None
        if host is not None:
            was_running = self._browser_was_running
            try:
                alive = bool(host.poll())
            except Exception:  # noqa: BLE001 - polling must never break Core
                alive = False
            self._browser_was_running = alive
            if was_running and not alive:
                self.status_var.set(host.status)
            if not alive and str(host.status).startswith("Browser unavailable"):
                self._set_browser_buttons(False)
            elif alive:
                self.status_var.set(f"{provider.display_name} browser open")
        self._schedule_poll()

    # --------------------------------------------------------------- context
    def _add_files(self) -> None:
        root = self._project_root()
        if root is None:
            messagebox.showinfo(
                "AI Web Chat Context",
                "Select or open a project first. Without a project you can still "
                "ask a question with no file context.",
                parent=self.winfo_toplevel(),
            )
            return
        paths = filedialog.askopenfilenames(
            parent=self.winfo_toplevel(),
            title="Select context files from the active project",
        )
        current = set(self._files_list.get(0, tk.END))
        for raw in paths:
            try:
                relative = Path(raw).resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                self.context_status_var.set("Skipped a file outside the active project")
                continue
            if relative not in current:
                self._files_list.insert(tk.END, relative)
                current.add(relative)
        self.context_status_var.set("Context files selected; prepare to review the prompt.")

    def _remove_files(self) -> None:
        selected = list(self._files_list.curselection())
        for index in reversed(selected):
            self._files_list.delete(index)
        if selected:
            self._context = None
            self._set_context_actions(False)

    def _clear_files(self) -> None:
        self._files_list.delete(0, tk.END)
        self._context = None
        self.context_status_var.set("No context prepared")
        self._set_context_actions(False)

    def _set_context_actions(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for button in (
            getattr(self, "_copy_btn", None),
            getattr(self, "_open_with_prompt_btn", None),
        ):
            if button is not None:
                button.configure(state=state)

    def _prepare_context(self) -> Optional[WebChatContext]:
        provider = self._provider()
        root = self._project_root()
        question = self._question_text.get("1.0", "end-1c")
        files = list(self._files_list.get(0, tk.END))
        adapter = self._registry.adapter(provider.id)
        try:
            if adapter is not None:
                context = adapter.build_context(root, files, question)
            else:
                context = build_web_chat_context(provider, root, files, question)
        except ContextBuildError as exc:
            self._context = None
            self.context_status_var.set(f"Context not ready: {exc}")
            self._set_context_actions(False)
            return None
        self._context = context
        self._set_text(self._prompt_preview, context.prompt)
        self.context_status_var.set(
            f"Prepared {len(context.files)} file(s) for {provider.display_name}; "
            f"redactions={context.redactions}; context={context.context_sha256[:12]}…"
        )
        self._set_context_actions(True)
        return context

    def _confirm_export(self, prompt: str) -> bool:
        name = self._provider().display_name
        files = len(self._context.files) if self._context else 0
        return bool(
            messagebox.askyesno(
                f"Share Reviewed Context with {name}",
                f"Copy the reviewed context to the clipboard and open the {name} "
                "browser?\n\n"
                f"Files: {files}\n"
                "GisCode will not send it automatically. You must paste it into "
                f"{name} yourself.",
                parent=self.winfo_toplevel(),
            )
        )

    def _copy_prompt(self) -> None:
        context = self._context or self._prepare_context()
        if context is None or not self._confirm_export(context.prompt):
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(context.prompt)
            self.context_status_var.set("Reviewed prompt copied to clipboard.")
        except tk.TclError as exc:
            self.context_status_var.set(f"Clipboard unavailable: {exc}")

    def _copy_prompt_and_open(self) -> None:
        context = self._context or self._prepare_context()
        if context is None or not self._confirm_export(context.prompt):
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(context.prompt)
        except tk.TclError as exc:
            self.context_status_var.set(f"Clipboard unavailable: {exc}")
            return
        if self._open_browser():
            self.context_status_var.set(
                "Prompt copied. Paste it into the provider, then copy the answer back here."
            )

    # --------------------------------------------------------------- browser
    def _open_browser(self) -> bool:
        self._refresh_project()
        provider = self._provider()
        if not provider.available:
            reason = (
                provider.qualification_note
                or f"the {provider.display_name} browser adapter is not qualified"
            )
            self.status_var.set(
                f"{provider.display_name} is not yet qualified — {reason}"
            )
            self._set_browser_buttons(False)
            return False
        host = self._active_host()
        if host is None:
            self.status_var.set(f"Browser unavailable for {provider.display_name}")
            self._set_browser_buttons(False)
            return False
        try:
            host.start()
        except BrowserUnavailableError as exc:
            self.status_var.set(f"Browser unavailable: {exc}")
            self._set_browser_buttons(False)
            return False
        except BrowserStartError as exc:
            self.status_var.set(f"Browser start failed: {exc}")
            self._set_browser_buttons(False)
            return False
        except Exception as exc:  # noqa: BLE001 - a browser failure must not crash Core
            self.status_var.set(f"Browser start failed: {type(exc).__name__}: {exc}")
            self._set_browser_buttons(False)
            return False
        self._browser_was_running = provider.browser_mode != EXTERNAL
        if provider.browser_mode == EXTERNAL:
            # The system browser is not ours; report what was opened, not a
            # window GisCode owns.
            self.status_var.set(host.status)
        else:
            self.status_var.set(
                f"{provider.display_name} browser open — sign in inside that window."
            )
        self._set_browser_buttons(True)
        return True

    def _reload_browser(self) -> None:
        provider = self._provider()
        host = self._active_host()
        if host is None:
            self.status_var.set(f"Browser unavailable for {provider.display_name}")
            return
        try:
            host.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser_was_running = False
        self._open_browser()

    def _close_browser(self) -> None:
        provider = self._provider()
        host = self._active_host()
        if host is not None:
            try:
                host.stop()
            except Exception:  # noqa: BLE001
                pass
        self._browser_was_running = False
        self.status_var.set(f"{provider.display_name} browser closed")

    def _forget_session(self) -> None:
        provider = self._provider()
        if provider.browser_mode == EXTERNAL:
            messagebox.showinfo(
                "AI Web Chat Browser",
                f"GisCode keeps no {provider.display_name} browser profile. "
                "Clear the session in your own browser if you want to sign out.",
                parent=self.winfo_toplevel(),
            )
            return
        host = self._active_host()
        if host is None:
            self.status_var.set(
                f"{provider.display_name} is not yet qualified; there is no browser "
                "session to forget"
            )
            return
        if getattr(host, "is_running", False):
            messagebox.showinfo(
                "AI Web Chat Browser",
                f"Close the {provider.display_name} browser before forgetting its session.",
                parent=self.winfo_toplevel(),
            )
            return
        if not messagebox.askyesno(
            f"Forget {provider.display_name} Session",
            f"Remove the isolated {provider.display_name} browser profile, including "
            "its sign-in session? This does not touch your normal browser or any "
            "other provider's profile.",
            parent=self.winfo_toplevel(),
        ):
            return
        try:
            host.clear_session()
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(str(exc))
            return
        self.status_var.set("Browser session cleared; sign in again when ready.")

    # -------------------------------------------------------------- guidance
    def _import_clipboard(self) -> None:
        context = self._context or self._prepare_context()
        if context is None:
            return
        provider = self._provider()
        try:
            raw = self.clipboard_get()
        except tk.TclError as exc:
            self.guidance_status_var.set(f"Clipboard unavailable: {exc}")
            return
        try:
            guidance = build_web_chat_guidance(raw, context, provider)
        except GuidanceError as exc:
            self.guidance_status_var.set(str(exc))
            return
        self._guidance = guidance
        self._set_text(self._guidance_preview, guidance.to_task_block())
        self._use_guidance_btn.configure(state=tk.NORMAL)
        self.guidance_status_var.set(
            f"Imported from {provider.display_name} as unverified; "
            f"context={guidance.context_sha256[:12]}…; redactions={guidance.redactions}"
        )

    def _use_guidance(self) -> None:
        if self._guidance is None or self._on_guidance is None:
            self.guidance_status_var.set("No guidance imported")
            return
        try:
            self._on_guidance(self._guidance.to_task_block())
        except Exception as exc:  # noqa: BLE001
            self.guidance_status_var.set(
                f"Could not insert guidance: {type(exc).__name__}"
            )
            return
        self.guidance_status_var.set(
            "Inserted into Chat as unverified guidance; review before sending."
        )

    def _clear_guidance(self) -> None:
        self._guidance = None
        self._set_text(self._guidance_preview, "")
        self._use_guidance_btn.configure(state=tk.DISABLED)
        self.guidance_status_var.set("No guidance imported")

    # -------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        self._closing = True
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        try:
            if self._fixed_host is not None:
                self._fixed_host.shutdown()
            else:
                self._sessions.shutdown()
        except Exception:
            pass
