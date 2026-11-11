"""Interactive acceptance scenarios executed inside the frozen application.

The ordinary Core build bundles this module but can never reach it: the entry
points are gated by ``sys._giscode_acceptance_build`` (set only by a PyInstaller
runtime hook baked into ``GisCodeAcceptanceHardened.exe``) *and* the explicit
``GISCODE_ACCEPTANCE_OPT_IN=1`` opt-in. A product launch returns before any
scenario variable is read.

Before a scenario touches anything it must prove its profile is disposable (see
:mod:`giscode.acceptance_safety`): possession of a random token marker, a
canonical path under the temporary root, no reparse points and no location
inside the user profile. The destructive phase additionally needs
``GISCODE_ACCEPTANCE_ALLOW_DESTRUCTIVE=1``, and the delete target is re-verified
inside the home immediately before the handler runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from giscode.acceptance_safety import (
    DisposableHomeError,
    verify_destructive_target,
    verify_disposable_home,
)

#: Selects the scenario. Unset means "not an interactive run".
SCENARIO_ENV = "GISCODE_ACCEPTANCE_SCENARIO"

INTERACTIVE = "interactive"
RESUME = "resume"
TASK2 = "task2"
SCENARIOS = (INTERACTIVE, RESUME, TASK2)

FIXTURE_DIR = "acceptance_project"
FIXTURE_SECRET = "sk-abcdefghijklmnopqrstuvwxyz"


def preflight(scenario: str, evidence_name) -> None:
    """Validate a scenario request without arming it or writing anything.

    Raises :class:`DisposableHomeError` when the run must be refused. The
    application entry point calls this before logging, the settings store, the
    knowledge database or the GUI exist, so a refusal leaves no trace at all in
    the profile. The scenario still re-validates when it is armed on the event
    loop; this is the early gate, not a replacement for that check.
    """

    name = (scenario or "").strip().lower()
    if name not in SCENARIOS:
        raise DisposableHomeError("unknown scenario")
    verify_disposable_home(require_destructive=name == INTERACTIVE)
    evidence_value = os.environ.get("GISCODE_ACCEPTANCE_EVIDENCE", "").strip()
    # Only the name is checked here: resolving the evidence root would create
    # the temporary directory, and a refusal should not write anywhere at all.
    if evidence_name(evidence_value) is None:
        raise DisposableHomeError("the evidence path is not valid")


def schedule_scenario(root, scenario: str, *, evidence_path, write_evidence) -> bool:
    """Validate the request and arm the scenario on the Tk event loop."""

    name = (scenario or "").strip().lower()
    if name not in SCENARIOS:
        _emit_summary(0, 0)
        evidence_value = os.environ.get("GISCODE_ACCEPTANCE_EVIDENCE", "").strip()
        if evidence_path(evidence_value) is not None:
            write_evidence(
                evidence_value,
                json.dumps(
                    {
                        "scenario": name,
                        "status": "FAILED",
                        "detail": "unknown scenario",
                        "checks": [],
                    }
                ),
            )
        _refuse("unknown scenario")

    try:
        home = verify_disposable_home(require_destructive=name == INTERACTIVE)
    except DisposableHomeError as exc:
        _refuse(exc.reason)
        return True  # unreachable; _refuse exits

    evidence_value = os.environ.get("GISCODE_ACCEPTANCE_EVIDENCE", "").strip()
    if evidence_path(evidence_value) is None:
        # Returning quietly here left the packaged application running with no
        # scenario armed: the process stayed alive until the driver timed out,
        # and no evidence explained why. A requested scenario that cannot write
        # its evidence is a refusal, exactly like an unsafe home.
        _refuse("the evidence path is not valid")
        return False  # unreachable; _refuse exits

    def start() -> None:
        results: list[dict] = []
        try:
            if name == INTERACTIVE:
                _run_interactive(root, results, home)
            elif name == RESUME:
                _run_resume(root, results, home)
            else:
                _run_task2(root, results, home)
        except Exception as exc:  # noqa: BLE001 - the evidence must record it
            results.append(
                {
                    "check": "scenario completed without an unhandled error",
                    "result": "FAIL",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            _print_result(results[-1])
        failed = [item for item in results if item["result"] != "PASS"]
        _emit_summary(len(results) - len(failed), len(failed))
        _finish(
            root,
            0 if not failed else 1,
            evidence_value,
            write_evidence,
            {
                "scenario": name,
                "status": "PASS" if not failed else "FAIL",
                **_identity(),
                "disposable_home": str(home),
                "project": str(Path(home) / FIXTURE_DIR),
                "checks": results,
            },
        )

    root.after(500, start)
    return True


def _refuse(reason: str) -> None:
    """Report the refusal and leave without writing or deleting anything."""

    print(f"SCENARIO_REFUSED: {reason}", flush=True)
    sys.stdout.flush()
    os._exit(2)


def _identity() -> dict:
    """Identity of the running executable, bound into every evidence file."""

    executable = Path(sys.executable)
    digest = hashlib.sha256()
    try:
        with executable.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        size = executable.stat().st_size
    except OSError:
        return {
            "executable": str(executable),
            "exe_sha256": "",
            "exe_size": 0,
            "frozen": bool(getattr(sys, "frozen", False)),
            "bundle_root": getattr(sys, "_MEIPASS", ""),
            "main_window_module": _module_path("giscode.gui.main_window"),
        }
    return {
        "executable": str(executable),
        "exe_sha256": digest.hexdigest(),
        "exe_size": size,
        "frozen": bool(getattr(sys, "frozen", False)),
        "bundle_root": getattr(sys, "_MEIPASS", ""),
        "main_window_module": _module_path("giscode.gui.main_window"),
    }


def _finish(root, code: int, evidence_value, write_evidence, payload: dict) -> None:
    """Write confined evidence, tear the window down and set the exit status."""

    write_evidence(evidence_value, json.dumps(payload, ensure_ascii=False, indent=2))
    window = getattr(root, "_giscode_main_window", None)
    view = getattr(window, "_ai_web_chat_view", None)
    if view is not None:
        try:
            view.shutdown()
        except Exception:
            pass
    try:
        root.destroy()
    except Exception:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def _module_path(name: str) -> str:
    try:
        module = __import__(name, fromlist=["__file__"])
        return str(getattr(module, "__file__", ""))
    except Exception:
        return ""


def _emit_summary(passed: int, failed: int) -> None:
    print(f"SUMMARY {passed} passed, {failed} failed", flush=True)
    print("SCENARIO_OK" if failed == 0 else "SCENARIO_FAIL", flush=True)


def _print_result(item: dict) -> None:
    suffix = f" :: {item['detail']}" if item["detail"] else ""
    print(f"{item['result']} {item['check']}{suffix}", flush=True)


def _check(results: list[dict], name: str, func) -> None:
    """Run one check; an exception is a failure with its reason, never a crash."""

    try:
        ok, detail = func()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    item = {
        "check": name,
        "result": "PASS" if ok else "FAIL",
        "detail": str(detail)[:400],
    }
    results.append(item)
    _print_result(item)


def _home() -> Path:
    return Path(os.environ.get("GISCODE_HOME") or (Path.home() / ".giscode"))


def _stored_provider() -> str:
    try:
        data = json.loads((_home() / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    section = data.get("ai_web_chat")
    if isinstance(section, dict):
        return str(section.get("provider") or "")
    return ""


def _ensure_fixture(home: Path) -> Path:
    """Create the project the scenario tests with; the driver supplies nothing."""

    root = Path(home) / FIXTURE_DIR
    if root.is_dir():
        return root
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "VALUE = 1\n"
        f"API_KEY = '{FIXTURE_SECRET}'\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("TOKEN=should-not-be-sent\n", encoding="utf-8")
    (root / "README.md").write_text("# Frozen acceptance project\n", encoding="utf-8")
    return root


def _fixture_ok(root: Path) -> bool:
    try:
        source = (root / "src" / "app.py").read_text(encoding="utf-8")
    except OSError:
        return False
    return FIXTURE_SECRET in source and (root / ".env").is_file()


class _FakeProcess:
    """Process double for the real browser host; no window and no network."""

    pid = 4242

    def __init__(self, counters: dict | None = None) -> None:
        self.alive = True
        self._counters = counters if counters is not None else {}

    def poll(self):
        return None if self.alive else 0

    def terminate(self) -> None:
        if self.alive:
            self._counters["terminated"] = self._counters.get("terminated", 0) + 1
        self.alive = False

    def kill(self) -> None:
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        return None


def _label_frame_texts(widget, found=None) -> list[str]:
    from tkinter import ttk

    found = [] if found is None else found
    for child in widget.winfo_children():
        try:
            if isinstance(child, ttk.LabelFrame):
                found.append(str(child.cget("text")))
        except Exception:
            pass
        _label_frame_texts(child, found)
    return found


def _quiet_dialogs() -> None:
    """Auto-confirm the dialogs the handlers would otherwise open."""

    from giscode.gui import ai_web_chat as webchat_gui

    webchat_gui.messagebox.askyesno = lambda *args, **kwargs: True
    webchat_gui.messagebox.showinfo = lambda *args, **kwargs: None


def _run_interactive(root, results: list[dict], home: Path) -> None:
    import tkinter as tk

    from giscode import edition
    from giscode.gui.main_window import ProjectContextStore
    from giscode.webchat.host import (
        BrowserHostError,
        BrowserRuntimeStatus,
        BrowserStartError,
        WebChatBrowserHost,
        default_profile_root,
    )
    from giscode.webchat.providers import CHATGPT_PROVIDER, PROVIDERS

    _quiet_dialogs()
    window = getattr(root, "_giscode_main_window", None)
    if window is None:
        _check(results, "the packaged window is available", lambda: (False, "no window"))
        return
    view = window._ai_web_chat_view
    project = _ensure_fixture(home)

    identity = _identity()
    bundle_root = os.path.normcase(str(identity["bundle_root"]))
    window_module = os.path.normcase(str(identity["main_window_module"]))
    _check(
        results,
        "frozen process runs the bundled MainWindow",
        lambda: (
            bool(identity["frozen"])
            and bool(bundle_root)
            and window_module.startswith(bundle_root)
            and len(identity["exe_sha256"]) == 64,
            f"{window_module} sha={identity['exe_sha256'][:12]}",
        ),
    )
    _check(
        results,
        "scenario created its own project fixture",
        lambda: (
            _fixture_ok(project) and Path(project).parent == home,
            str(project),
        ),
    )
    _check(
        results,
        "default provider is ChatGPT",
        lambda: (view._provider().id == "chatgpt", view._provider().id),
    )
    _check(
        results,
        "tab is AI Web Chat (no ChatGPT-only tab)",
        lambda: (
            "AI Web Chat" in window._tab_frames
            and "ChatGPT" not in window._tab_frames
            and tuple(window._tab_frames) == edition.tabs(),
            str(list(window._tab_frames)),
        ),
    )

    def switch_all():
        detail = []
        ok = True
        for provider in PROVIDERS:
            view._select_provider(provider.id)
            detail.append(f"{provider.id}={view._provider().id}")
            ok = ok and view._provider().id == provider.id
        return ok, "; ".join(detail)

    _check(results, "selector switches across all six providers", switch_all)

    def persisted():
        view._select_provider("claude")
        return _stored_provider() == "claude", f"ai_web_chat.provider={_stored_provider()!r}"

    _check(results, "provider choice is written to settings", persisted)

    def select_project():
        window._select_project_path(str(project))
        view._refresh_project()
        return str(project) in view.project_var.get(), view.project_var.get()

    _check(results, "project can be selected and shown", select_project)

    def project_survives_switch():
        view._select_provider("chatgpt")
        view._refresh_project()
        before = view.project_var.get()
        view._select_provider("gemini")
        view._refresh_project()
        view._files_list.delete(0, tk.END)
        view._files_list.insert(tk.END, "src/app.py")
        view._question_text.delete("1.0", tk.END)
        view._question_text.insert("1.0", "Review this file after switching provider.")
        context = view._prepare_context()
        prompt = context.prompt if context else ""
        return (
            view.project_var.get() == before
            and str(project) in before
            and "src/app.py" in prompt
            and str(project) not in prompt,
            f"{len(prompt)} chars",
        )

    _check(results, "switching provider keeps the project and re-prepares context", project_survives_switch)

    def chat_input_preserved():
        window.task_entry.delete("1.0", tk.END)
        window.task_entry.insert("1.0", "keep this draft")
        view._select_provider("grok")
        text = window.task_entry.get("1.0", tk.END).strip()
        return text == "keep this draft", text

    _check(results, "switching provider preserves the Chat draft", chat_input_preserved)

    def external_mode():
        view._select_provider("gemini")
        provider = view._provider()
        status = view.status_var.get()
        return (
            provider.browser_mode == "external"
            and "your browser" in status.lower()
            and "separate" in view.api_var.get().lower(),
            status[:160],
        )

    _check(results, "external provider uses the browser and reports API billing", external_mode)

    def external_host():
        host = view._sessions.host_for("gemini")
        return (
            host is not None
            and getattr(host, "profile_root", "missing") is None
            and not (home / "ai_web_chat" / "gemini").exists(),
            type(host).__name__,
        )

    _check(results, "external provider keeps no GisCode profile", external_host)

    def no_project():
        view._select_provider("chatgpt")
        window._active_project_context = None
        window._external_project_root = None
        view._refresh_project()
        view._files_list.delete(0, tk.END)
        view._question_text.delete("1.0", tk.END)
        view._question_text.insert("1.0", "Explain Python decorators without project files.")
        context = view._prepare_context()
        return (
            context is not None
            and context.project_name == "No project"
            and context.files == ()
            and "Explain Python decorators" in context.prompt,
            context.project_name if context else "none",
        )

    _check(results, "no-project question prepares without project context", no_project)

    def copy_prompt():
        root.clipboard_clear()
        view._copy_prompt()
        copied = root.clipboard_get()
        return (
            "Explain Python decorators" in copied and "No project context" in copied,
            f"{len(copied)} chars",
        )

    _check(results, "Copy Prompt puts the reviewed prompt on the clipboard", copy_prompt)

    state: dict[str, object] = {}

    def project_context():
        window._select_project_path(str(project))
        view._refresh_project()
        view._files_list.delete(0, tk.END)
        view._files_list.insert(tk.END, "src/app.py")
        view._question_text.delete("1.0", tk.END)
        view._question_text.insert("1.0", "Review this file.")
        context = view._prepare_context()
        state["context"] = context
        prompt = context.prompt if context else ""
        return "src/app.py" in prompt and str(project) not in prompt, f"{len(prompt)} chars"

    _check(results, "selected project file is included without absolute path", project_context)

    def redaction():
        context = state.get("context")
        prompt = getattr(context, "prompt", "") or ""
        return (
            _fixture_ok(project)
            and FIXTURE_SECRET not in prompt
            and "[REDACTED]" in prompt,
            f"redactions={getattr(context, 'redactions', 'n/a')}",
        )

    _check(results, "secret in the selected file is redacted", redaction)

    def credential_refused():
        view._files_list.insert(tk.END, ".env")
        refused = view._prepare_context() is None
        reason = view.context_status_var.get()
        view._files_list.delete(0, tk.END)
        view._files_list.insert(tk.END, "src/app.py")
        view._prepare_context()
        return refused and "credential" in reason.lower(), reason[:160]

    _check(results, "credential file is refused as context", credential_refused)

    def imported():
        window.task_entry.delete("1.0", tk.END)
        root.clipboard_clear()
        root.clipboard_append("Prefer a pure function and add a regression test.")
        view._import_clipboard()
        view._use_guidance()
        inserted = window.task_entry.get("1.0", tk.END)
        state["inserted"] = inserted
        return (
            "[UNVERIFIED CHATGPT GUIDANCE]" in inserted
            and "Provider: chatgpt" in inserted
            and "not executed or verified" in inserted,
            "label + provider + status",
        )

    _check(results, "imported answer is labelled unverified and attributed", imported)
    _check(
        results,
        "guidance is only inserted, never sent",
        lambda: (
            "Prefer a pure function" in str(state.get("inserted", ""))
            and not window._active_task,
            "task not started",
        ),
    )

    # Real host, injected process factory: lifecycle code and profile handling
    # are the production ones; only the WebView2 process is doubled.
    calls = {"factory": 0, "terminated": 0}
    probe_root = home / "browser_probe"

    def factory(url, profile_root):
        calls["factory"] += 1
        return _FakeProcess(calls)

    host = WebChatBrowserHost(
        CHATGPT_PROVIDER,
        profile_root=probe_root,
        probe=lambda: BrowserRuntimeStatus(True),
        process_factory=factory,
    )
    view._select_provider("chatgpt")
    view._fixed_host = host

    def host_start():
        opened = view._open_browser()
        duplicate = False
        try:
            host.start()
        except BrowserStartError:
            duplicate = True
        return (
            opened
            and probe_root.is_dir()
            and calls["factory"] == 1
            and host.is_running
            and duplicate,
            f"profile_created={probe_root.is_dir()} starts={calls['factory']} duplicate_refused={duplicate}",
        )

    _check(results, "real host starts once and refuses a duplicate start", host_start)

    def reload_browser():
        view._reload_browser()
        return (
            calls["factory"] == 2
            and calls["terminated"] == 1
            and host.is_running
            and "open" in host.status.lower(),
            f"starts={calls['factory']} stops={calls['terminated']} status={host.status}",
        )

    _check(results, "Reload restarts the real host", reload_browser)

    def clear_while_running():
        refused = False
        try:
            host.clear_session()
        except BrowserHostError:
            refused = True
        return refused and probe_root.is_dir(), "profile preserved while running"

    _check(results, "clear_session refuses while the browser is running", clear_while_running)

    def close_browser():
        view._close_browser()
        return (
            calls["terminated"] == 2
            and not host.is_running
            and host.status == "ChatGPT browser closed",
            f"stops={calls['terminated']} status={host.status}",
        )

    _check(results, "Close stops the real host", close_browser)
    view._fixed_host = None

    def forget():
        profile = default_profile_root("chatgpt")
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "cookie-store").write_text("isolated", encoding="utf-8")
        verify_destructive_target(profile, home)
        view._forget_session()
        return (
            not profile.exists()
            and home.is_dir()
            and probe_root.is_dir(),
            str(profile),
        )

    _check(results, "Forget Session deletes only the contained profile", forget)
    _check(
        results,
        "each provider has its own profile path",
        lambda: (
            len({default_profile_root(p.id) for p in PROVIDERS}) == len(PROVIDERS),
            f"{len({default_profile_root(p.id) for p in PROVIDERS})} distinct paths",
        ),
    )

    def settings_sections():
        frame = window._tab_frames["Settings"]
        labels = _label_frame_texts(frame)
        window._refresh_chatgpt_status()
        accounts = list(getattr(window, "_chatgpt_accounts", []))
        account_text = window._chatgpt_account_var.get()
        paid = bool(window._openai_paid_var.get())
        active = bool(window._chatgpt_active_var.get())
        state["accounts"] = accounts
        state["account_text"] = account_text
        return (
            any("ChatGPT plan" in text for text in labels)
            and any("OpenAI" in text for text in labels)
            and accounts == []
            and "No ChatGPT account connected" in account_text
            and paid is False
            and active is False,
            f"accounts={len(accounts)} paid={paid} active={active}",
        )

    _check(results, "Settings separates plan from billable API and stays off", settings_sections)

    def store_has_project():
        stored = ProjectContextStore().load_all()
        matched = any(
            os.path.normcase(os.path.abspath(item.root_path))
            == os.path.normcase(str(Path(project).resolve()))
            for item in stored.values()
        )
        return matched, f"{len(stored)} stored project(s)"

    _check(results, "project is persisted for reopening after restart", store_has_project)

    def leave_marker():
        view._select_provider("claude")
        return _stored_provider() == "claude", f"ai_web_chat.provider={_stored_provider()!r}"

    _check(results, "provider is left as claude for the restart phase", leave_marker)


def _run_resume(root, results: list[dict], home: Path) -> None:
    import tkinter as tk

    from giscode.gui.main_window import ProjectContextStore
    from giscode.webchat.host import default_profile_root
    from giscode.webchat.providers import PROVIDERS

    _quiet_dialogs()
    window = getattr(root, "_giscode_main_window", None)
    view = window._ai_web_chat_view
    project = Path(home) / FIXTURE_DIR

    _check(
        results,
        "resume phase runs the frozen bundle",
        lambda: (
            bool(_identity()["frozen"]) and _identity()["exe_size"] > 0,
            _identity()["exe_sha256"][:12],
        ),
    )
    _check(
        results,
        "a second frozen process reads the stored provider",
        lambda: (view._provider().id == "claude", view._provider().id),
    )
    _check(
        results,
        "scenario fixture survived the restart",
        lambda: (_fixture_ok(project), str(project)),
    )

    def reopen():
        window._select_project_path(str(project))
        view._refresh_project()
        view._files_list.delete(0, tk.END)
        view._files_list.insert(tk.END, "src/app.py")
        view._question_text.delete("1.0", tk.END)
        view._question_text.insert("1.0", "Continue where the last session stopped.")
        context = view._prepare_context()
        prompt = context.prompt if context else ""
        return (
            context is not None
            and "src/app.py" in prompt
            and str(project) not in prompt
            and "[REDACTED]" in prompt,
            f"{len(prompt)} chars",
        )

    _check(results, "project reopens with redacted context after restart", reopen)

    def stored():
        items = ProjectContextStore().load_all()
        matched = any(
            os.path.normcase(os.path.abspath(item.root_path))
            == os.path.normcase(str(project.resolve()))
            for item in items.values()
        )
        return matched, f"{len(items)} stored project(s)"

    _check(results, "stored project entry is still present", stored)

    def accounts():
        window._refresh_chatgpt_status()
        return (
            list(getattr(window, "_chatgpt_accounts", [])) == []
            and "No ChatGPT account connected" in window._chatgpt_account_var.get()
            and not bool(window._openai_paid_var.get()),
            window._chatgpt_account_var.get(),
        )

    _check(results, "settings still report no connected account", accounts)

    def profile_isolation():
        paths = {default_profile_root(provider.id) for provider in PROVIDERS}
        return len(paths) == len(PROVIDERS), f"{len(paths)} distinct paths"

    _check(results, "provider profiles stay isolated after restart", profile_isolation)


# --------------------------------------------------------------------- task 2
def _shots_dir() -> Path:
    import tempfile

    path = Path(tempfile.gettempdir()) / "giscode_acceptance" / "task2-shots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _enum_windows() -> list[tuple[int, str, int]]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((int(hwnd), buffer.value, int(pid.value)))
        return True

    user32.EnumWindows(callback, 0)
    return found


def _main_window_handle(root=None) -> int | None:
    if root is not None:
        try:
            import ctypes

            child = int(root.winfo_id())
            parent = ctypes.windll.user32.GetParent(child)
            return int(parent or child)
        except Exception:
            pass
    own = os.getpid()
    for hwnd, text, pid in _enum_windows():
        if pid == own and text.startswith("GisCode"):
            return hwnd
    return None


def _wait_main_window(root, timeout: float = 20.0) -> int | None:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hwnd = _main_window_handle(root)
        if hwnd:
            return hwnd
        time.sleep(0.5)
    return None


def _wait_window(title: str, timeout: float = 60.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for hwnd, text, pid in _enum_windows():
            if text == title:
                return hwnd, pid
        time.sleep(0.5)
    return None


def _process_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def _pid_image(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.normcase(os.path.abspath(buffer.value))
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _image_pids(image: str) -> list[int]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    array = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(array, ctypes.sizeof(array), ctypes.byref(needed)):
        return []
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    target = os.path.normcase(os.path.abspath(image))
    out: list[int] = []
    for index in range(count):
        pid = int(array[index])
        if pid == 0:
            continue
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            continue
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                if os.path.normcase(os.path.abspath(buffer.value)) == target:
                    out.append(pid)
        finally:
            kernel32.CloseHandle(handle)
    return out


def _capture_window(hwnd: int, path: Path) -> tuple[int, int, float]:
    import ctypes
    from ctypes import wintypes

    from PIL import Image, ImageStat

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise RuntimeError("window has no visible area")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(memory_dc, bitmap)
    try:
        user32.PrintWindow(hwnd, memory_dc, 2)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0)
        image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        deviation = ImageStat.Stat(image.convert("L")).stddev[0]
        return width, height, float(deviation)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def _capture_check(results: list[dict], name: str, root, filename: str) -> None:
    """Capture the *main* window and count non-blank pixels as visible content.

    The launch splash is a separate window and can be brighter than the main
    window, so the capture is pinned to the root wrapper window and retried
    until the launch sequence has painted it.
    """

    def run():
        import time

        try:
            root.update()
        except Exception:
            pass
        path = _shots_dir() / filename
        deadline = time.monotonic() + 40
        best = (0, 0, 0.0)
        while time.monotonic() < deadline:
            # Pump Tk so the launch sequence actually paints while we wait.
            try:
                root.update()
            except Exception:
                pass
            hwnd = _main_window_handle(root)
            if hwnd:
                temp = path.parent / f"{path.stem}.tmp{path.suffix}"
                width, height, deviation = _capture_window(hwnd, temp)
                if deviation > best[2]:
                    best = (width, height, deviation)
                    try:
                        os.replace(temp, path)
                    except OSError:
                        pass
                else:
                    try:
                        temp.unlink()
                    except OSError:
                        pass
                # The launch splash is larger than the main window; refuse
                # it so a splash frame can never be recorded as app evidence.
                if (
                    deviation > 5.0
                    and 1000 < width <= 1700
                    and 700 < height <= 1150
                ):
                    break
            time.sleep(0.7)
        width, height, deviation = best
        return (
            deviation > 5.0 and 1000 < width <= 1700 and 700 < height <= 1150,
            f"{filename} {width}x{height} stddev={deviation:.1f}",
        )

    _check(results, name, run)


def _run_task2(root, results: list[dict], home: Path) -> None:
    """Real workflow plus real WebView2 lifecycle (no process double)."""

    import tkinter as tk
    import time

    from giscode.webchat.host import (
        WebChatBrowserHost,
        default_profile_root,
        probe_webview2_runtime,
    )
    from giscode.webchat.providers import CHATGPT_PROVIDER, PROVIDERS

    _quiet_dialogs()
    window = getattr(root, "_giscode_main_window", None)
    view = window._ai_web_chat_view
    project = _ensure_fixture(home)
    state: dict = {}

    identity = _identity()
    _check(
        results,
        "task2 runs inside the frozen bundle",
        lambda: (
            bool(identity["frozen"]) and len(identity["exe_sha256"]) == 64,
            f"sha={identity['exe_sha256'][:12]}",
        ),
    )

    runtime = probe_webview2_runtime()
    _check(
        results,
        "real WebView2 runtime is available",
        lambda: (
            runtime.available,
            f"engine={runtime.engine} reason={runtime.reason or 'none'}",
        ),
    )
    _check(
        results,
        "provider URL stays on the ChatGPT origin",
        lambda: (
            CHATGPT_PROVIDER.url.startswith("https://chatgpt.com"),
            CHATGPT_PROVIDER.url,
        ),
    )

    # ------------------------------------------------------------- workflow
    view._select_provider("chatgpt")
    window._active_project_context = None
    window._external_project_root = None
    view._refresh_project()
    view._files_list.delete(0, tk.END)

    def no_project_prompt():
        view._question_text.delete("1.0", tk.END)
        view._question_text.insert("1.0", "Explain decorators without project files.")
        context = view._prepare_context()
        state["plain"] = context.prompt if context else ""
        return (
            context is not None
            and "Explain decorators without project files." in context.prompt,
            f"{len(context.prompt) if context else 0} chars",
        )

    _check(results, "workflow prepares a prompt with no project", no_project_prompt)
    _capture_check(
        results,
        "no-project prompt is visible in the window",
        root,
        "workflow-01-no-project.png",
    )

    def project_prompt():
        window._select_project_path(str(project))
        view._refresh_project()
        view._files_list.delete(0, tk.END)
        view._files_list.insert(tk.END, "src/app.py")
        view._question_text.delete("1.0", tk.END)
        view._question_text.insert("1.0", "Review this file for secrets.")
        context = view._prepare_context()
        prompt = context.prompt if context else ""
        state["project_prompt"] = prompt
        return (
            context is not None
            and "[REDACTED]" in prompt
            and FIXTURE_SECRET not in prompt
            and str(project) not in prompt
            and "src/app.py" in prompt,
            f"redactions={context.redactions if context else 0}",
        )

    _check(results, "workflow prepares project context with redaction", project_prompt)
    _capture_check(
        results,
        "project prompt is visible in the window",
        root,
        "workflow-02-project.png",
    )

    def copy_prompt():
        view._copy_prompt()
        view.update()
        copied = view.clipboard_get()
        return (
            copied == state.get("project_prompt")
            and "copied" in view.context_status_var.get().lower(),
            view.context_status_var.get(),
        )

    _check(results, "workflow copies the reviewed prompt to the clipboard", copy_prompt)

    def import_answer():
        view.clipboard_clear()
        view.clipboard_append(
            "Prefer a pure function instead of a class here."
        )
        view.update()
        view._import_clipboard()
        view._use_guidance()
        inserted = window.task_entry.get("1.0", tk.END)
        state["inserted"] = inserted
        try:
            window._notebook.select(window._tab_frames["Chat"])
        except Exception:
            pass
        return (
            "[UNVERIFIED CHATGPT GUIDANCE]" in inserted
            and "Prefer a pure function" in inserted
            and not window._active_task,
            "labelled unverified, not executed",
        )

    _check(results, "workflow imports an answer as unverified, never executed", import_answer)
    _capture_check(
        results,
        "imported answer is visible in Chat",
        root,
        "workflow-03-imported.png",
    )

    def switch_provider():
        before = view.project_var.get()
        view._select_provider("claude")
        view._refresh_project()
        context = view._prepare_context()
        prompt = context.prompt if context else ""
        return (
            view.project_var.get() == before
            and "[REDACTED]" in prompt
            and str(project) not in prompt,
            f"project kept, {len(prompt)} chars",
        )

    _check(results, "workflow keeps project state when switching provider", switch_provider)

    # ------------------------------------------------------- real browser
    profile = default_profile_root("chatgpt")
    host = WebChatBrowserHost(CHATGPT_PROVIDER, profile_root=profile)
    view._select_provider("chatgpt")
    view._fixed_host = host
    state["pids"] = []

    def start_browser():
        started = view._open_browser()
        found = _wait_window(CHATGPT_PROVIDER.window_title)
        if not found:
            return False, f"started={started} window absent status={view.status_var.get()}"
        hwnd, pid = found
        state["browser_hwnd"] = hwnd
        state["pids"].append(pid)
        return (
            started
            and pid != os.getpid()
            and _process_running(pid)
            and profile.is_dir()
            and any(profile.iterdir()),
            f"pid={pid} profile_entries={len(list(profile.iterdir()))}",
        )

    _check(results, "real browser process and window are created", start_browser)

    def renders():
        hwnd = state.get("browser_hwnd")
        deadline = time.monotonic() + 45
        best = (0, 0.0)
        path = _shots_dir() / "browser-01-page.png"
        while time.monotonic() < deadline:
            width, height, deviation = _capture_window(hwnd, path)
            best = (width, deviation)
            if deviation > 5.0:
                break
            time.sleep(2.0)
        return best[1] > 5.0, f"browser-01-page.png {best[0]}px stddev={best[1]:.1f}"

    _check(results, "real browser page renders (off-screen capture)", renders)

    def isolation():
        others = [
            default_profile_root(p.id)
            for p in PROVIDERS
            if p.id != "chatgpt"
        ]
        return (
            all(not p.exists() for p in others)
            and str(profile).startswith(str(home)),
            f"chatgpt profile inside home; {len(others)} other profiles absent",
        )

    _check(results, "real browser uses only the isolated ChatGPT profile", isolation)

    def reload_browser():
        old_pid = state["pids"][-1]
        view._reload_browser()
        found = _wait_window(CHATGPT_PROVIDER.window_title)
        gone = not _process_running(old_pid)
        if not found:
            return False, f"old_gone={gone} new window absent"
        hwnd, pid = found
        state["browser_hwnd"] = hwnd
        state["pids"].append(pid)
        return (
            gone and pid != old_pid and _process_running(pid),
            f"old={old_pid} gone={gone} new={pid}",
        )

    _check(results, "reload replaces the browser process", reload_browser)

    def reloaded_page():
        hwnd = state.get("browser_hwnd")
        deadline = time.monotonic() + 45
        best = (0, 0.0)
        path = _shots_dir() / "browser-02-reloaded.png"
        while time.monotonic() < deadline:
            width, height, deviation = _capture_window(hwnd, path)
            best = (width, deviation)
            if deviation > 5.0:
                break
            time.sleep(2.0)
        return best[1] > 5.0, f"browser-02-reloaded.png {best[0]}px stddev={best[1]:.1f}"

    _check(results, "reloaded browser page is visible", reloaded_page)

    def close_browser():
        pid = state["pids"][-1]
        view._close_browser()
        deadline = time.monotonic() + 25
        gone = False
        while time.monotonic() < deadline:
            if not _process_running(pid) and _wait_window(CHATGPT_PROVIDER.window_title, 0.1) is None:
                gone = True
                break
            time.sleep(0.5)
        return gone, f"pid={pid} exited={gone} status={view.status_var.get()}"

    _check(results, "Close exits the real browser process and window", close_browser)

    def repeated_cycles():
        details = []
        for _ in range(2):
            host.start()
            found = _wait_window(CHATGPT_PROVIDER.window_title, 40)
            if not found:
                return False, "window absent in cycle"
            pid = found[1]
            host.stop()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and _process_running(pid):
                time.sleep(0.4)
            details.append(f"pid={pid} exited={not _process_running(pid)}")
            if _process_running(pid):
                return False, "; ".join(details)
        return True, "; ".join(details)

    _check(results, "repeated start/stop leaves nothing behind", repeated_cycles)

    def recovery():
        host.stop()
        host.stop()
        host.start()
        found = _wait_window(CHATGPT_PROVIDER.window_title, 40)
        pid = found[1] if found else 0
        host.stop()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and pid and _process_running(pid):
            time.sleep(0.4)
        return (
            bool(found) and (not pid or not _process_running(pid)),
            f"restarted pid={pid}, stopped cleanly",
        )

    _check(results, "browser recovers after a stop when already closed", recovery)

    def no_orphans():
        browser_pids = list(state.get("pids", []))
        own_image = os.path.normcase(os.path.abspath(sys.executable))
        alive = [
            pid
            for pid in browser_pids
            if _process_running(pid) and _pid_image(pid) == own_image
        ]
        titles = {p.window_title for p in PROVIDERS}
        windows_left = [
            text for _hwnd, text, _pid in _enum_windows() if text in titles
        ]
        helpers = [
            pid
            for pid in _image_pids(sys.executable)
            if pid != os.getpid() and pid not in browser_pids
        ]
        return (
            not alive and not windows_left,
            f"browser_alive={alive} browser_windows={windows_left} "
            f"internal_helpers={helpers}",
        )

    _check(results, "no orphan browser process or window remains", no_orphans)

    def final_close():
        view._close_browser()
        view._fixed_host = None
        return (
            "closed" in view.status_var.get().lower()
            and _wait_window(CHATGPT_PROVIDER.window_title, 0.1) is None,
            view.status_var.get(),
        )

    _check(results, "workflow closes the browser and cleans up", final_close)
    _capture_check(
        results,
        "final window state is visible",
        root,
        "workflow-04-closed.png",
    )
