import math

import pytest

from giscode.inference import runtime


def test_default_ready_timeout_is_bounded():
    assert runtime.resolve_llama_ready_timeout(None) == 60.0
    assert runtime.MAX_LLAMA_READY_TIMEOUT == 600.0


def test_valid_configured_timeout(monkeypatch):
    monkeypatch.setenv(runtime.LLAMA_READY_TIMEOUT_ENV, "180")
    assert runtime.resolve_llama_ready_timeout() == 180.0


def test_invalid_timeout_uses_default(monkeypatch):
    for value in ("not-a-number", "0", "-1", "nan", "inf", "-inf"):
        monkeypatch.setenv(runtime.LLAMA_READY_TIMEOUT_ENV, value)
        assert runtime.resolve_llama_ready_timeout() == 60.0


def test_timeout_is_capped(monkeypatch):
    monkeypatch.setenv(runtime.LLAMA_READY_TIMEOUT_ENV, "9999")
    assert runtime.resolve_llama_ready_timeout() == 600.0


def test_ready_after_default_window_with_configured_timeout(monkeypatch):
    class Process:
        def poll(self):
            return None

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    # _wait_ready reads the clock three times: once for the deadline, once in
    # the loop condition and once for the remaining-time computation.
    now = iter((0.0, 61.0, 61.0))
    monkeypatch.setattr(runtime.time, "time", lambda: next(now))
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    instance = runtime.SubprocessLlamaCppRuntime(ready_timeout=120.0)
    instance._process = Process()
    assert instance._wait_ready(120.0) is True


def test_readiness_fails_after_configured_timeout(monkeypatch):
    class Process:
        def poll(self):
            return None

    now = iter((0.0, 121.0))
    monkeypatch.setattr(runtime.time, "time", lambda: next(now))
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    instance = runtime.SubprocessLlamaCppRuntime(ready_timeout=120.0)
    instance._process = Process()
    assert instance._wait_ready(120.0) is False


def test_reachable_but_loading_server_keeps_the_long_wait(monkeypatch):
    """A server that answers but is still loading must be waited for.

    Only a refused connection fails fast. A reachable server whose completion
    probe is not ready yet keeps the wait, and the outcome is reported as a
    readiness timeout, not as a failed chat request.
    """

    class Process:
        def poll(self):
            return None

    class HealthResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, *_args, **_kwargs):
        url = req if isinstance(req, str) else getattr(req, "full_url", "")
        if "/health" in url:
            return HealthResponse()
        raise runtime.urllib.error.HTTPError(url, 503, "Loading model", None, None)

    clock = {"now": 0.0}

    def fake_time():
        clock["now"] += 60.0
        return clock["now"]

    monkeypatch.setattr(runtime.time, "time", fake_time)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)

    instance = runtime.SubprocessLlamaCppRuntime(ready_timeout=120.0)
    instance._process = Process()
    with pytest.raises(runtime.InferenceError) as exc:
        instance._ensure_model_ready()
    assert "not ready" in str(exc.value).lower()
    assert "chat request failed" not in str(exc.value).lower()


def test_refused_connection_fails_fast_instead_of_waiting(monkeypatch):
    """Nothing listening must fail immediately, not after the whole timeout."""

    class Process:
        def poll(self):
            return None

    def fake_urlopen(*_args, **_kwargs):
        raise runtime.urllib.error.URLError("connection refused")

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    instance = runtime.SubprocessLlamaCppRuntime(ready_timeout=120.0)
    instance._process = Process()
    with pytest.raises(runtime.InferenceError) as exc:
        instance._ensure_model_ready()
    assert "chat request failed" in str(exc.value).lower()
    assert "connection refused" in str(exc.value).lower()
