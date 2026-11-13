"""The whole first-run sequence, driven headlessly.

Covers the eleven scenarios the milestone asks for, each with the smallest fake
that can prove it: a brand-new profile, missing Ollama, Ollama present but not
answering, an existing installation, a model that is not installed, an inference
failure, a restart mid-sequence, an interrupted download, and the CLI paths.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from giscode.onboarding.cli import (
    EXIT_CONFIRMATION_REQUIRED,
    EXIT_OK,
    EXIT_RUNTIME_NOT_READY,
    EXIT_VALIDATION_FAILED,
    run_cli,
)
from giscode.onboarding.download import DONE, OllamaPuller
from giscode.onboarding.service import OnboardingService

GOOD_CODE = "```python\ndef greet(name):\n    return f\"Hello, {name}!\"\n```"


class _Status:
    def __init__(self, available, detail="", version="0.9.1", endpoint="http://127.0.0.1:11434"):
        self.available = available
        self.detail = detail
        self.version = version
        self.endpoint = endpoint
        self.model_count = 1


class _Model:
    def __init__(self, model_id):
        self.id = model_id


class _Provider:
    """Answers both onboarding prompts: the probe token and the coding task."""

    def __init__(self, available=True, models=("qwen2.5-coder:7b",), reply=None, fail=False):
        self.available = available
        self.models = list(models)
        self.reply = reply
        self.fail = fail

    def status(self):
        return _Status(self.available, "" if self.available else "connection refused")

    def list_models(self):
        return [_Model(name) for name in self.models]

    def chat(self, model, messages, **kwargs):
        if self.fail:
            raise RuntimeError("model crashed")
        content = messages[0]["content"]
        if self.reply is not None:
            return self.reply
        if "greeting.py" in content:
            return GOOD_CODE
        return "GISCODE_READY"


def _puller(counter=None):
    def runner(model, emit):
        if counter is not None:
            counter.append(model)
        emit("pulling 40% 2.0 GB/4.7 GB")
        emit("success")
        return 0

    return OllamaPuller(runner=runner)


@pytest.fixture(autouse=True)
def _isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("GISCODE_HOME", str(tmp_path / "profile"))
    return tmp_path


def _service(tmp_path, **kwargs) -> OnboardingService:
    return OnboardingService(config_path=tmp_path / "config.json", **kwargs)


# -- first launch -----------------------------------------------------------
def test_a_brand_new_profile_starts_incomplete(tmp_path):
    service = _service(tmp_path, provider=_Provider())
    status = service.begin()
    assert status["stage"] == "welcome"
    assert status["completed"] is False
    assert status["legacy"] is False


def test_the_sequence_only_completes_after_a_passing_first_task(tmp_path):
    service = _service(tmp_path, provider=_Provider(), puller=_puller())
    service.begin()
    assert service.detect_runtime()["ready"] is True
    service.assess_hardware()
    service.select_model("qwen2.5-coder:7b", size_bytes=4_700_000_000)
    service.confirm_download()

    job = service.start_download()
    assert service.wait_download(timeout=10)["status"] == DONE
    assert service.status()["completed"] is False  # a download is not setup

    validation = service.validate_model()
    assert validation["ok"] is True
    assert service.status()["completed"] is False  # an answer is not setup either

    service.configure_model()
    task = service.run_first_task(root=str(tmp_path / "workspaces"))
    assert task["ok"] is True
    assert service.status()["completed"] is True
    assert service.status()["stage"] == "complete"


# -- runtime states ---------------------------------------------------------
def test_missing_ollama_is_reported_with_the_official_route(tmp_path, monkeypatch):
    from giscode.onboarding import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "find_executable", lambda which=None: "")
    service = _service(tmp_path, provider=_Provider(available=False))
    report = service.detect_runtime()
    assert report["installed"] is False
    assert report["ready"] is False
    assert "official" in service.recovery_hint().lower()
    assert service.install_instructions()["automatic"] is False


def test_ollama_installed_but_not_answering_points_at_starting_it(tmp_path, monkeypatch):
    from giscode.onboarding import runtime as runtime_module

    service = _service(tmp_path, provider=_Provider(available=False))
    monkeypatch.setattr(runtime_module, "find_executable", lambda which=None: "ollama")
    report = service.detect_runtime()
    assert report["installed"] is True
    assert report["service_available"] is False
    assert "Start" in service.recovery_hint() or "start" in service.recovery_hint()


# -- consent ----------------------------------------------------------------
def test_a_download_without_confirmation_transfers_nothing(tmp_path):
    calls = []
    service = _service(tmp_path, provider=_Provider(), puller=_puller(calls))
    service.begin()
    service.detect_runtime()
    service.select_model("qwen2.5-coder:7b", size_bytes=4_700_000_000)
    with pytest.raises(PermissionError):
        service.start_download()
    assert calls == []


def test_configuration_is_refused_before_test_my_ai_passes(tmp_path):
    service = _service(tmp_path, provider=_Provider(), puller=_puller())
    service.begin()
    service.select_model("qwen2.5-coder:7b")
    with pytest.raises(PermissionError):
        service.configure_model()


# -- invalid / failing models ----------------------------------------------
def test_a_model_that_is_not_installed_fails_validation(tmp_path):
    service = _service(tmp_path, provider=_Provider(models=("llama3.2:3b",)), puller=_puller())
    service.begin()
    service.select_model("qwen2.5-coder:7b")
    result = service.validate_model()
    assert result["ok"] is False
    assert "not installed" in result["detail"]
    assert service.status()["last_error"]


def test_an_inference_failure_is_not_reported_as_ready(tmp_path):
    service = _service(tmp_path, provider=_Provider(fail=True), puller=_puller())
    service.begin()
    service.select_model("qwen2.5-coder:7b")
    result = service.validate_model()
    assert result["ok"] is False
    assert "did not answer" in result["detail"]
    assert service.status()["completed"] is False


# -- restart and recovery ---------------------------------------------------
def test_state_survives_a_restart_and_resumes_at_the_right_stage(tmp_path):
    first = _service(tmp_path, provider=_Provider(), puller=_puller())
    first.begin()
    first.detect_runtime()
    first.assess_hardware()
    first.select_model("qwen2.5-coder:7b", size_bytes=4_700_000_000)

    second = _service(tmp_path, provider=_Provider(), puller=_puller())
    status = second.status()
    assert status["stage"] == "model"
    assert status["chosen_model"] == "qwen2.5-coder:7b"
    assert status["history"], "actions must be recorded across restarts"


def test_an_interrupted_download_offers_a_retry_and_does_not_auto_resume(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "onboarding": {
                    "schema": 1,
                    "stage": "download",
                    "chosen_model": "qwen2.5-coder:7b",
                    "download": {"model": "qwen2.5-coder:7b", "status": "running", "percent": 55},
                }
            }
        ),
        encoding="utf-8",
    )
    calls = []
    service = _service(tmp_path, provider=_Provider(), puller=_puller(calls))
    status = service.status()
    assert status["download"]["status"] == "interrupted"
    assert "Retry" in status["recovery_hint"]
    assert calls == [], "an interrupted download must never restart itself"
    assert status["completed"] is False


# -- upgrade path -----------------------------------------------------------
def test_an_existing_installation_is_recognised_and_not_reset(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"model": {"selected_model": "Qwen3-VL-4B-Instruct"}}), encoding="utf-8")
    service = _service(tmp_path, provider=_Provider())
    status = service.begin()
    assert status["legacy"] is True
    assert status["completed"] is True
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["model"]["selected_model"] == "Qwen3-VL-4B-Instruct"


def test_configuring_the_model_writes_a_coding_profile(tmp_path):
    service = _service(tmp_path, provider=_Provider(), puller=_puller())
    service.begin()
    service.detect_runtime()
    service.select_model("qwen2.5-coder:7b")
    service.validate_model()
    configured = service.configure_model()
    assert configured["selected_model"] == "qwen2.5-coder:7b"
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["model"]["selected_model"] == "qwen2.5-coder:7b"
    assert data["providers"]["ollama"]["enabled"] is True
    assert any(item["model"] == "qwen2.5-coder:7b" for item in data["profiles"]["items"])


# -- CLI --------------------------------------------------------------------
def test_check_mode_reports_json_and_exits_zero(tmp_path):
    service = _service(tmp_path, provider=_Provider())
    lines = []
    assert run_cli(["--onboarding-check"], service=service, emit=lines.append) == EXIT_OK
    payload = json.loads("\n".join(lines))
    assert payload["mode"] == "check"
    assert payload["stage"] == "welcome"


def test_run_mode_without_confirmation_downloads_nothing(tmp_path):
    calls = []
    service = _service(tmp_path, provider=_Provider(), puller=_puller(calls))
    lines = []
    code = run_cli(
        ["--onboarding-run", "--model", "qwen2.5-coder:7b"], service=service, emit=lines.append
    )
    assert code == EXIT_CONFIRMATION_REQUIRED
    assert calls == []
    assert "CONFIRMATION_REQUIRED" in "\n".join(lines)


def test_run_mode_reaches_ready_with_a_working_model(tmp_path):
    service = _service(tmp_path, provider=_Provider(), puller=_puller())
    lines = []
    code = run_cli(
        ["--onboarding-run", "--model", "qwen2.5-coder:7b", "--yes"],
        service=service,
        emit=lines.append,
    )
    assert code == EXIT_OK, "\n".join(lines)[-2000:]
    assert "ONBOARDING_READY" in "\n".join(lines)
    assert service.status()["completed"] is True


def test_run_mode_stops_when_ollama_is_not_ready(tmp_path):
    service = _service(tmp_path, provider=_Provider(available=False), puller=_puller())
    lines = []
    code = run_cli(
        ["--onboarding-run", "--model", "qwen2.5-coder:7b", "--yes"],
        service=service,
        emit=lines.append,
    )
    assert code == EXIT_RUNTIME_NOT_READY


def test_run_mode_reports_a_failed_validation(tmp_path):
    service = _service(tmp_path, provider=_Provider(fail=True), puller=_puller())
    lines = []
    code = run_cli(
        ["--onboarding-run", "--model", "qwen2.5-coder:7b", "--yes"],
        service=service,
        emit=lines.append,
    )
    assert code == EXIT_VALIDATION_FAILED


# -- GUI contract -----------------------------------------------------------
def test_the_wizard_builds_and_reads_the_service_state(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:
        pytest.skip("no display available")
    root.withdraw()
    from giscode.config.settings import SettingsManager
    from giscode.gui.onboarding_wizard import OnboardingWizard

    wizard = OnboardingWizard(root, settings=SettingsManager(tmp_path / "config.json"))
    try:
        assert wizard.state()["stage"] == "welcome"
        assert wizard.window.winfo_exists()
    finally:
        wizard.close()
        root.destroy()
