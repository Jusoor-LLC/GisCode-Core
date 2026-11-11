"""Project-level transaction recovery for the Core GUI.

The panel is a view over the native recovery engine. It lists durable
transaction records for the selected project, shows exactly which files an
interrupted task wrote, asks for explicit confirmation and then calls
``recover_pending_transactions`` - the same function the CLI uses. It does not
implement a second rollback path.

Unresolved work stays visible: a failed rollback keeps its record ``open``, so
the panel shows it again after a restart and the same action can be retried.
A record that cannot be parsed is reported as unreadable; nothing is ever
reported as recovered unless the engine says so.

Nothing here imports ``giscode.gisai`` or any Pro module.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

from giscode.agentic.recover_edits import (
    recover_pending_transactions,
    scan_transactions,
)

_CONFIRM_TITLE = "Recover interrupted transaction"


def _normalise_result(result) -> dict:
    """One engine result as a plain dict, whatever shape the runner used."""
    if isinstance(result, dict):
        return result
    outcome = getattr(result, "outcome", None)
    detail = {}
    if outcome is not None and hasattr(outcome, "as_dict"):
        try:
            detail = outcome.as_dict()
        except Exception:  # noqa: BLE001 - display only, never fatal
            detail = {}
    return {
        "task": str(getattr(result, "task", "") or ""),
        "status": str(getattr(result, "status", "") or ""),
        "blocked": str(getattr(result, "blocked", "") or ""),
        "result": detail,
    }


def summarise_results(results: list[dict]) -> tuple[bool, str]:
    """The user-facing account of a recovery pass.

    ``(complete, text)`` where *complete* is true only when every result is a
    real recovery. A conflict names the file and why it was left alone, so the
    message never reads as a full undo when it was not one.
    """
    if not results:
        return True, "No interrupted transaction needed recovery."
    lines: list[str] = []
    complete = True
    for item in results:
        task = item.get("task", "") or "transaction"
        status = item.get("status", "")
        detail = item.get("result") or {}
        restored = list(detail.get("restored") or [])
        removed = list(detail.get("removed") or [])
        if status == "recovered":
            done = ", ".join(restored + removed) or "nothing needed restoring"
            lines.append(f"{task}: recovered ({done}).")
            continue
        complete = False
        lines.append(f"{task}: {status or 'recovery required'}.")
        if item.get("blocked"):
            lines.append(f"  {item['blocked']}")
        for conflict in detail.get("conflicts") or []:
            path = conflict.get("path", "")
            reason = conflict.get("reason", "")
            lines.append(f"  left {path} untouched: {reason}.")
        if restored or removed:
            lines.append(
                "  already restored: " + ", ".join(restored + removed) + "."
            )
    return complete, "\n".join(lines)


class RecoveryPanel(ttk.Frame):
    """Compact recovery list for the project selected in the header."""

    def __init__(
        self,
        parent,
        *,
        project_root_provider: Callable[[], Optional[str]],
        home_root=None,
        recover_runner: Optional[Callable] = None,
        on_message: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent, padding=(0, 6))
        self._project_root_provider = project_root_provider
        self._home_root = home_root
        self._recover_runner = recover_runner or recover_pending_transactions
        self._on_message = on_message
        self._root_path: Optional[str] = None
        self._records: list[dict] = []
        self._results: "queue.Queue[list[dict]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._build()

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill=tk.X)

        ttk.Label(
            header, text="TRANSACTION RECOVERY", style="Gis.CardMuted.TLabel"
        ).pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value="No project selected.")
        ttk.Label(
            header, textvariable=self.status_var, style="Gis.Card.TLabel"
        ).pack(side=tk.LEFT, padx=(10, 0))

        self._refresh_btn = ttk.Button(
            header, text="Refresh", command=self.refresh
        )
        self._refresh_btn.pack(side=tk.RIGHT)
        self._recover_all_btn = ttk.Button(
            header,
            text="Recover all…",
            command=self.recover_all,
            state=tk.DISABLED,
        )
        self._recover_all_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self._recover_btn = ttk.Button(
            header,
            text="Recover selected…",
            command=self.recover_selected,
            state=tk.DISABLED,
            style="Gis.Accent.TButton",
        )
        self._recover_btn.pack(side=tk.RIGHT, padx=(6, 0))

        self._body = ttk.Frame(self)
        columns = ("status", "task", "files", "updated")
        self._tree = ttk.Treeview(
            self._body,
            columns=columns,
            show="headings",
            height=3,
            selectmode="browse",
        )
        for column, label, width in (
            ("status", "Status", 150),
            ("task", "Task", 220),
            ("files", "Files", 80),
            ("updated", "Updated", 150),
        ):
            self._tree.heading(column, text=label)
            self._tree.column(column, width=width, anchor=tk.W, stretch=True)
        scrollbar = ttk.Scrollbar(
            self._body, orient=tk.VERTICAL, command=self._tree.yview
        )
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=(6, 0))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=(6, 0))
        self._tree.bind("<<TreeviewSelect>>", lambda _event: self._show_details())

        self._details = tk.Text(self._body, height=5, wrap=tk.WORD)
        self._details.pack(fill=tk.X, pady=(4, 0))
        self._details.configure(state=tk.DISABLED)

        self._outcome = tk.Text(self, height=3, wrap=tk.WORD)
        self._outcome.configure(state=tk.DISABLED, relief=tk.FLAT)
        self._outcome.pack_forget()

    # ----------------------------------------------------------------- state

    @property
    def records(self) -> list[dict]:
        return list(self._records)

    def _current_root(self) -> Optional[str]:
        try:
            root = self._project_root_provider()
        except Exception:  # noqa: BLE001 - a host without a project is normal
            return None
        return str(root) if root else None

    def set_project(self, path: Optional[str]) -> None:
        """Point the panel at *path* and re-read its transaction records."""
        self._root_path = str(path) if path else None
        self.refresh()

    # --------------------------------------------------------------- refresh

    def refresh(self) -> list[dict]:
        root = self._current_root()
        if root != self._root_path:
            self._root_path = root
        if not root or not Path(root).is_dir():
            self._records = []
            self._tree.delete(*self._tree.get_children())
            self.status_var.set("No project selected.")
            self._show_body(False)
            self._set_actions_enabled(False)
            return []
        try:
            records = scan_transactions(root, home_root=self._home_root)
        except Exception as exc:  # noqa: BLE001 - report, never crash the tab
            self._records = []
            self._tree.delete(*self._tree.get_children())
            self.status_var.set(f"Recovery scan failed ({type(exc).__name__}).")
            self._show_body(False)
            self._set_actions_enabled(False)
            return []
        self._records = records
        self._tree.delete(*self._tree.get_children())
        for index, record in enumerate(records):
            files = record.get("files") or []
            updated = str(record.get("updated_at") or "").replace("T", " ")[:19]
            self._tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    record.get("label", ""),
                    record.get("task", ""),
                    f"{len(files)} file(s)" if files else "-",
                    updated,
                ),
            )
        pending = [item for item in records if item.get("pending")]
        unreadable = [item for item in records if item.get("status") == "unreadable"]
        if not records:
            self.status_var.set("No unfinished transactions.")
        elif pending:
            self.status_var.set(f"{len(pending)} transaction(s) need attention.")
        elif unreadable:
            self.status_var.set(
                f"{len(unreadable)} record(s) could not be read; nothing is pending."
            )
        else:
            self.status_var.set(
                f"No unfinished transactions ({len(records)} closed record(s))."
            )
        self._show_body(bool(records))
        self._set_actions_enabled(bool(pending))
        if pending:
            iid = str(records.index(pending[0]))
            self._tree.selection_set(iid)
            self._tree.focus(iid)
            self._show_details()
        else:
            self._details.configure(state=tk.NORMAL)
            self._details.delete("1.0", tk.END)
            self._details.configure(state=tk.DISABLED)
        return records

    def _show_body(self, visible: bool) -> None:
        if visible and not self._body.winfo_manager():
            self._body.pack(fill=tk.X, pady=(2, 0))
        elif not visible and self._body.winfo_manager():
            self._body.pack_forget()

    def _set_actions_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        self._recover_btn.configure(state=state)
        self._recover_all_btn.configure(state=state)

    # --------------------------------------------------------------- details

    def _selected_record(self) -> Optional[dict]:
        selection = self._tree.selection()
        if not selection:
            return None
        try:
            return self._records[int(selection[0])]
        except (IndexError, ValueError):
            return None

    def _show_details(self) -> None:
        record = self._selected_record()
        self._details.configure(state=tk.NORMAL)
        self._details.delete("1.0", tk.END)
        if record is not None:
            lines = [
                f"Task: {record.get('task', '')}",
                f"Status: {record.get('label', '')}",
            ]
            if record.get("project_root"):
                lines.append(f"Project: {record['project_root']}")
            lines.append(f"Record: {record.get('path', '')}")
            if record.get("updated_at"):
                lines.append(f"Updated: {record['updated_at']}")
            files = record.get("files") or []
            if files:
                lines.append("Files this task wrote:")
                for item in files:
                    state = "created by task" if item.get("created") else "modified by task"
                    lines.append(f"  - {item.get('path', '')} ({state})")
            if record.get("problem"):
                lines.append(f"Problem: {record['problem']}")
            if record.get("pending"):
                lines.append(
                    "Recovery restores the pre-task bytes of these files. A file "
                    "changed after the task wrote it is left untouched."
                )
            self._details.insert("1.0", "\n".join(lines))
        self._details.configure(state=tk.DISABLED)

    def _set_outcome(self, text: str) -> None:
        self._outcome.configure(state=tk.NORMAL)
        self._outcome.delete("1.0", tk.END)
        self._outcome.insert("1.0", text)
        self._outcome.configure(state=tk.DISABLED)
        if not self._outcome.winfo_manager():
            self._outcome.pack(fill=tk.X, pady=(4, 0))

    # -------------------------------------------------------------- recovery

    def recover_selected(self, *, wait: bool = False) -> list[dict]:
        record = self._selected_record()
        if record is None or not record.get("pending"):
            self.status_var.set("Select a transaction that still needs recovery.")
            return []
        return self._confirm_and_recover([record], wait=wait)

    def recover_all(self, *, wait: bool = False) -> list[dict]:
        pending = [item for item in self._records if item.get("pending")]
        if not pending:
            self.status_var.set("No transaction needs recovery.")
            return []
        return self._confirm_and_recover(pending, wait=wait)

    def _confirm_message(self, records: list[dict]) -> str:
        lines = ["These interrupted task(s) will be rolled back:", ""]
        for record in records:
            lines.append(f"  {record.get('task', '')}  ({record.get('label', '')})")
            for item in record.get("files") or []:
                state = "created by task" if item.get("created") else "modified by task"
                lines.append(f"    - {item.get('path', '')} ({state})")
        lines.extend(
            [
                "",
                "Files changed after the task wrote them are left untouched.",
                "Restore these files now?",
            ]
        )
        return "\n".join(lines)

    def _confirm_and_recover(
        self, records: list[dict], *, wait: bool = False
    ) -> list[dict]:
        if not messagebox.askyesno(
            _CONFIRM_TITLE, self._confirm_message(records), parent=self
        ):
            text = "Recovery cancelled. Nothing was changed."
            self.status_var.set(text)
            self._set_outcome(text)
            return []
        if wait:
            results = self._perform(records)
            self._finish(results)
            return results
        self._set_actions_enabled(False)
        self.status_var.set("Recovering…")
        self._worker = threading.Thread(
            target=lambda: self._results.put(self._perform(records)), daemon=True
        )
        self._worker.start()
        self.after(50, self._poll)
        return []

    def _perform(self, records: list[dict]) -> list[dict]:
        groups: dict[str, list[str]] = {}
        for record in records:
            profile_root = str(record.get("profile_root") or "")
            if profile_root:
                groups.setdefault(profile_root, []).append(str(record.get("task", "")))
        results: list[dict] = []
        for profile_root, tasks in groups.items():
            try:
                raw = self._recover_runner(
                    self._root_path,
                    profile_root=profile_root,
                    approve=lambda _task, _files: True,
                    tasks=set(tasks),
                )
            except TypeError:
                # A runner without the task filter still recovers the profile.
                raw = self._recover_runner(
                    self._root_path,
                    profile_root=profile_root,
                    approve=lambda _task, _files: True,
                )
            except Exception as exc:  # noqa: BLE001 - report, never claim success
                results.append(
                    {
                        "task": ", ".join(tasks),
                        "status": "recovery failed",
                        "blocked": f"{type(exc).__name__}: {exc}",
                        "result": {},
                    }
                )
                continue
            results.extend(_normalise_result(item) for item in raw or [])
        return results

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            results = self._results.get_nowait()
        except queue.Empty:
            self.after(50, self._poll)
            return
        self._finish(results)

    def _finish(self, results: list[dict]) -> None:
        self._worker = None
        complete, text = summarise_results(results)
        self.refresh()
        self.status_var.set(
            "Recovery finished." if complete else "Recovery needs attention."
        )
        self._set_outcome(text)
        if self._on_message is not None:
            try:
                self._on_message(text)
            except Exception:  # noqa: BLE001 - a reporting host is not fatal
                pass
