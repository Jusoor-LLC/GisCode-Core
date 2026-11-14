"""Headless onboarding, so a clean machine can be qualified without a desktop.

Two entry points, both reachable from ``python -m giscode.main``:

``--onboarding-check``
    Print the persisted first-run state as JSON and exit 0. Read-only: it loads
    the profile, reconciles an interrupted download and reports. This is what the
    clean-Windows qualification records as baseline evidence.

``--onboarding-run --model <tag> --yes``
    Drive the whole sequence with no GUI: detect Ollama, measure hardware, pick
    (or take) a model, download it with Ollama, run Test My AI, configure it, and
    run the first coding task. Exit code 0 means the first task passed.

``--yes`` is not a formality: it stands in for the click that confirms a
multi-gigabyte download, so without it the run refuses and transfers nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_CONFIRMATION_REQUIRED = 2
EXIT_RUNTIME_NOT_READY = 3
EXIT_NO_MODEL = 4
EXIT_DOWNLOAD_FAILED = 5
EXIT_VALIDATION_FAILED = 6
EXIT_FIRST_TASK_FAILED = 7


def _flag_value(args: list[str], name: str) -> str:
    for index, item in enumerate(args):
        if item == name and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return ""


def _write(payload: dict, path: str, emit: Callable[[str], None]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    if path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        emit(f"ONBOARDING_JSON={target}")
    else:
        emit(text)


def run_cli(
    argv: Optional[Iterable[str]] = None,
    *,
    service=None,
    emit: Callable[[str], None] = print,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    json_path = _flag_value(args, "--json")

    if service is None:
        from giscode.onboarding.service import OnboardingService

        service = OnboardingService()

    if "--onboarding-check" in args:
        payload = service.status()
        payload["mode"] = "check"
        _write(payload, json_path, emit)
        return EXIT_OK

    if "--onboarding-run" not in args:
        emit("usage: --onboarding-check | --onboarding-run --model <tag> --yes [--json <path>]")
        return EXIT_USAGE

    confirmed = "--yes" in args
    requested = _flag_value(args, "--model")

    service.begin()
    runtime = service.detect_runtime()
    hardware = service.assess_hardware()
    plan = service.plan()
    if not runtime.get("ready"):
        payload = {
            "mode": "run",
            "verdict": "NOT_READY",
            "reason": "ollama",
            "runtime": runtime,
            "recovery_hint": service.recovery_hint(),
        }
        _write(payload, json_path, emit)
        emit(f"ONBOARDING_RUNTIME_NOT_READY: {runtime.get('detail') or runtime.get('action')}")
        return EXIT_RUNTIME_NOT_READY

    recommendations = plan.get("recommended") or []
    chosen = requested or (recommendations[0]["model"] if recommendations else "")
    if not chosen:
        payload = {
            "mode": "run",
            "verdict": "NOT_READY",
            "reason": "no_candidate",
            "plan": plan,
            "hardware": hardware,
            "recovery_hint": plan.get("insufficient_explanation") or service.recovery_hint(),
        }
        _write(payload, json_path, emit)
        emit("ONBOARDING_NO_MODEL: nothing in the catalogue fits this machine as measured.")
        return EXIT_NO_MODEL

    size = 0
    for entry in recommendations + (plan.get("rejected") or []):
        if entry.get("model") == chosen:
            size = int(entry.get("size_bytes") or 0)
            break
    service.select_model(chosen, size_bytes=size)

    if not confirmed:
        payload = {
            "mode": "run",
            "verdict": "CONFIRMATION_REQUIRED",
            "model": chosen,
            "size_bytes": size,
            "note": "Re-run with --yes to confirm the download. Nothing was transferred.",
        }
        _write(payload, json_path, emit)
        emit(f"ONBOARDING_CONFIRMATION_REQUIRED: {chosen} ({size / (1024 ** 3):.1f} GB)")
        return EXIT_CONFIRMATION_REQUIRED

    service.confirm_download(chosen)
    service.start_download(chosen)
    job = service.wait_download(chosen)
    if job.get("status") != "done":
        payload = {
            "mode": "run",
            "verdict": "DOWNLOAD_FAILED",
            "model": chosen,
            "download": job,
            "recovery_hint": service.recovery_hint(),
        }
        _write(payload, json_path, emit)
        emit(f"ONBOARDING_DOWNLOAD_FAILED: {job.get('status')} {job.get('detail')}")
        return EXIT_DOWNLOAD_FAILED

    validation = service.validate_model(chosen)
    if not validation.get("ok"):
        payload = {
            "mode": "run",
            "verdict": "VALIDATION_FAILED",
            "model": chosen,
            "validation": validation,
            "recovery_hint": service.recovery_hint(),
        }
        _write(payload, json_path, emit)
        emit(f"ONBOARDING_VALIDATION_FAILED: {validation.get('detail')}")
        return EXIT_VALIDATION_FAILED

    configured = service.configure_model(chosen)
    task = service.run_first_task(chosen)
    payload = {
        "mode": "run",
        "verdict": "READY" if task.get("ok") else "FIRST_TASK_FAILED",
        "model": chosen,
        "size_bytes": size,
        "runtime": runtime,
        "hardware": hardware,
        "validation": validation,
        "configured": configured,
        "first_task": task,
        "state": service.status(),
    }
    _write(payload, json_path, emit)
    if task.get("ok"):
        emit(f"ONBOARDING_READY: {chosen} answered and fixed greeting.py")
        return EXIT_OK
    emit(f"ONBOARDING_FIRST_TASK_FAILED: {task.get('detail')}")
    return EXIT_FIRST_TASK_FAILED
