"""Diagnostics export: useful facts in, prompts/secrets/source out."""
from __future__ import annotations

import json
import zipfile

from giscode.onboarding.diagnostics import (
    build_report,
    render_preview,
    write_zip,
)

PROMPT = "PROMPT_SENTINEL: explain my private project"
REPLY = "REPLY_SENTINEL: here is my source"
API_KEY = "sk-thisMustNeverAppear12345"
HOME = r"C:\Users\PrivatePerson"


def _status():
    return {
        "stage": "complete",
        "completed": True,
        "chosen_model": "qwen2.5-coder:7b",
        "download": {
            "model": "qwen2.5-coder:7b",
            "status": "done",
            "percent": 100,
            "verified": True,
        },
        "validation": {
            "ok": True,
            "model": "qwen2.5-coder:7b",
            "detail": "answered",
            "reply_preview": REPLY,
            "checks": [{"name": "inference", "ok": True, "detail": "ok"}],
        },
        "first_task": {
            "ok": True,
            "model": "qwen2.5-coder:7b",
            "prompt": PROMPT,
            "reply_preview": REPLY,
            "diff": "+ private source",
            "workspace": HOME + r"\workspace",
            "detail": "passed",
            "checks": [{"name": "verification", "ok": True, "detail": "ok"}],
        },
        "benchmark": {
            "verdict": "READY",
            "model": "qwen2.5-coder:7b",
            "correct": True,
            "detail": "correct",
            "performance": {"tokens_per_second": 20, "total_seconds": 1.2},
        },
        "last_error": f"failed with api_key={API_KEY} under {HOME}",
        "history": [
            {"event": "error", "detail": f"api_key={API_KEY} at {HOME}"},
            {"event": "prompt", "detail": PROMPT},
        ],
    }


def test_report_excludes_prompts_replies_diffs_and_secrets():
    report = build_report(
        product_version="0.1.1",
        status=_status(),
        hardware={"ram_total_bytes": 16, "summary": f"16 GB at {HOME}", "disk_path": HOME},
        runtime={"installed": True, "service_available": True, "version": "0.9.1", "models": ["qwen2.5-coder:7b"]},
        models=[{"id": "qwen2.5-coder:7b", "size_bytes": 1, "quantization": "Q4"}],
        provider_status={"available": True, "detail": "ok"},
        home=HOME,
    )
    text = json.dumps(report)
    assert PROMPT not in text
    assert REPLY not in text
    assert "+ private source" not in text
    assert API_KEY not in text
    assert HOME not in text
    assert '"reply_preview"' not in text
    assert '"prompt"' not in text
    assert '"diff"' not in text
    assert '"workspace"' not in text
    assert report["onboarding"]["validation"]["ok"] is True
    assert report["onboarding"]["benchmark"]["verdict"] == "READY"
    assert report["installed_models"][0]["quantization"] == "Q4"


def test_excluded_history_events_are_not_exported():
    report = build_report(status=_status(), home=HOME)
    events = [item["event"] for item in report["onboarding"]["history"]]
    assert "prompt" not in events
    assert "error" in events


def test_zip_contains_only_the_report_and_readme(tmp_path):
    report = build_report(product_version="0.1.1", status=_status(), home=HOME)
    target = write_zip(tmp_path / "diagnostics.zip", report)
    assert target.is_file()
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == ["README.txt", "diagnostics.json"]
        body = archive.read("diagnostics.json").decode("utf-8")
        readme = archive.read("README.txt").decode("utf-8")
    assert PROMPT not in body
    assert REPLY not in body
    assert API_KEY not in body
    assert "no prompts" in readme.lower()


def test_preview_is_produced_from_the_same_report():
    report = build_report(product_version="0.1.1", status=_status(), home=HOME)
    preview = render_preview(report)
    assert "will contain" in preview
    assert "will NOT contain" in preview
    assert "Credentials" in preview


class _DiagnosticsProvider:
    def status(self):
        return type(
            "Status",
            (),
            {"available": True, "detail": "ok", "version": "0.9.1", "secret_free_endpoint": "http://127.0.0.1:11434"},
        )()

    def list_models(self):
        return [type("Model", (), {"id": "qwen2.5-coder:7b", "size_bytes": 1, "quantization": "Q4", "context_length": 8192, "families": (), "capabilities": ("chat",)})()]


def test_service_export_writes_a_zip_without_prompt_fields(tmp_path, monkeypatch):
    from giscode.onboarding.service import OnboardingService

    monkeypatch.setenv("GISCODE_HOME", str(tmp_path / "profile"))
    service = OnboardingService(config_path=tmp_path / "config.json", provider=_DiagnosticsProvider())
    result = service.export_diagnostics(str(tmp_path / "export.zip"))
    assert result["path"].endswith("export.zip")
    with zipfile.ZipFile(result["path"]) as archive:
        body = archive.read("diagnostics.json").decode("utf-8")
    assert "qwen2.5-coder:7b" in body
    assert "reply_preview" not in body
    assert "PROMPT_SENTINEL" not in body
