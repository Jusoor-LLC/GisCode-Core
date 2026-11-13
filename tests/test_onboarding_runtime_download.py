"""Runtime detection and the Ollama pull driver.

Nothing here starts a real ``ollama pull``: the runner is injected, so the
consent gate, the duplicate guard, progress parsing, cancellation and retry are
all provable without a network or a multi-gigabyte download.
"""
from __future__ import annotations

import threading

import pytest

from giscode.onboarding import runtime as runtime_module
from giscode.onboarding.download import (
    CANCELLED,
    DONE,
    FAILED,
    OllamaPuller,
    PullError,
    model_installed,
    parse_progress,
)


class _Status:
    def __init__(self, available, detail="", version="", endpoint="http://127.0.0.1:11434", model_count=0):
        self.available = available
        self.detail = detail
        self.version = version
        self.endpoint = endpoint
        self.model_count = model_count


class _Model:
    def __init__(self, model_id: str):
        self.id = model_id


class _Provider:
    def __init__(self, available=True, models=(), detail="connection refused"):
        self.available = available
        self.models = list(models)
        self.detail = detail

    def status(self):
        return _Status(self.available, self.detail if not self.available else "", "0.9.1", model_count=len(self.models))

    def list_models(self):
        return [_Model(name) for name in self.models]


@pytest.fixture(autouse=True)
def _no_host_ollama(monkeypatch):
    monkeypatch.setattr(runtime_module, "find_executable", lambda which=None: "")


def test_reports_not_installed_when_there_is_no_executable():
    report = runtime_module.detect(_Provider(available=False))
    assert report.installed is False
    assert report.ready is False
    assert "not installed" in report.describe()


def test_distinguishes_installed_from_answering():
    report = runtime_module.detect(_Provider(available=False))
    assert report.installed is False and report.service_available is False

    runtime_module.find_executable = lambda which=None: r"C:\Users\x\AppData\Local\Programs\Ollama\ollama.exe"
    try:
        installed = runtime_module.detect(_Provider(available=False, detail="timed out"))
    finally:
        runtime_module.find_executable = lambda which=None: ""
    assert installed.installed is True
    assert installed.service_available is False
    assert "not answering" in installed.describe()
    assert "Start" in installed.action or "start" in installed.action


def test_a_running_service_lists_its_models():
    report = runtime_module.detect(_Provider(available=True, models=("qwen2.5-coder:7b", "llama3.2:3b")))
    assert report.ready is True
    assert report.version == "0.9.1"
    assert report.models == ("llama3.2:3b", "qwen2.5-coder:7b")
    assert "running" in report.describe()


def test_install_guidance_is_official_and_never_automatic():
    info = runtime_module.install_instructions()
    assert info["url"] == "https://ollama.com/download"
    assert info["automatic"] is False
    assert len(info["steps"]) >= 3
    assert "never" in info["note"].lower()


def test_progress_parsing_reads_percent_and_bytes():
    parsed = parse_progress("pulling 8934d96d3f08...  45% ▕████     ▏ 1.2 GB/2.6 GB")
    assert parsed["percent"] == 45
    assert parsed["bytes_total"] > parsed["bytes_done"] > 0


def test_progress_parsing_tolerates_lines_without_numbers():
    parsed = parse_progress("pulling manifest")
    assert "percent" not in parsed
    assert parsed["detail"] == "pulling manifest"


def test_a_pull_refuses_without_confirmation():
    called = []
    puller = OllamaPuller(runner=lambda model, emit: called.append(model) or 0)
    with pytest.raises(PermissionError):
        puller.start("qwen2.5-coder:7b", confirm=False)
    assert called == []


def test_an_already_installed_model_transfers_nothing():
    called = []
    puller = OllamaPuller(runner=lambda model, emit: called.append(model) or 0)
    job = puller.start("qwen2.5-coder:7b", confirm=True, installed=("qwen2.5-coder:7b",))
    assert job.status == DONE
    assert job.percent == 100
    assert called == []


def test_progress_and_success_are_reported():
    def runner(model, emit):
        emit("pulling manifest")
        emit("pulling abc  60% ▕███▏ 2.8 GB/4.7 GB")
        emit("success")
        return 0

    puller = OllamaPuller(runner=runner)
    puller.start("qwen2.5-coder:7b", confirm=True)
    job = puller.wait("qwen2.5-coder:7b", timeout=10)
    assert job.status == DONE
    assert job.exit_code == 0
    assert job.percent == 100


def test_a_failing_pull_is_failed_and_explained():
    puller = OllamaPuller(runner=lambda model, emit: 1)
    puller.start("qwen2.5-coder:7b", confirm=True)
    job = puller.wait("qwen2.5-coder:7b", timeout=10)
    assert job.status == FAILED
    assert "code 1" in job.detail


def test_a_second_start_reuses_the_running_pull():
    gate = threading.Event()

    def runner(model, emit):
        emit("pulling manifest")
        gate.wait(5)
        return 0

    puller = OllamaPuller(runner=runner)
    first = puller.start("qwen2.5-coder:7b", confirm=True)
    second = puller.start("qwen2.5-coder:7b", confirm=True)
    assert second.attempts == 1
    assert second.started_at == first.started_at
    gate.set()
    assert puller.wait("qwen2.5-coder:7b", timeout=10).status == DONE


def test_cancelling_stops_the_job_and_says_so():
    gate = threading.Event()

    def runner(model, emit):
        emit("pulling abc 10% 0.5 GB/4.7 GB")
        gate.wait(5)
        return 0

    puller = OllamaPuller(runner=runner)
    puller.start("qwen2.5-coder:7b", confirm=True)
    job = puller.cancel("qwen2.5-coder:7b")
    assert job.status == CANCELLED
    gate.set()
    puller.wait("qwen2.5-coder:7b", timeout=10)


def test_a_retry_is_a_new_attempt_and_needs_confirmation():
    puller = OllamaPuller(runner=lambda model, emit: 1)
    puller.start("qwen2.5-coder:7b", confirm=True)
    puller.wait("qwen2.5-coder:7b", timeout=10)
    with pytest.raises(TypeError):
        puller.retry("qwen2.5-coder:7b")  # confirm is keyword-only and required
    puller.retry("qwen2.5-coder:7b", confirm=True)
    job = puller.wait("qwen2.5-coder:7b", timeout=10)
    assert job.attempts == 2


def test_an_empty_tag_is_refused():
    puller = OllamaPuller(runner=lambda model, emit: 0)
    with pytest.raises(PullError):
        puller.start("   ", confirm=True)


def test_model_presence_tolerates_the_default_tag():
    assert model_installed("qwen2.5-coder", ("qwen2.5-coder:latest",)) is True
    assert model_installed("qwen2.5-coder:7b", ("qwen2.5-coder:7b",)) is True
    assert model_installed("qwen2.5-coder:7b", ("llama3.2:3b",)) is False
