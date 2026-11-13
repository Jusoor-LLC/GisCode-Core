"""The first-run window.

One window, six sections, no new dependency: Tk only. The sections mirror the
service stages exactly, and the window is a *view* - it never invents a verdict.
Every green line comes from a recorded result in :class:`OnboardingService`.

Threading rule: detection, a pull, an inference test and the first task all run
off the Tk thread and report back with ``after``. A frozen window during a model
download would look like a hang, and the user would kill it mid-transfer.
"""
from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from tkinter import ttk

from giscode.onboarding.service import OnboardingService


class OnboardingWizard:
    def __init__(self, root, *, settings=None, model_manager=None) -> None:
        self.root = root
        self.model_manager = model_manager
        self.service = OnboardingService(settings=settings)
        self.window = tk.Toplevel(root)
        self.window.title("Set up GisCode")
        self.window.geometry("980x760")
        self.window.minsize(860, 640)
        try:
            self.window.transient(root)
        except Exception:
            pass
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.status_var = tk.StringVar(value=self.service.recovery_hint())
        self.hardware_var = tk.StringVar(value="Hardware has not been measured yet.")
        self.runtime_var = tk.StringVar(value="Press Check Ollama to look for it.")
        self.download_var = tk.StringVar(value="No download has started.")
        self.test_var = tk.StringVar(value="Test My AI has not run yet.")
        self.task_var = tk.StringVar(value="The first coding task has not run yet.")
        self.install_consent = tk.BooleanVar(value=False)
        self._poll_job = None

        self._build()
        self._refresh()

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            outer,
            text="Welcome to GisCode",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            outer,
            text=(
                "GisCode runs models on this computer through Ollama. This setup finds "
                "Ollama, measures your hardware, recommends a model that fits, and proves "
                "everything works with a small real coding task. Nothing is downloaded "
                "until you confirm it."
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._section(outer, "1. Ollama runtime")
        row = ttk.Frame(outer)
        row.pack(anchor=tk.W, pady=(2, 0))
        ttk.Button(row, text="Check Ollama", command=self._check_runtime).pack(side=tk.LEFT)
        ttk.Button(row, text="Start instructions", command=self._show_start_hint).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Checkbutton(
            row,
            text="I will install Ollama myself from the official page",
            variable=self.install_consent,
            command=self._sync_install_button,
        ).pack(side=tk.LEFT, padx=(16, 0))
        self.install_btn = ttk.Button(
            row, text="Open official Ollama page", command=self._open_installer, state=tk.DISABLED
        )
        self.install_btn.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(outer, textvariable=self.runtime_var, wraplength=900, foreground="gray").pack(
            anchor=tk.W, pady=(4, 8)
        )

        self._section(outer, "2. This computer")
        ttk.Button(outer, text="Measure hardware", command=self._measure).pack(anchor=tk.W)
        ttk.Label(outer, textvariable=self.hardware_var, wraplength=900, foreground="gray").pack(
            anchor=tk.W, pady=(4, 8)
        )

        self._section(outer, "3. Recommended model")
        columns = ("model", "size", "disk", "fit", "installed")
        self.model_tree = ttk.Treeview(outer, columns=columns, show="headings", height=5)
        for name, width, label in (
            ("model", 210, "Model"),
            ("size", 90, "Download"),
            ("disk", 110, "Needs free"),
            ("fit", 210, "Fit"),
            ("installed", 90, "Installed"),
        ):
            self.model_tree.heading(name, text=label)
            self.model_tree.column(name, width=width, anchor=tk.W)
        self.model_tree.pack(fill=tk.X, pady=(4, 4))
        ttk.Button(outer, text="Recommend for this machine", command=self._recommend).pack(
            anchor=tk.W
        )

        self._section(outer, "4. Download")
        row = ttk.Frame(outer)
        row.pack(anchor=tk.W)
        ttk.Button(row, text="Confirm and download selected model", command=self._download).pack(
            side=tk.LEFT
        )
        self.cancel_btn = ttk.Button(row, text="Cancel", command=self._cancel_download, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row, text="Retry", command=self._retry_download).pack(side=tk.LEFT, padx=(6, 0))
        self.progress = ttk.Progressbar(outer, orient=tk.HORIZONTAL, length=860, maximum=100)
        self.progress.pack(anchor=tk.W, pady=(6, 2))
        ttk.Label(outer, textvariable=self.download_var, wraplength=900, foreground="gray").pack(
            anchor=tk.W, pady=(0, 8)
        )

        self._section(outer, "5. Test My AI")
        ttk.Button(outer, text="Run Test My AI", command=self._test_model).pack(anchor=tk.W)
        ttk.Label(outer, textvariable=self.test_var, wraplength=900, foreground="gray").pack(
            anchor=tk.W, pady=(4, 8)
        )

        self._section(outer, "6. First coding task")
        ttk.Button(outer, text="Run the first coding task", command=self._first_task).pack(
            anchor=tk.W
        )
        ttk.Label(outer, textvariable=self.task_var, wraplength=900, foreground="gray").pack(
            anchor=tk.W, pady=(4, 4)
        )
        self.diff_box = tk.Text(outer, height=6, wrap="none")
        self.diff_box.pack(fill=tk.BOTH, expand=True, pady=(4, 8))

        footer = ttk.Frame(outer)
        footer.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(footer, textvariable=self.status_var, wraplength=700, foreground="gray").pack(
            side=tk.LEFT, anchor=tk.W
        )
        ttk.Button(footer, text="Finish", command=self.close).pack(side=tk.RIGHT)

    @staticmethod
    def _section(parent, text: str) -> None:
        ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=(10, 6))
        ttk.Label(parent, text=text, font=("Segoe UI", 11, "bold")).pack(anchor=tk.W)

    # -- threading helpers --------------------------------------------------
    def _run(self, work, done) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # surfaced in the UI, never swallowed
                result = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                self.window.after(0, lambda: done(result))
            except Exception:
                pass

        threading.Thread(target=runner, daemon=True, name="giscode-onboarding").start()

    def _refresh(self) -> None:
        self.status_var.set(self.service.recovery_hint())
        try:
            self.window.update_idletasks()
        except Exception:
            pass

    def _sync_install_button(self) -> None:
        self.install_btn.configure(state=tk.NORMAL if self.install_consent.get() else tk.DISABLED)

    # -- actions ------------------------------------------------------------
    def _check_runtime(self) -> None:
        self.runtime_var.set("Asking Ollama whether it is running...")
        self._run(self.service.detect_runtime, self._after_runtime)

    def _after_runtime(self, report: dict) -> None:
        if report.get("error"):
            self.runtime_var.set(f"Ollama could not be checked: {report['error']}")
        elif report.get("ready"):
            models = report.get("models") or []
            self.runtime_var.set(
                f"Ollama {report.get('version') or ''} is running at {report.get('endpoint')}; "
                f"{len(models)} model(s) installed."
            )
        elif report.get("installed"):
            self.runtime_var.set(
                "Ollama is installed but not answering. " + (report.get("action") or "")
            )
        else:
            self.runtime_var.set("Ollama is not installed on this machine.")
        self._refresh()

    def _show_start_hint(self) -> None:
        from giscode.onboarding.runtime import service_start_hint

        self.runtime_var.set(service_start_hint() + " Nothing has been installed.")

    def _open_installer(self) -> None:
        if not self.install_consent.get():
            self.runtime_var.set(
                "Tick the box first: GisCode will open the page, and you run the installer."
            )
            return
        info = self.service.confirm_install()
        self.runtime_var.set(
            "Opening the official Ollama page. " + " ".join(info["steps"][:2])
        )
        try:
            webbrowser.open(info["url"])
        except Exception as exc:
            self.runtime_var.set(f"Could not open a browser ({exc}). The page is {info['url']}")

    def _measure(self) -> None:
        self.hardware_var.set("Measuring RAM, GPU and disk...")
        self._run(self.service.assess_hardware, self._after_hardware)

    def _after_hardware(self, hardware: dict) -> None:
        if hardware.get("error"):
            self.hardware_var.set(f"Hardware could not be measured: {hardware['error']}")
            return
        summary = hardware.get("summary") or ""
        notes = " ".join(hardware.get("notes") or [])
        self.hardware_var.set(f"{summary}. {notes}".strip())
        self._refresh()

    def _recommend(self) -> None:
        self._run(self.service.plan, self._after_plan)

    def _after_plan(self, plan: dict) -> None:
        if plan.get("error"):
            self.download_var.set(f"Recommendations failed: {plan['error']}")
            return
        for item in list(self.model_tree.get_children()):
            self.model_tree.delete(item)
        for entry in plan.get("recommended") or []:
            self.model_tree.insert(
                "",
                tk.END,
                values=(
                    entry["model"],
                    f"{entry['size_gb']:.1f} GB",
                    f"{entry['required_disk_gb']:.1f} GB",
                    entry["headline"] or entry["level"],
                    "yes" if entry["installed"] else "no",
                ),
            )
        children = self.model_tree.get_children()
        if children:
            self.model_tree.selection_set(children[0])
            self.status_var.set("Recommended model selected. Confirm the download to continue.")
        else:
            self.status_var.set(
                plan.get("insufficient_explanation") or "No model fits this machine as measured."
            )

    def _selected_model(self) -> tuple[str, int]:
        selection = self.model_tree.selection()
        if not selection:
            return "", 0
        values = self.model_tree.item(selection[0], "values")
        model = str(values[0]) if values else ""
        size_text = str(values[1]).replace("GB", "").strip() if len(values) > 1 else "0"
        try:
            size = int(float(size_text) * (1024 ** 3))
        except ValueError:
            size = 0
        return model, size

    def _download(self) -> None:
        model, size = self._selected_model()
        if not model:
            self.download_var.set("Select a model in the list first.")
            return
        try:
            self.service.select_model(model, size_bytes=size)
            self.service.confirm_download(model)
            self.service.start_download(model)
        except (PermissionError, ValueError, RuntimeError) as exc:
            self.download_var.set(str(exc))
            return
        self.cancel_btn.configure(state=tk.NORMAL)
        self.download_var.set(f"Downloading {model} with Ollama...")
        self._poll_download()

    def _poll_download(self) -> None:
        job = self.service.download_status()
        status = str(job.get("status") or "")
        self.progress["value"] = int(job.get("percent") or 0)
        self.download_var.set(
            f"{status}: {job.get('percent', 0)}% - {job.get('detail') or 'working'}"
        )
        if status in ("running", "pending"):
            self._poll_job = self.window.after(500, self._poll_download)
        else:
            self.cancel_btn.configure(state=tk.DISABLED)
            if status == "done":
                self.status_var.set("Download finished. Run Test My AI next.")
            elif status in ("failed", "interrupted", "cancelled"):
                self.status_var.set(self.service.recovery_hint())

    def _cancel_download(self) -> None:
        job = self.service.cancel_download()
        self.download_var.set(f"Cancelled ({job.get('percent', 0)}% reached).")
        self.cancel_btn.configure(state=tk.DISABLED)

    def _retry_download(self) -> None:
        try:
            self.service.retry_download()
        except (PermissionError, ValueError, RuntimeError) as exc:
            self.download_var.set(str(exc))
            return
        self.cancel_btn.configure(state=tk.NORMAL)
        self._poll_download()

    def _test_model(self) -> None:
        self.test_var.set("Sending one real request to the model...")
        self._run(self.service.validate_model, self._after_test)

    def _after_test(self, result: dict) -> None:
        if result.get("error"):
            self.test_var.set(f"Test could not run: {result['error']}")
            return
        mark = "PASS" if result.get("ok") else "FAIL"
        self.test_var.set(f"{mark} - {result.get('detail')}")
        self._refresh()

    def _first_task(self) -> None:
        self.task_var.set("Asking the model to fix greeting.py in a disposable workspace...")
        self._run(self.service.run_first_task, self._after_task)

    def _after_task(self, result: dict) -> None:
        if result.get("error"):
            self.task_var.set(f"The task could not run: {result['error']}")
            return
        mark = "PASS" if result.get("ok") else "FAIL"
        self.task_var.set(f"{mark} - {result.get('detail')}")
        diff = result.get("diff") or ""
        try:
            self.diff_box.delete("1.0", tk.END)
            self.diff_box.insert("1.0", diff or "(no change was applied)")
        except Exception:
            pass
        self._refresh()

    def close(self) -> None:
        if self._poll_job is not None:
            try:
                self.window.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        try:
            self.window.destroy()
        except Exception:
            pass

    def state(self) -> dict:
        return self.service.status()
