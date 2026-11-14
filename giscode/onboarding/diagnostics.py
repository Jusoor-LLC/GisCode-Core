"""Diagnostics that help support without shipping the user's work.

The export is intentionally assembled from an allowlist. It contains machine
facts, provider/model metadata, status and benchmark numbers. It never contains
prompts, model replies, diffs, project source or conversation history, and all
free text is passed through the existing secret redactor plus home-directory and
user-name scrubbing.

The preview is generated from the same report object the ZIP receives, so what
the user is shown is what is exported.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from giscode.providers.redaction import redact_text

INCLUDED = (
    "GisCode version and operating-system/Python environment",
    "Measured hardware summary (RAM, VRAM, GPU, free disk)",
    "Ollama availability and version",
    "Installed model names and provider-reported metadata",
    "Provider connection status",
    "Sanitized onboarding errors and check results",
    "Coding benchmark measurements",
)
EXCLUDED = (
    "Prompts and conversation history",
    "Project source files and diffs",
    "Model replies or generated code",
    "Credentials, API keys, tokens and secrets",
    "Personal files and model contents",
)

_USER_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _scrub(text: Any, *, home: str = "") -> str:
    value = str(text if text is not None else "")
    if not value:
        return ""
    value = redact_text(value)
    value = _USER_RE.sub("[REDACTED_EMAIL]", value)
    candidates = []
    if home:
        candidates.extend((home, home.replace("\\", "/"), home.replace("/", "\\")))
    try:
        candidates.append(str(Path.home()))
    except Exception:
        pass
    for path in candidates:
        if path:
            value = value.replace(path, "<home>")
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if len(user) >= 3:
        value = value.replace(user, "<user>")
    return value[:800]


def _safe_checks(checks, *, include_detail: bool = True) -> list[dict]:
    result = []
    for item in checks or ():
        if not isinstance(item, dict):
            continue
        entry = {"name": _scrub(item.get("name")), "ok": bool(item.get("ok"))}
        if include_detail:
            entry["detail"] = _scrub(item.get("detail"))
        result.append(entry)
    return result


def _safe_model(item) -> dict:
    if isinstance(item, dict):
        data = item
    else:
        data = {
            "id": getattr(item, "id", ""),
            "size_bytes": getattr(item, "size_bytes", 0),
            "quantization": getattr(item, "quantization", ""),
            "context_length": getattr(item, "context_length", 0),
            "families": getattr(item, "families", ()),
            "capabilities": getattr(item, "capabilities", ()),
            "provider": getattr(item, "provider", ""),
        }
    return {
        "id": _scrub(data.get("id") or data.get("model") or data.get("name")),
        "provider": _scrub(data.get("provider")),
        "size_bytes": int(data.get("size_bytes") or 0),
        "quantization": _scrub(data.get("quantization")),
        "context_length": int(data.get("context_length") or 0),
        "families": [_scrub(v) for v in (data.get("families") or ())],
        "capabilities": [_scrub(v) for v in (data.get("capabilities") or ())],
    }


def _safe_runtime(runtime: Any) -> dict:
    if not isinstance(runtime, dict):
        return {}
    return {
        "installed": bool(runtime.get("installed")),
        "service_available": bool(runtime.get("service_available")),
        "version": _scrub(runtime.get("version")),
        "endpoint": _scrub(runtime.get("endpoint")),
        "models": [_scrub(name) for name in (runtime.get("models") or ())],
        "detail": _scrub(runtime.get("detail")),
        "action": _scrub(runtime.get("action")),
    }


def _safe_validation(validation: Any) -> dict:
    if not isinstance(validation, dict):
        return {}
    # reply_preview is intentionally absent.
    return {
        "ok": bool(validation.get("ok")),
        "model": _scrub(validation.get("model")),
        "detail": _scrub(validation.get("detail")),
        "seconds": float(validation.get("seconds") or 0),
        "checks": _safe_checks(validation.get("checks")),
    }


def _safe_first_task(task: Any) -> dict:
    if not isinstance(task, dict):
        return {}
    # prompt, reply_preview, diff and workspace are intentionally absent.
    return {
        "ok": bool(task.get("ok")),
        "model": _scrub(task.get("model")),
        "detail": _scrub(task.get("detail")),
        "seconds": float(task.get("seconds") or 0),
        # Verification output can quote generated code; keep only pass/fail.
        "checks": _safe_checks(task.get("checks"), include_detail=False),
        "file_count": len(task.get("files") or ()),
    }


def _safe_benchmark(benchmark: Any) -> dict:
    if not isinstance(benchmark, dict):
        return {}
    return {
        "ok": bool(benchmark.get("ok")),
        "verdict": _scrub(benchmark.get("verdict")),
        "model": _scrub(benchmark.get("model")),
        # The detail can quote a generated traceback line; omit it entirely.
        "load_ok": bool(benchmark.get("load_ok")),
        "correct": bool(benchmark.get("correct")),
        "performance": {
            key: value
            for key, value in (benchmark.get("performance") or {}).items()
            if key in {
                "first_token_seconds",
                "generation_seconds",
                "total_seconds",
                "completion_tokens",
                "tokens_per_second",
                "tokens_estimated",
                "speed_source",
                "memory_rss_mb",
                "system_available_mb",
                "model_memory_bytes",
                "model_vram_bytes",
            }
        },
        "correctness": benchmark.get("correctness") or {},
        "checks": _safe_checks(benchmark.get("checks"), include_detail=False),
    }


def build_report(
    *,
    product_version: str = "",
    status: Optional[dict] = None,
    environment: Optional[dict] = None,
    hardware: Optional[dict] = None,
    runtime: Optional[dict] = None,
    models=(),
    provider_status=None,
    benchmark: Optional[dict] = None,
    home: str = "",
) -> dict:
    """Assemble one privacy-reviewed report from allowlisted fields."""
    status = status if isinstance(status, dict) else {}
    hardware = hardware if isinstance(hardware, dict) else status.get("hardware") or {}
    runtime = runtime if isinstance(runtime, dict) else status.get("runtime") or {}
    benchmark = benchmark if isinstance(benchmark, dict) else status.get("benchmark") or {}

    if provider_status is None:
        provider_status = {}
    elif not isinstance(provider_status, dict):
        endpoint = getattr(provider_status, "secret_free_endpoint", None)
        if callable(endpoint):
            endpoint = endpoint()
        if endpoint is None:
            endpoint = getattr(provider_status, "endpoint", "")
        provider_status = {
            "available": bool(getattr(provider_status, "available", False)),
            "detail": str(getattr(provider_status, "detail", "")),
            "version": str(getattr(provider_status, "version", "")),
            "endpoint": str(endpoint or ""),
        }

    env = {
        "os": os.name,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count() or 0,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    env.update(environment or {})

    report = {
        "generated_at": _now(),
        "privacy": {"included": list(INCLUDED), "excluded": list(EXCLUDED)},
        "giscode": {
            "version": _scrub(product_version or "unknown"),
            "environment": {key: _scrub(value) for key, value in env.items()},
        },
        "hardware": {
            key: (_scrub(hardware.get(key)) if isinstance(hardware.get(key), str) else hardware.get(key))
            for key in (
                "ram_total_bytes",
                "ram_available_bytes",
                "vram_total_bytes",
                "vram_available_bytes",
                "gpu_name",
                "cpu_count",
                "disk_total_bytes",
                "disk_free_bytes",
                "disk_path",
                "os_name",
                "summary",
            )
            if key in hardware
        },
        "ollama": _safe_runtime(runtime),
        "installed_models": [_safe_model(item) for item in (models or ())],
        "provider_status": {
            key: _scrub(value) if isinstance(value, str) else value
            for key, value in provider_status.items()
            if key in {"available", "detail", "version", "endpoint", "kind", "label"}
        },
        "onboarding": {
            "stage": _scrub(status.get("stage")),
            "completed": bool(status.get("completed")),
            "legacy": bool(status.get("legacy")),
            "chosen_model": _scrub(status.get("chosen_model")),
            "download": {
                key: _scrub(value) if isinstance(value, str) else value
                for key, value in (status.get("download") or {}).items()
                if key
                in {
                    "model",
                    "status",
                    "percent",
                    "detail",
                    "bytes_done",
                    "bytes_total",
                    "attempts",
                    "verified",
                    "verification_detail",
                }
            },
            "storage": {
                key: _scrub(value) if isinstance(value, str) else value
                for key, value in (status.get("storage") or {}).items()
                if key
                in {
                    "current_path",
                    "recommended_path",
                    "free_gb",
                    "required_gb",
                    "sufficient",
                    "needs_override",
                    "detail",
                }
            },
            "validation": _safe_validation(status.get("validation")),
            "first_task": _safe_first_task(status.get("first_task")),
            "benchmark": _safe_benchmark(benchmark),
            "last_error": _scrub(status.get("last_error")),
            "history": [
                {
                    "at": _scrub(item.get("at")),
                    "event": _scrub(item.get("event")),
                    "detail": _scrub(item.get("detail")),
                }
                for item in (status.get("history") or ())[-20:]
                if isinstance(item, dict)
                and item.get("event")
                in {
                    "error",
                    "download_failed",
                    "download_cancelled",
                    "download_interrupted",
                    "validated",
                    "configured",
                }
            ],
        },
    }
    return report


def render_preview(report: dict) -> str:
    """Human-readable list of exactly what the ZIP will contain."""
    lines = [
        "This diagnostics ZIP will contain:",
    ]
    lines.extend(f"  - {item}" for item in (report.get("privacy") or {}).get("included", INCLUDED))
    lines.append("")
    lines.append("It will NOT contain:")
    lines.extend(f"  - {item}" for item in (report.get("privacy") or {}).get("excluded", EXCLUDED))
    lines.append("")
    lines.append(f"Report generated: {report.get('generated_at', '')}")
    lines.append(f"GisCode version: {(report.get('giscode') or {}).get('version', '')}")
    lines.append(f"Models listed: {len(report.get('installed_models') or [])}")
    return "\n".join(lines)


def write_zip(path: str | Path, report: dict) -> Path:
    """Write the allowlisted report; no project or conversation data is added."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    readme = (
        "GisCode Core diagnostics\n"
        "=======================\n\n"
        "diagnostics.json contains only the allowlisted machine, provider, model and\n"
        "benchmark facts described in its privacy section. It contains no prompts,\n"
        "conversation history, project source, model replies, credentials or personal\n"
        "files. Free text was redacted for secrets and home/user paths.\n"
    )
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", payload)
        archive.writestr("README.txt", readme)
    return target


def default_path(*, home: Optional[Path] = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = Path(home) if home is not None else Path.home()
    return base / "giscode-diagnostics" / f"giscode-diagnostics-{stamp}.zip"
