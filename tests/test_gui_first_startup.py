"""Focused regression tests: "GUI-first startup with deferred model autoload".

Production change (ONLY giscode/main.py):

  * bootstrap() now returns the 19 services WITHOUT synchronously invoking the
    blocking model/server autoload (previously that autoload work was mashed
    inline inside bootstrap, delaying the whole app by the full llama-server +
    ~4.97 GB GGUF cold load).
  * run() defers that autoload onto a background DAEMON thread (named
    "giscode-autoload") and STARTS it BEFORE entering root.mainloop(), so the
    window appears immediately while llama-server loads the selected model in
    the background.
  * The deferred worker receives the EXISTING logger / settings / model_manager
    objects (identity preserved - no duplicate services, no mid-bootstrap
    autoload).

These tests NEVER load a real model and NEVER spawn llama-server:

  * A uses the REAL bootstrap() (proven in-process pattern, mirrors
    tests/test_hang_vision_fix.py) with the REAL module-level
    _try_autoload_model replaced by a spy: we assert bootstrap() assembles the
    services WITHOUT calling the spy and that the produced model manager is
    still UNLOADED.
  * B+C drive the REAL run() through the REAL run.py entry path with
    bootstrap() replaced by a deterministic 19-slot stub and the REAL
    module-level _try_autoload_model replaced by a recording spy. run()'s
    `threading.Thread` global is swapped for the recording thread and
    run()'s `from giscode.gui import launch_gui` (module-global re-import at
    call time) targets a fake root. A fake root whose mainloop waits on the
    worker proves the daemon thread is STARTED before mainloop and that it
    receives the EXISTING logger/settings/model_manager objects (identity
    preserved).
  * D mirrors tests/test_chat_model_selector.py: MainWindow._poll_model_state
    must tolerate an initially-unloaded model manager without ever triggering
    a load.
  * E is run separately (tests/test_gui_project_workspace_threading.py) and
    stays green - the GUI threading regression suite is NOT duplicated here.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from types import SimpleNamespace
from typing import List, Tuple

import pytest

from giscode import main as main_module
from giscode.gui.main_window import MainWindow


# ---------------------------------------------------------------------------
# Shared spy state. All content is set synchronously on the main test thread
# (by spies on the real daemon worker) OR from the fake root whose mainloop
# waits on the real daemon thread, so there is no cross-thread visibility race.
# ---------------------------------------------------------------------------

_autoload_calls: List[tuple] = []
_autoload_order: List[str] = []
_worker_done = threading.Event()


def _spy_autoload(logger, settings, model_manager) -> None:
    """Runs on the REAL daemon thread; only records identity. It NEVER loads
    the 4.97 GB GGUF and NEVER spawns llama-server."""
    _autoload_calls.append((settings, model_manager))
    _autoload_order.append("worker:run")
    _worker_done.set()


class _StubLoggerName:
    """Recorder for logger-identity assertions; see _run_with_stubs."""

    def __init__(self) -> None:
        self.name: object = None


class _MonitoringStub:
    def startup(self, namespace: str = "default") -> None:
        return None

    def shutdown(self, namespace: str = "default") -> None:
        return None


class _StubThread(threading.Thread):
    """Real daemon thread that records construction order and then runs the
    real caller-supplied autoload spy on the background daemon."""

    _instances: List["_StubThread"] = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=""):
        super().__init__(target=target, args=args, kwargs=kwargs or {},
                         daemon=daemon, name=name)
        _autoload_order.append("thread:created")
        self.spec = (target, args, daemon, name)
        _StubThread._instances.append(self)

    def start(self) -> None:
        _autoload_order.append("thread:started")
        super().start()


class _FakeRoot:
    def mainloop(self) -> None:
        _autoload_order.append("mainloop:enter")
        if not _worker_done.wait(15.0):
            raise AssertionError("deferred autoload worker never finished")
        _autoload_order.append("mainloop:exit")


def _stub_bootstrap(settings_sentinel, model_manager_sentinel) -> tuple:
    services = [SimpleNamespace() for _ in range(19)]
    services[0] = settings_sentinel
    services[4] = model_manager_sentinel
    services[13] = _MonitoringStub()
    return tuple(services)


def test_bootstrap_does_not_synchronously_autoload(monkeypatch) -> None:
    _autoload_calls.clear()
    _autoload_order.clear()
    _worker_done.clear()

    monkeypatch.setattr(main_module, "_try_autoload_model", _spy_autoload)

    services = main_module.bootstrap()

    model_manager = services[4]
    assert _autoload_calls == [], (
        "bootstrap() must assemble services WITHOUT synchronously autoloading"
    )
    assert model_manager.get_current_model() is None, (
        "after bootstrap() the model manager must still be unloaded"
    )


def test_run_starts_deferred_worker_before_mainloop(
    monkeypatch, model_autoload_enabled
) -> None:
    _autoload_calls.clear()
    _autoload_order.clear()
    _worker_done.clear()
    _StubThread._instances.clear()

    settings_sentinel = object()
    model_manager_sentinel = object()

    monkeypatch.setattr(
        main_module, "bootstrap",
        lambda: _stub_bootstrap(settings_sentinel, model_manager_sentinel),
    )
    monkeypatch.setattr(main_module, "_try_autoload_model", _spy_autoload)
    monkeypatch.setattr(main_module, "threading",
                        SimpleNamespace(Thread=_StubThread))
    # run() does `from giscode.gui import launch_gui` inside the function at
    # call time; patch the attribute on the gui package, not on main_module.
    monkeypatch.setattr("giscode.gui.launch_gui", lambda **_kw: _FakeRoot())
    monkeypatch.setattr(sys, "argv", ["giscode"])

    main_module.run()

    order = list(_autoload_order)
    assert order.index("thread:started") < order.index("mainloop:enter"), (
        "the deferred autoload worker must START BEFORE mainloop is entered"
    )
    assert order.index("worker:run") < order.index("mainloop:exit"), (
        "the deferred worker must finish while the mainloop is running"
    )
    thread = _StubThread._instances[0]
    target, args, daemon, name = thread.spec
    assert daemon is True, "the deferred worker must be a daemon thread"
    assert name == "giscode-autoload", f"unexpected thread name: {name!r}"
    assert target is _spy_autoload, (
        "the deferred worker must target the module-level _try_autoload_model"
    )
    worker_logger, worker_settings, worker_model_manager = args
    assert isinstance(worker_logger, logging.Logger), (
        "the deferred worker must receive the existing logger"
    )
    assert worker_settings is settings_sentinel, (
        "the deferred worker must receive the existing settings object (identity)"
    )
    assert worker_model_manager is model_manager_sentinel, (
        "the deferred worker must receive the existing model_manager (identity)"
    )


class _UnloadedModelManager:
    """Initially-unloaded model manager whose fake contract EXACTLY mirrors
    tests/test_chat_model_selector.py::_FakeModelManager (same attributes the
    real MainWindow._poll_model_state reads at main_window.py:382-390). It
    NEVER loads anything; every load attempt is recorded."""

    def __init__(self) -> None:
        self.loaded = None
        self.load_calls = []
        self.unload_calls = []
        self._settings = {"model.selected_model": ""}
        self._inference = SimpleNamespace(
            get_health=lambda: {"model_state": "READY"}
        )

    def auto_register_discovered(self) -> None:
        return None

    def get_registry(self):
        return SimpleNamespace(list_all=lambda: [])

    def get_current_model(self):
        return self.loaded

    def load_model(self, name) -> None:
        self.load_calls.append(name)
        return None

    def unload_model(self, name) -> None:
        self.unload_calls.append(name)
        return None


def test_mainwindow_polling_tolerates_unloaded_model(tk_root) -> None:
    mm = _UnloadedModelManager()
    mw = MainWindow(tk_root, model_manager=mm, gisai_engine=None)

    mw._poll_model_state()

    assert mm.load_calls == [], (
        "polling an initially-unloaded manager must NEVER trigger a load"
    )
    assert mm.get_current_model() is None, "the model must stay unloaded"
