"""Server lifecycle: restart, unload, and crash all leave zero orphans."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from giscode.inference import runtime as rt
from giscode.inference.api import InferenceAPI
from giscode.inference.errors import InferenceUnavailableError
from giscode.inference.lifecycle import ModelInfo, ModelState


class _FakePopen:
    def __init__(self, pid: int):
        self.pid = pid
        self._alive = True
        self._wait_timeout: float | None = None

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False


def _make_runtime(tmp_path: Path):
    server = tmp_path / "llama-server-fake.exe"
    server.write_text("# fake", encoding="utf-8")
    return rt.SubprocessLlamaCppRuntime(server_path=str(server), n_ctx=4096)


def test_restart_leaves_exactly_one_server(tmp_path):
    runtime = _make_runtime(tmp_path)
    p1 = _FakePopen(101)
    p2 = _FakePopen(202)
    with patch.object(rt.subprocess, "Popen", side_effect=[p1, p2]), \
         patch.object(runtime, "_wait_ready", return_value=True), \
         patch.object(rt, "_kill_process_tree", return_value=True):
        assert runtime.start_server(str(tmp_path / "m.gguf"))
        assert runtime.start_server(str(tmp_path / "m2.gguf"))
    assert runtime._process is p2
    assert not p1._alive, "old server process must be stopped on restart"
    assert p2._alive, "new server process must be alive"
    runtime.stop_server()


def test_unload_leaves_no_server(tmp_path):
    runtime = _make_runtime(tmp_path)
    proc = _FakePopen(303)
    with patch.object(rt.subprocess, "Popen", return_value=proc), \
         patch.object(runtime, "_wait_ready", return_value=True), \
         patch.object(rt, "_kill_process_tree", return_value=True):
        assert runtime.start_server(str(tmp_path / "m.gguf"))
    assert runtime._process is proc
    runtime.stop_server()
    assert runtime._process is None
    assert not proc._alive


def test_crash_leaves_no_server(tmp_path):
    runtime = _make_runtime(tmp_path)
    proc = _FakePopen(404)
    proc._alive = False
    runtime._process = proc
    rt._OWNED_SERVER_PIDS.add(proc.pid)
    with patch.object(rt, "_kill_process_tree", return_value=True):
        runtime.stop_server()
    assert runtime._process is None
    assert proc.pid not in rt._OWNED_SERVER_PIDS


def test_kill_process_tree_only_owned(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    with patch.object(rt.subprocess, "run", side_effect=fake_run):
        assert rt._kill_process_tree(999)
    assert calls == [["taskkill", "/F", "/T", "/PID", "999"]]


def test_report_foreign_servers_excludes_owned():
    rt._OWNED_SERVER_PIDS.add(1234)
    try:
        with patch("psutil.process_iter") as mock_iter:
            mock_iter.return_value = iter([])
            result = rt.report_foreign_servers()
        assert result == []
    finally:
        rt._OWNED_SERVER_PIDS.discard(1234)


def test_estimate_ready_timeout(tmp_path):
    small = tmp_path / "small.gguf"
    small.write_bytes(b"x" * int(0.5 * 1024 ** 3))
    assert rt.estimate_ready_timeout(str(small)) == 180.0
    large = tmp_path / "large.gguf"
    large.write_bytes(b"x" * int(2.0 * 1024 ** 3))
    assert rt.estimate_ready_timeout(str(large)) == 200.0
    missing = tmp_path / "missing.gguf"
    assert rt.estimate_ready_timeout(str(missing)) == 180.0


def test_ready_timeout_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("GISCODE_LLAMA_READY_TIMEOUT", "45")
    monkeypatch.setattr(rt, "DEFAULT_LLAMA_READY_TIMEOUT", 60.0)
    runtime = _make_runtime(tmp_path)
    assert runtime._ready_timeout_for(str(tmp_path / "m.gguf")) == 45.0

def _listening_port():
    import socket as _socket

    listener = _socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    return listener, listener.getsockname()[1]


def test_is_model_alive_requires_alive_process_and_open_port(tmp_path):
    listener, port = _listening_port()
    try:
        runtime = _make_runtime(tmp_path)
        runtime._process = _FakePopen(557)
        runtime._server_port = port
        assert runtime.is_model_alive(probe_timeout=1.0) is True
        dead = _FakePopen(558)
        dead._alive = False
        runtime._process = dead
        assert runtime.is_model_alive(probe_timeout=1.0) is False
        runtime._process = _FakePopen(559)
        runtime._server_port = 1  # closed port
        assert runtime.is_model_alive(probe_timeout=0.5) is False
    finally:
        listener.close()


def test_busy_server_stays_loaded(tmp_path, monkeypatch):
    """A live server whose HTTP health probe would fail still counts as loaded.

    The regression this pins: a liveness probe that failed while the server
    was busy (mid-generation) used to clear the cached READY state, so every
    later call in the same ticket failed with "No model loaded".
    """
    listener, port = _listening_port()
    try:
        runtime = _make_runtime(tmp_path)
        runtime._process = _FakePopen(555)
        runtime._server_port = port

        def _no_http(*args, **kwargs):
            raise OSError("server busy: health probe would time out")

        monkeypatch.setattr("urllib.request.urlopen", _no_http)
        api = InferenceAPI(runtime=runtime)
        api._model = ModelInfo(name="m", path="m.gguf", state=ModelState.READY)
        assert api._resolved_model_state_locked() is not None
        assert api.get_health()["model_loaded"] is True
    finally:
        listener.close()


def test_killed_server_is_detected_and_state_cleared(tmp_path):
    runtime = _make_runtime(tmp_path)
    proc = _FakePopen(556)
    proc._alive = False
    runtime._process = proc
    runtime._server_port = 1
    api = InferenceAPI(runtime=runtime)
    api._model = ModelInfo(name="m", path="m.gguf", state=ModelState.READY)
    assert api._resolved_model_state_locked() is None
    with pytest.raises(InferenceUnavailableError):
        api.chat_generate(rt.ChatRequest(messages=[rt.ChatMessage(role="user", content="hi")]))


def test_request_after_cancel_works():
    class _StubRuntime:
        def __init__(self):
            self.calls = 0

        def is_model_alive(self, probe_timeout: float = 5.0) -> bool:
            return True

        def chat_generate(self, request, cancel_event=None):
            self.calls += 1
            return rt.ChatResponse(text="ok")

    stub = _StubRuntime()
    api = InferenceAPI(runtime=stub)
    api._model = ModelInfo(name="m", path="m.gguf", state=ModelState.BUSY)
    api.cancel()
    assert api._model.state == ModelState.READY
    resp = api.chat_generate(rt.ChatRequest(messages=[rt.ChatMessage(role="user", content="hi")]))
    assert resp.text == "ok"
    assert api._model.state == ModelState.READY
    assert stub.calls == 1
