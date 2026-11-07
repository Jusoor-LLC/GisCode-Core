"""Native mouse/keyboard text interaction for the GisCode GUI.

Tk already gives ``tk.Text`` selection, ``Ctrl+C`` / ``Ctrl+V`` / ``Ctrl+X``
and ``Ctrl+A`` (select all) for free, and those work on a ``disabled``
``Text`` widget too. What the app was missing was:

* a consistent right-click menu, and
* a copy affordance for the single-line *informational* values (status
  lines, project paths, model/status text) that are shown with
  ``ttk.Label`` and therefore cannot take a selection at all.

This module adds only interaction to widgets that already display text.
It never changes how that text is produced, stored, or rendered, and it
deliberately leaves buttons, icons, dropdowns, listboxes and canvas
items alone -- those keep their normal interaction.

Two entry points:

``add_text_context_menu(widget, editable=...)``
    Right-click Cut / Copy / Paste / Select All on a ``Text`` or
    ``Entry``. Read-only widgets get Copy + Select All only, so the menu
    never advertises an edit that cannot happen.

``make_value_copyable(widget, value_getter=...)``
    Right-click Copy on a Label/Entry-like value widget, for text a user
    may want to reuse but which cannot be selected by dragging.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

__all__ = [
    "add_text_context_menu",
    "copy_text_selection",
    "make_value_copyable",
    "menu_labels",
    "select_all_text",
]

# Widget classes whose text content the user can select and reuse.
_TEXT_WIDGET_CLASSES = frozenset(
    {"Text", "Entry", "TEntry", "Listbox", "TListbox"}
)


def menu_labels(target) -> list:
    """Command labels of ``target``'s context menu (separators skipped).

    Accepts either the :class:`tk.Menu` itself or the widget the menu is
    attached to, so callers do not have to remember which one they hold.
    """
    if hasattr(target, "entrycget"):
        menu = target
    else:
        menu = getattr(target, "_giscode_text_menu", None)
    if menu is None:
        raise TypeError(
            f"{type(target).__name__} has no text context menu attached"
        )
    return _menu_labels(menu)


def _is_readonly(widget) -> bool:
    """True when the widget cannot be edited."""
    try:
        state = str(widget.cget("state"))
    except tk.TclError:
        return False
    return state in ("disabled", "readonly")


def select_all_text(widget) -> None:
    """Select the whole contents of a ``Text``, ``Entry`` or ``Listbox``."""
    cls = widget.winfo_class()
    if cls == "Text":
        widget.tag_add(tk.SEL, "1.0", tk.END)
        widget.mark_set(tk.INSERT, "1.0")
        widget.see(tk.INSERT)
    elif cls in ("Entry", "TEntry"):
        widget.selection_range(0, tk.END)
        widget.icursor(tk.END)
    elif cls in ("Listbox", "TListbox"):
        widget.selection_set(0, tk.END)
    else:
        raise TypeError(f"select_all_text does not support {cls!r}")


def _entry_selection_bounds(widget):
    """Return ``(first, last)`` integer indices for an Entry selection.

    tkinter's ``Entry`` exposes no ``selection_first``/``selection_last``
    methods, and ``Entry.get()`` takes no index arguments either, so the
    Tcl ``sel.first``/``sel.last`` indices are read and the caller slices
    the full value.
    """
    try:
        if not widget.selection_present():
            return None
        return int(widget.index("sel.first")), int(widget.index("sel.last"))
    except (tk.TclError, ValueError):
        return None


def copy_text_selection(widget) -> str:
    """Copy the current selection of ``widget`` to the clipboard.

    Returns the copied text (empty string when nothing was selected).
    The selection is read directly rather than relying on the virtual
    ``<<Copy>>`` event, which silently does nothing for some widget
    states and is awkward to assert on in tests.
    """
    text = _selected_text(widget)
    if text:
        widget.clipboard_clear()
        widget.clipboard_append(text)
    return text


def _selected_text(widget) -> str:
    """The selection without touching the clipboard."""
    cls = widget.winfo_class()
    if cls == "Text":
        ranges = widget.tag_ranges(tk.SEL)
        return widget.get(ranges[0], ranges[-1]) if ranges else ""
    if cls in ("Entry", "TEntry"):
        bounds = _entry_selection_bounds(widget)
        return widget.get()[bounds[0]:bounds[1]] if bounds else ""
    if cls in ("Listbox", "TListbox"):
        return "".join(widget.get(i) + "\n" for i in widget.curselection())
    raise TypeError(f"text selection is not supported for {cls!r}")


def _menu_labels(menu: tk.Menu) -> list:
    """Command labels of ``menu``, skipping separators.

    ``entrycget`` raises for a separator row, so callers that introspect
    the menu (including tests) must filter by entry type.
    """
    labels = []
    for index in range(menu.index("end") + 1):
        if menu.type(index) == "command":
            labels.append(menu.entrycget(index, "label"))
    return labels


def add_text_context_menu(
    widget,
    *,
    editable: Optional[bool] = None,
    name: Optional[str] = None,
) -> tk.Menu:
    """Attach a right-click Cut/Copy/Paste/Select All menu to ``widget``.

    ``editable`` defaults to "whatever the widget currently allows", so
    a ``state="disabled"`` output pane gets Copy/Select All only while an
    input box also gets Cut/Paste/Delete.

    A ``<Control-a>`` select-all binding is added only for widget classes
    where Tk does not already provide one, and only when the application
    has not bound it itself. It is added without swallowing the event so
    the native behaviour still runs.

    ``name`` is the role used in error messages for debugging.

    Returns the menu so callers can inspect it in tests.
    """
    if editable is None:
        editable = not _is_readonly(widget)

    role = name or widget.winfo_class()
    if widget.winfo_class() not in _TEXT_WIDGET_CLASSES:
        raise TypeError(
            f"{role!r} is a {widget.winfo_class()}; text interaction only "
            f"applies to Text, Entry and Listbox"
        )

    menu = tk.Menu(widget, tearoff=0)

    # Entries are created enabled; ``_show`` refines the enabled state
    # from the live selection just before the menu pops up. Creating
    # them already-disabled would leave Cut/Copy/Delete permanently
    # greyed out, because a Menu entry's state is not re-evaluated.
    if editable:
        menu.add_command(label="Cut", command=lambda: _do_cut(widget))
    menu.add_command(label="Copy", command=lambda: copy_text_selection(widget))
    if editable:
        menu.add_command(label="Paste", command=lambda: _do_paste(widget))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: _do_delete(widget))
    menu.add_command(label="Select All", command=lambda: select_all_text(widget))

    def _show(event):
        # Refresh the enabled/disabled state for the current selection.
        has_selection = bool(_selected_text(widget))
        if editable:
            menu.entryconfigure("Cut", state="normal" if has_selection else tk.DISABLED)
            menu.entryconfigure("Delete", state="normal" if has_selection else tk.DISABLED)
        menu.entryconfigure("Copy", state="normal" if has_selection else tk.DISABLED)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
    widget.bind("<Button-3>", _show)

    # Tk's Text already does Ctrl+A; Entry/Listbox on Windows do not
    # reliably. Only fill the gap, and never override an app binding.
    cls = widget.winfo_class()
    if cls in ("Entry", "TEntry", "Listbox", "TListbox") and not widget.bind("<Control-a>"):
        widget.bind("<Control-a>", lambda _event: select_all_text(widget))

    widget._giscode_text_menu = menu  # type: ignore[attr-defined]
    return menu


def _selection_start(widget) -> str:
    """The index where the current selection begins."""
    cls = widget.winfo_class()
    if cls == "Text":
        try:
            return widget.index(tk.SEL_FIRST)
        except tk.TclError:
            return tk.INSERT
    bounds = _entry_selection_bounds(widget)
    if bounds:
        return bounds[0]
    return tk.INSERT


def _do_cut(widget) -> None:
    text = _selected_text(widget)
    if not text:
        return
    widget.clipboard_clear()
    widget.clipboard_append(text)
    _delete_selection(widget)


def _do_paste(widget) -> None:
    try:
        data = widget.clipboard_get()
    except tk.TclError:
        return
    # Replace the selection in place, the way Windows does: remember
    # where it started before removing it, otherwise the insert mark
    # (which sits after any previously inserted text) would append.
    start = _selection_start(widget)
    _delete_selection(widget)
    widget.insert(start, data)
    cls = widget.winfo_class()
    if cls in ("Listbox", "TListbox"):
        # A Listbox has no insert mark; the caller already appended it.
        return


def _do_delete(widget) -> None:
    _delete_selection(widget)


def _delete_selection(widget) -> None:
    cls = widget.winfo_class()
    if cls == "Text":
        try:
            widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            pass
    elif cls in ("Entry", "TEntry"):
        bounds = _entry_selection_bounds(widget)
        if bounds:
            widget.delete(bounds[0], bounds[1])


def make_value_copyable(
    widget,
    value_getter: Optional[Callable[[], str]] = None,
    *,
    label: str = "Copy",
):
    """Give a value widget (Label/Entry-like) a right-click Copy action.

    ``ttk.Label`` cannot take a selection, so status lines, project paths
    and model/status text shown as labels would otherwise be impossible
    to reuse. This adds the conventional Windows "right-click -> Copy"
    affordance instead of trying to make the label selectable.

    ``value_getter`` defaults to reading the bound ``textvariable`` and
    then the literal ``text`` option, so it keeps working as the value
    changes.

    Returns ``widget`` so it can wrap a widget literal in a layout call,
    e.g. ``make_value_copyable(ttk.Label(...)).grid(...)``. The menu is
    left on ``widget._giscode_value_menu`` for inspection.
    """
    if value_getter is None:
        def value_getter() -> str:
            variable = None
            try:
                variable = widget.cget("textvariable")
            except tk.TclError:
                variable = None
            if variable:
                try:
                    return str(widget.tk.globalgetvar(variable))
                except tk.TclError:
                    pass
            try:
                return str(widget.cget("text"))
            except tk.TclError:
                return ""

    def _copy_value() -> str:
        text = value_getter() or ""
        if text:
            widget.clipboard_clear()
            widget.clipboard_append(text)
        return text

    existing = getattr(widget, "_giscode_value_menu", None)
    if existing is not None:
        existing.entryconfigure(label, command=_copy_value)
        return widget

    menu = tk.Menu(widget, tearoff=0)
    menu.add_command(label=label, command=_copy_value)

    def _show(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    widget.bind("<Button-3>", _show)
    widget._giscode_value_menu = menu  # type: ignore[attr-defined]
    return widget

