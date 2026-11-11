"""Queue management panel shared by Core and Pro.

The panel deliberately uses plain ttk widgets so both editions can host it:
Core's themed ttk defaults already apply, and Pro's older UI is not forced to
adopt Ink & Ember. It owns no execution logic; every action delegates to the
host application, which supplies the edition-specific adapter.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

STATUS_LABELS = {
    "waiting": "WAITING",
    "running": "RUNNING",
    "needs_approval": "NEEDS APPROVAL",
    "completed": "COMPLETED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
    "interrupted": "INTERRUPTED",
}


def _selected_index(listbox: tk.Listbox):
    selection = listbox.curselection()
    return selection[0] if selection else None


def build_queue_panel(app, parent) -> dict:
    """Build the queue view on ``parent`` and return its widgets."""
    frame = ttk.LabelFrame(parent, text="TASK QUEUE", padding=(10, 6))
    frame.pack(fill=tk.X, pady=(0, 8))

    header = ttk.Frame(frame)
    header.pack(fill=tk.X)
    state = tk.StringVar(value="Idle")
    ttk.Label(header, textvariable=state).pack(side=tk.LEFT)
    ttk.Label(
        header,
        text="One task runs at a time · completed, failed and cancelled tasks stay in history",
    ).pack(side=tk.RIGHT)

    body = ttk.Frame(frame)
    body.pack(fill=tk.X, pady=(6, 0))
    listbox = tk.Listbox(body, height=5, exportselection=False, activestyle="none")
    scroll = ttk.Scrollbar(body, orient=tk.VERTICAL, command=listbox.yview)
    listbox.configure(yscrollcommand=scroll.set)
    listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
    scroll.pack(side=tk.LEFT, fill=tk.Y)

    column = ttk.Frame(body)
    column.pack(side=tk.LEFT, padx=(8, 0), fill=tk.Y)

    def button(text, command, row, column_index=0):
        widget = ttk.Button(column, text=text, command=command, width=14)
        widget.grid(row=row, column=column_index, sticky="ew", pady=1, padx=1)
        return widget

    button("Add request", app._queue_add_from_entry, 0)
    button("Start", app._queue_start, 1)
    button("Pause", app._queue_pause, 2)
    button("Resume", app._queue_resume, 3)
    button("Stop task", app._queue_cancel_current, 4)

    side = ttk.Frame(body)
    side.pack(side=tk.LEFT, padx=(8, 0), fill=tk.Y)

    def side_button(text, command, row):
        widget = ttk.Button(side, text=text, command=command, width=14)
        widget.grid(row=row, column=0, sticky="ew", pady=1, padx=1)
        return widget

    side_button("Move up", lambda: app._queue_move_selected(-1), 0)
    side_button("Move down", lambda: app._queue_move_selected(1), 1)
    side_button("Remove", app._queue_remove_selected, 2)
    side_button("Retry", app._queue_retry_selected, 3)
    side_button("Cancel queue", app._queue_cancel_all, 4)

    ttk.Button(frame, text="Clear finished", command=app._queue_clear_finished).pack(
        anchor=tk.E, pady=(6, 0)
    )

    widgets = {"frame": frame, "list": listbox, "state": state}
    app._queue_widgets = widgets
    app._queue_refresh()
    app.root.after(700, app._queue_tick)
    return widgets


def refresh(app) -> None:
    """Render the queue snapshot onto the widgets, if they exist."""
    widgets = getattr(app, "_queue_widgets", None)
    queue = getattr(app, "_task_queue", None)
    if not widgets or queue is None:
        return
    try:
        snapshot = queue.snapshot()
    except Exception:
        return
    listbox = widgets.get("list")
    if listbox is None or not listbox.winfo_exists():
        return
    listbox.delete(0, tk.END)
    running_index = snapshot.get("running_index")
    for index, item in enumerate(snapshot.get("items", [])):
        marker = "▶" if index == running_index else " "
        label = STATUS_LABELS.get(item.get("status"), str(item.get("status")).upper())
        text = f"{marker} {index + 1:02d}  {label:<14} {item.get('prompt', '')[:72]}"
        listbox.insert(tk.END, text)
        if item.get("status") == "running":
            listbox.itemconfigure(index, foreground="#D98A1A")
        elif item.get("status") == "completed":
            listbox.itemconfigure(index, foreground="#2E7D5B")
        elif item.get("status") in ("failed", "cancelled", "interrupted"):
            listbox.itemconfigure(index, foreground="#B23A3A")
        elif item.get("status") == "needs_approval":
            listbox.itemconfigure(index, foreground="#8A5A00")
    if snapshot.get("count") == 0:
        state_text = "Idle · no queued tasks"
    elif snapshot.get("waiting") == 0 and running_index is None:
        state_text = f"Idle · {snapshot['count']} task(s) finished"
    elif running_index is not None:
        state_text = (
            f"Running task {running_index + 1} of {snapshot['count']} · "
            f"{snapshot['waiting']} waiting"
        )
    else:
        state_text = f"{snapshot['waiting']} waiting · paused" if snapshot.get("paused") else f"{snapshot['waiting']} waiting"
    widgets["state"].set(state_text)


def selected_item(app):
    widgets = getattr(app, "_queue_widgets", None)
    queue = getattr(app, "_task_queue", None)
    if not widgets or queue is None:
        return None
    index = _selected_index(widgets["list"])
    items = queue.items()
    if index is None or index >= len(items):
        return None
    return items[index]
