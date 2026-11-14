"""Test My AI: a real request, and a verdict that can only be honest.

"Reachable" and "can answer" are different claims, and onboarding needs the
second one. This module sends one deterministic prompt to the chosen model and
records what came back, how long it took and which checks passed, so the UI can
show evidence instead of a green tick that means nothing.

The check is deliberately cheap (a few dozen tokens, temperature 0) because its
job is to prove the pipeline works, not to benchmark the model.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from giscode.onboarding.download import model_installed
from giscode.providers.base import ProviderError

#: Deterministic and short. The marker is reported but not required: models add
#: punctuation and refuse odd instructions, and none of that means inference is
#: broken. An empty answer or an exception does.
PROBE_PROMPT = "Reply with exactly this token and nothing else: GISCODE_READY"
PROBE_MARKER = "GISCODE_READY"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    model: str
    detail: str
    seconds: float = 0.0
    reply_preview: str = ""
    checks: tuple[dict, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "model": self.model,
            "detail": self.detail,
            "seconds": round(self.seconds, 2),
            "reply_preview": self.reply_preview[:400],
            "checks": [dict(item) for item in self.checks],
        }


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:300]}


def validate(
    provider,
    model: str,
    *,
    available_models=None,
    chat: Optional[Callable[..., str]] = None,
    timeout: float = 90.0,
    max_tokens: int = 32,
) -> ValidationResult:
    """Prove that *model* answers on *provider*, or explain what failed."""
    name = (model or "").strip()
    checks: list[dict] = []
    if not name:
        return ValidationResult(
            ok=False,
            model="",
            detail="No model was selected, so there is nothing to test.",
            checks=(_check("model_selected", False, "no model selected"),),
        )

    if available_models is not None:
        present = model_installed(name, available_models)
        checks.append(
            _check(
                "model_present",
                present,
                f"{name} is installed" if present else f"{name} is not in Ollama's model list",
            )
        )
        if not present:
            return ValidationResult(
                ok=False,
                model=name,
                detail=(
                    f"{name} is not installed. Download it before testing; "
                    "GisCode will not substitute another model."
                ),
                checks=tuple(checks),
            )

    try:
        status = provider.status()
    except Exception as exc:
        return ValidationResult(
            ok=False,
            model=name,
            detail=f"The provider could not be asked whether it is running: {type(exc).__name__}: {exc}",
            checks=tuple(checks + [_check("provider_reachable", False, str(exc))]),
        )
    checks.append(
        _check(
            "provider_reachable",
            bool(status.available),
            status.detail or ("reachable" if status.available else "not reachable"),
        )
    )
    if not status.available:
        return ValidationResult(
            ok=False,
            model=name,
            detail=f"Ollama is not answering: {status.detail or 'no response'}",
            checks=tuple(checks),
        )

    call = chat or provider.chat
    started = time.monotonic()
    try:
        reply = call(
            name,
            [{"role": "user", "content": PROBE_PROMPT}],
            max_tokens=max_tokens,
            temperature=0.0,
            timeout=timeout,
        )
    except ProviderError as exc:
        elapsed = time.monotonic() - started
        action = getattr(exc, "action", "") or ""
        return ValidationResult(
            ok=False,
            model=name,
            detail=f"The model did not answer: {exc}" + (f" {action}" if action else ""),
            seconds=elapsed,
            checks=tuple(checks + [_check("inference", False, str(exc))]),
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        return ValidationResult(
            ok=False,
            model=name,
            detail=f"The model did not answer: {type(exc).__name__}: {exc}",
            seconds=elapsed,
            checks=tuple(checks + [_check("inference", False, str(exc))]),
        )

    elapsed = time.monotonic() - started
    text = str(reply or "").strip()
    checks.append(_check("inference", bool(text), "a reply was received" if text else "the reply was empty"))
    if not text:
        return ValidationResult(
            ok=False,
            model=name,
            detail="Ollama returned an empty reply, so the model is not usable yet.",
            seconds=elapsed,
            checks=tuple(checks),
        )

    marker = PROBE_MARKER in text.upper().replace(" ", "_")
    checks.append(
        _check(
            "marker",
            marker,
            "the model repeated the requested token"
            if marker
            else "the model answered in its own words; that still proves inference works",
        )
    )
    checks.append(
        _check(
            "latency",
            elapsed <= timeout,
            f"answered in {elapsed:.1f}s (limit {timeout:.0f}s)",
        )
    )
    return ValidationResult(
        ok=True,
        model=name,
        detail=(
            f"{name} answered in {elapsed:.1f}s. Inference works on this machine."
            + ("" if marker else " It did not repeat the exact token, which is allowed.")
        ),
        seconds=elapsed,
        reply_preview=text,
        checks=tuple(checks),
    )
