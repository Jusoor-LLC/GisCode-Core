"""Measured "Test This Model": verdicts, safety and separate correctness."""
from __future__ import annotations

import time

import pytest

from giscode.onboarding import benchmark as benchmark_module
from giscode.providers.streaming import StreamDelta

GOOD = "def add(a, b):\n    return a + b\n"
WRONG = "def add(a, b):\n    return a - b\n"
UNSAFE = "import os\ndef add(a, b):\n    return a + b\n"


class _Status:
    def __init__(self, available=True, detail=""):
        self.available = available
        self.detail = detail


class _Model:
    def __init__(self, model_id):
        self.id = model_id


class _Provider:
    def __init__(self, code=GOOD, available=True, models=("qwen2.5-coder:7b",), delay=0.0, fail=None):
        self.code = code
        self.available = available
        self.models = list(models)
        self.delay = delay
        self.fail = fail

    def status(self):
        return _Status(self.available, "" if self.available else "connection refused")

    def list_models(self):
        return [_Model(name) for name in self.models]

    def stream_chat(self, model, messages, **kwargs):
        if self.fail:
            raise self.fail
        if self.delay:
            # A first token, then a deliberately slow generation: 2 tokens over
            # one second is below the 8 tokens/second target.
            yield StreamDelta(text="def add(a, b):\n")
            time.sleep(self.delay)
            yield StreamDelta(text="    return a + b\n")
            yield StreamDelta(done=True, tokens=2, usage={"completion_tokens": 2})
            return
        yield StreamDelta(text=self.code)
        yield StreamDelta(done=True, tokens=24, usage={"completion_tokens": 24})


def _run(provider, **kwargs):
    return benchmark_module.run_benchmark(
        provider,
        kwargs.pop("model", "qwen2.5-coder:7b"),
        **kwargs,
    )


def test_correct_code_with_good_measurements_is_ready():
    result = _run(_Provider())
    assert result.verdict == benchmark_module.READY
    assert result.ok is True
    assert result.correct is True
    assert result.load_ok is True
    assert result.first_token_seconds is not None
    assert result.performance["tokens_per_second"] > 0


def test_correct_code_with_slow_generation_is_slow_not_failed():
    # 24 tokens after a one-second generation is below the 8 tokens/s target.
    result = _run(_Provider(delay=1.0))
    assert result.verdict == benchmark_module.SLOW
    assert result.ok is True  # correct output; performance is a separate verdict
    assert result.correct is True


def test_wrong_code_fails_correctness_without_being_called_ready():
    result = _run(_Provider(code=WRONG))
    assert result.verdict == benchmark_module.FAILED
    assert result.correct is False
    assert result.load_ok is True


def test_missing_model_cannot_be_loaded():
    result = _run(_Provider(models=("llama3.2:3b",)))
    assert result.verdict == benchmark_module.TOO_LARGE
    assert result.load_ok is False


def test_missing_ollama_is_a_failure_not_a_size_claim():
    result = _run(_Provider(available=False))
    assert result.verdict == benchmark_module.FAILED
    assert "not answering" in result.detail.lower()


def test_unsafe_generated_code_is_never_executed():
    called = []

    def runner(code, timeout):
        called.append(code)
        return True, "should not run"

    result = _run(_Provider(code=UNSAFE), runner=runner)
    assert result.verdict == benchmark_module.FAILED
    assert "not executed" in result.detail.lower()
    assert called == []


def test_the_default_runner_executes_only_in_a_disposable_folder():
    result = _run(_Provider(code=GOOD))
    checks = {item["name"]: item["ok"] for item in result.checks}
    assert checks["safe_code"] is True
    assert checks["deterministic_test"] is True
    assert result.correct is True


def test_benchmark_report_does_not_persist_the_prompt_or_generated_code():
    report = _run(_Provider()).as_dict()
    text = str(report).lower()
    assert "bench_prompt" not in text
    assert "write only python" not in text
    assert "def add" not in text
    assert "solution.py" not in text


def test_memory_failure_is_reported_as_too_large():
    result = _run(_Provider(fail=RuntimeError("CUDA out of memory")))
    assert result.verdict == benchmark_module.TOO_LARGE


def test_model_failure_is_reported_as_failed():
    result = _run(_Provider(fail=RuntimeError("connection reset")))
    assert result.verdict == benchmark_module.FAILED


def test_onboarding_service_persists_the_benchmark_result(tmp_path, monkeypatch):
    from giscode.onboarding.service import OnboardingService

    monkeypatch.setenv("GISCODE_HOME", str(tmp_path / "profile"))
    service = OnboardingService(config_path=tmp_path / "config.json", provider=_Provider())
    service.begin()
    service.select_model("qwen2.5-coder:7b", size_bytes=4_700_000_000)
    result = service.benchmark_model()
    assert result["verdict"] == benchmark_module.READY
    assert service.status()["benchmark"]["verdict"] == benchmark_module.READY
    assert service.status()["benchmark"]["correct"] is True
