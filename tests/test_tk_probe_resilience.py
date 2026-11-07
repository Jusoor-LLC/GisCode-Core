"""A single transient Tcl init failure must not disable a GUI test module.

Four GUI modules (``test_gui_project_workspace``, ``test_gui_visual_design``,
``test_gui_text_interaction``, ``test_planer_inference_wiring``) used to probe Tk
with one bare ``tk.Tk()`` at import time and then ``skipif`` every test in the
module if that one attempt raised.  In this environment creating a Tcl interpreter repeatedly in one
process intermittently fails while pytest is still importing modules::

    TclError: Can't find a usable init.tcl in the following directories: ...

The next creation in the same process (milliseconds later) succeeds, and the
tests themselves pass, so those skips reported an environment limitation that
did not exist.  The probes now go through :func:`tests.conftest.tk_available`,
which retries exactly like the existing ``_create_tk_root`` helper.
"""
from __future__ import annotations

import importlib
import importlib.util
import tkinter as tk

import pytest

from tests import conftest

_PROBED_MODULES = (
    "tests.test_gui_project_workspace",
    "tests.test_gui_visual_design",
    "tests.test_gui_text_interaction",
    "tests.test_planer_inference_wiring",
)

# The public Core tree does not ship every GUI test module (the text
# interaction and planer wiring suites stay in this repository), so probing one
# that is absent would report a missing file instead of a wiring defect.
GUI_MODULES = tuple(
    name for name in _PROBED_MODULES if importlib.util.find_spec(name) is not None
)
if not GUI_MODULES:  # pragma: no cover - a tree with no GUI tests at all
    pytest.skip("no GUI test module to probe in this tree", allow_module_level=True)


def test_probe_survives_one_transient_tcl_failure(monkeypatch):
    """The exact flake seen during collection: fail once, then work."""
    real_tk = tk.Tk
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise tk.TclError(
                "Can't find a usable init.tcl in the following directories: "
                "/nonexistent/lib/tcl8.6"
            )
        return real_tk(*args, **kwargs)

    monkeypatch.setattr(tk, "Tk", flaky)

    assert conftest.tk_available() is True
    assert len(calls) >= 2, "the probe must retry instead of giving up"


def test_probe_still_reports_a_headless_environment(monkeypatch):
    """A persistent failure is still reported as 'Tk unavailable'."""

    def broken(*args, **kwargs):
        raise tk.TclError("no display name and no $DISPLAY environment variable")

    monkeypatch.setattr(tk, "Tk", broken)

    assert conftest.tk_available() is False


def test_probe_reports_available_in_this_environment():
    assert conftest.tk_available() is True


@pytest.mark.parametrize("module_name", GUI_MODULES)
def test_gui_modules_wire_their_skip_mark_to_the_probe(module_name):
    """The module-level probe must agree with the shared helper."""
    if not conftest.tk_available():  # pragma: no cover - headless only
        pytest.skip("Tk cannot initialise in this environment")

    module = importlib.import_module(module_name)

    assert module._TK_OK is True
