"""Connection test and coding-capability test - two different questions.

* **Connection** asks: is the provider there, is the model selectable, and does a
  minimal request complete? A green result says nothing about quality.
* **Coding** asks: does the model return something structurally usable as code?

The coding test never touches the user's project, never runs the model's code,
and never writes to disk. It parses the answer with :mod:`ast` and reports what
it found, so "it replied" cannot be mistaken for "it can write code".
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from typing import Optional

from giscode.providers.base import Provider, ProviderError

CONNECTION_PROMPT = "Reply with the single word: ready."
CODING_PROMPT = (
    "Write only Python code, no explanation and no markdown fences.\n"
    "Define a function `add(a, b)` returning the sum of two integers, and a "
    "pytest test `test_add` that asserts add(2, 3) == 5."
)


@dataclass(frozen=True)
class TestOutcome:
    ok: bool
    kind: str
    provider: str
    model: str
    seconds: float
    detail: str
    sample: str = ""
    checks: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "seconds": round(self.seconds, 2),
            "detail": self.detail,
            "sample": self.sample[:400],
            "checks": dict(self.checks),
        }


def connection_test(
    provider: Provider,
    model: str,
    *,
    timeout: float = 60.0,
) -> TestOutcome:
    """Ask for one word and report whether anything came back."""
    started = time.monotonic()
    try:
        content = provider.chat(
            model,
            [{"role": "user", "content": CONNECTION_PROMPT}],
            max_tokens=16,
            temperature=0.0,
            timeout=timeout,
        )
    except ProviderError as exc:
        return TestOutcome(
            ok=False,
            kind="connection",
            provider=provider.label,
            model=model,
            seconds=time.monotonic() - started,
            detail=f"{exc} {exc.action}".strip(),
        )
    except Exception as exc:  # pragma: no cover - defensive
        return TestOutcome(
            ok=False,
            kind="connection",
            provider=provider.label,
            model=model,
            seconds=time.monotonic() - started,
            detail=f"{type(exc).__name__}: {exc}",
        )
    elapsed = time.monotonic() - started
    text = (content or "").strip()
    if not text:
        return TestOutcome(
            ok=False,
            kind="connection",
            provider=provider.label,
            model=model,
            seconds=elapsed,
            detail="The provider answered, but with empty content.",
        )
    return TestOutcome(
        ok=True,
        kind="connection",
        provider=provider.label,
        model=model,
        seconds=elapsed,
        detail=f"Reached {provider.label} and got a reply in {elapsed:.1f}s.",
        sample=text[:200],
    )


def analyse_code_reply(text: str) -> dict:
    """Static checks on a model's code answer. Never executes it."""
    reply = (text or "").strip()
    # Models often wrap code in fences even when told not to.
    if "```" in reply:
        blocks = reply.split("```")
        for block in blocks:
            candidate = block
            if candidate.lstrip().lower().startswith("python"):
                candidate = candidate.lstrip()[len("python") :]
            if "def " in candidate:
                reply = candidate.strip()
                break
    checks = {"parses": False, "has_add": False, "has_test": False, "add_takes_two": False}
    try:
        tree = ast.parse(reply)
        checks["parses"] = True
    except SyntaxError:
        return checks
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == "add" and len(node.args.args) == 2:
                checks["has_add"] = True
                checks["add_takes_two"] = True
            if node.name.startswith("test_"):
                checks["has_test"] = True
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            checks["has_test"] = checks["has_test"] or True
    return checks


def coding_test(
    provider: Provider,
    model: str,
    *,
    timeout: float = 180.0,
) -> TestOutcome:
    """Ask for a tiny, well-specified function and judge the structure."""
    started = time.monotonic()
    try:
        content = provider.chat(
            model,
            [{"role": "user", "content": CODING_PROMPT}],
            max_tokens=400,
            temperature=0.0,
            timeout=timeout,
        )
    except ProviderError as exc:
        return TestOutcome(
            ok=False,
            kind="coding",
            provider=provider.label,
            model=model,
            seconds=time.monotonic() - started,
            detail=f"{exc} {exc.action}".strip(),
        )
    except Exception as exc:  # pragma: no cover - defensive
        return TestOutcome(
            ok=False,
            kind="coding",
            provider=provider.label,
            model=model,
            seconds=time.monotonic() - started,
            detail=f"{type(exc).__name__}: {exc}",
        )
    elapsed = time.monotonic() - started
    checks = analyse_code_reply(content)
    usable = checks["parses"] and checks["has_add"] and checks["add_takes_two"] and checks["has_test"]
    if usable:
        detail = f"The model returned a parseable `add(a, b)` with a test in {elapsed:.1f}s."
    elif not checks["parses"]:
        detail = "The answer was not valid Python, so it is not usable as code."
    else:
        missing = [
            name
            for name, key in (
                ("add(a, b)", "has_add"),
                ("a test function", "has_test"),
            )
            if not checks[key]
        ]
        detail = f"The answer parsed, but was missing {', '.join(missing)}."
    return TestOutcome(
        ok=usable,
        kind="coding",
        provider=provider.label,
        model=model,
        seconds=elapsed,
        detail=detail,
        sample=(content or "")[:400],
        checks=checks,
    )
