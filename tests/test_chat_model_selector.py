import tkinter as tk
from types import SimpleNamespace

from giscode.gui.main_window import MainWindow


class _FakeModelManager:
    def __init__(self):
        self.loaded = None
        self.load_calls = []
        self.unload_calls = []
        self._settings = {"model.selected_model": "Model-B"}
        self._inference = SimpleNamespace(get_health=lambda: {"model_state": "READY"})

    def auto_register_discovered(self):
        pass

    def get_registry(self):
        return SimpleNamespace(
            list_all=lambda: [
                SimpleNamespace(name="Model-A"),
                SimpleNamespace(name="Model-B"),
                SimpleNamespace(name="mmproj-x"),
            ]
        )

    def get_current_model(self):
        return self.loaded

    def load_model(self, name):
        self.load_calls.append(name)
        self.loaded = SimpleNamespace(name=name)
        return None

    def unload_model(self, name):
        self.unload_calls.append(name)
        self.loaded = None
        return None


def _make_window(tk_root):
    mm = _FakeModelManager()
    mw = MainWindow(tk_root, model_manager=mm, gisai_engine=None)
    mw._refresh_model_list()
    return mw, mm


def test_chat_and_device_share_one_selector(tk_root):
    mw, _ = _make_window(tk_root)
    mw.chat_model_combo.set("Model-A")
    assert mw._model_var.get() == "Model-A"
    assert mw._model_combo.get() == "Model-A"
    mw._model_combo.set("Model-B")
    assert mw.chat_model_combo.get() == "Model-B"


def test_polling_does_not_overwrite_chat_selection(tk_root):
    mw, mm = _make_window(tk_root)
    mm.loaded = SimpleNamespace(name="Model-B")
    mw._poll_model_state()
    assert mw.chat_model_combo.get() == "Model-B"
    mw._model_switch_active = True
    mw.chat_model_combo.set("Model-A")
    mw._poll_model_state()
    assert mw.chat_model_combo.get() == "Model-A"
    mw._model_switch_active = False
    mw._poll_model_state()
    assert mw.chat_model_combo.get() == "Model-B"
    assert mw._model_combo.get() == "Model-B"


def test_selecting_a_does_not_revert_to_b_during_polling(tk_root):
    mw, mm = _make_window(tk_root)
    mm.loaded = SimpleNamespace(name="Model-B")
    mw._poll_model_state()
    assert mw.chat_model_combo.get() == "Model-B"
    mw._model_switch_active = True
    mw.chat_model_combo.set("Model-A")
    for _ in range(3):
        mw._poll_model_state()
    assert mw.chat_model_combo.get() != "Model-B"
    assert mw.chat_model_combo.get() == "Model-A"
    mm.loaded = SimpleNamespace(name="Model-A")
    mw._model_switch_active = False
    mw._poll_model_state()
    assert mw.chat_model_combo.get() == "Model-A"


def test_load_and_unload_use_selected_model(monkeypatch, tk_root):
    class _SyncThread:
        def __init__(self, target=None, args=(), **kwargs):
            self._target = target
            self._args = args

        def start(self):
            if self._target is not None:
                self._target(*self._args)

    monkeypatch.setattr("giscode.gui.main_window.threading.Thread", _SyncThread)
    mw, mm = _make_window(tk_root)
    mw.chat_model_combo.set("Model-A")
    mw._load_selected_model()
    assert mm.load_calls == ["Model-A"]
    mw._poll_model_state()
    assert mw.chat_model_combo.get() == "Model-A"
    assert mw._model_combo.get() == "Model-A"
    mw._unload_current_model()
    assert mm.unload_calls == ["Model-A"]


def test_finish_model_switch_success_reflects_loaded_model(tk_root):
    mw, _ = _make_window(tk_root)
    mw._model_switch_active = True
    mw._finish_model_switch(SimpleNamespace(name="Model-A"), None, mw._model_switch_epoch)
    assert mw._model_switch_active is False
    assert mw.chat_model_combo.get() == "Model-A"


def test_finish_model_switch_failure_reverts_to_previous(tk_root):
    mw, _ = _make_window(tk_root)
    mw._model_switch_previous = "Model-B"
    mw.chat_model_combo.set("Model-A")
    mw._model_switch_active = True
    mw._finish_model_switch(None, RuntimeError("boom"), mw._model_switch_epoch)
    assert mw._model_switch_active is False
    assert mw.chat_model_combo.get() == "Model-B"


def test_worker_completion_is_applied_by_gui_poll_on_main_thread(tk_root):
    mw, _ = _make_window(tk_root)
    mw._model_switch_active = True
    epoch = mw._model_switch_epoch
    mw._model_switch_results.put((SimpleNamespace(name="Model-A"), None, epoch))

    mw._poll_model_state()

    assert mw._model_switch_active is False
    assert mw.chat_model_combo.get() == "Model-A"
    assert mw._model_status_var.get() == "Model-A (Ready)"


def test_worker_failure_is_applied_without_reporting_ready(tk_root):
    mw, _ = _make_window(tk_root)
    mw._model_switch_previous = "Model-B"
    mw.chat_model_combo.set("Model-A")
    mw._model_switch_active = True
    epoch = mw._model_switch_epoch
    mw._model_switch_results.put((None, RuntimeError("load failed"), epoch))

    mw._poll_model_state()

    assert mw._model_switch_active is False
    assert mw.chat_model_combo.get() == "Model-B"
    assert "failed" in mw._model_status_var.get().lower()
