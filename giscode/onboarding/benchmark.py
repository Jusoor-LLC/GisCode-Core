"""A measured coding benchmark for "Test This Model".

The existing Test My AI probe proves that a model answers. This module asks a
different question: can it produce a tiny, correct function on *this* machine,
and how fast does that happen? Performance and correctness are separate fields
because a fast wrong answer is not a usable coding model.

Safety boundaries:

* the generated source is parsed and checked against a deliberately small AST
  allowlist before anything is executed;
* execution happens in a new temporary directory with a separate interpreter,
  never in the user's project, and the directory is removed afterwards;
* imports, attribute access, calls, loops and other escape hatches are rejected,
  and a timeout bounds the check;
* only measurements and check results are persisted - the prompt and the model's
  source are not.

The thresholds are a product target, not a claim about a model: at least 8
tokens/second and a first token within 15 seconds is READY. A slower but correct
model is SLOW; a model that cannot load or exceeds safe resources is TOO LARGE;
a wrong, empty or unsafe answer is FAILED.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from giscode.onboarding.download import model_installed
from giscode.onboarding.first_task import extract_code
from giscode.providers.base import ProviderError

READY = "READY"
SLOW = "SLOW"
TOO_LARGE = "TOO_LARGE"
FAILED = "FAILED"

READY_MIN_TOKENS_PER_SECOND = 8.0
READY_MAX_FIRST_TOKEN_SECONDS = 15.0
MAX_CODE_CHARS = 8_000

BENCH_PROMPT = (
    "Write only Python code, with no explanation and no markdown. Define "
    "`add(a, b)` returning the sum of two numbers. The function must be pure "
    "and must not import anything."
)


@dataclass(frozen=True)
class BenchmarkResult:
    ok: bool
    verdict: str
    model: str
    detail: str
    load_ok: bool = False
    correct: bool = False
    first_token_seconds: Optional[float] = None
    generation_seconds: float = 0.0
    total_seconds: float = 0.0
    completion_tokens: int = 0
    tokens_per_second: Optional[float] = None
    tokens_estimated: bool = False
    speed_source: str = ""
    memory_rss_mb: Optional[float] = None
    system_available_mb: Optional[float] = None
    model_memory_bytes: Optional[int] = None
    model_vram_bytes: Optional[int] = None
    checks: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def performance(self) -> dict:
        return {
            "first_token_seconds": _round(self.first_token_seconds),
            "generation_seconds": round(self.generation_seconds, 3),
            "total_seconds": round(self.total_seconds, 3),
            "completion_tokens": int(self.completion_tokens),
            "tokens_per_second": _round(self.tokens_per_second),
            "tokens_estimated": bool(self.tokens_estimated),
            "speed_source": self.speed_source,
            "memory_rss_mb": _round(self.memory_rss_mb),
            "system_available_mb": _round(self.system_available_mb),
            "model_memory_bytes": self.model_memory_bytes,
            "model_vram_bytes": self.model_vram_bytes,
        }

    @property
    def correctness(self) -> dict:
        return {"ok": bool(self.correct), "deterministic_check": "add(2, 3) == 5"}

    def as_dict(self) -> dict:
        # Deliberately no prompt, model output, source or temporary path.
        return {
            "ok": bool(self.ok),
            "verdict": self.verdict,
            "model": self.model,
            "detail": self.detail,
            "load_ok": bool(self.load_ok),
            "correct": bool(self.correct),
            "performance": self.performance,
            "correctness": self.correctness,
            "checks": [dict(item) for item in self.checks],
        }


def _round(value: Optional[float]) -> Optional[float]:
    return round(float(value), 3) if value is not None else None


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:300]}


class _UnsafeSource(ValueError):
    """The generated source is outside the benchmark's execution allowlist."""


class _SafetyVisitor(ast.NodeVisitor):
    """Allow only a pure arithmetic function, never arbitrary Python."""

    _REJECT = (
        ast.Call,
        ast.Attribute,
        ast.Subscript,
        ast.Import,
        ast.ImportFrom,
        ast.ClassDef,
        ast.Lambda,
        ast.Global,
        ast.Nonlocal,
        ast.With,
        ast.Try,
        ast.Raise,
        ast.Assert,
        ast.Delete,
        ast.Yield,
        ast.YieldFrom,
        ast.Await,
        ast.AsyncFunctionDef,
        ast.While,
        ast.For,
        ast.AsyncFor,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
    )

    def generic_visit(self, node):  # noqa: ANN001 - ast dispatch signature
        if isinstance(node, self._REJECT):
            raise _UnsafeSource(
                f"generated code contains {type(node).__name__}, which is not allowed"
            )
        super().generic_visit(node)


def validate_generated_source(code: str) -> tuple[bool, str]:
    """Parse and narrowly validate the generated function without running it."""
    text = (code or "").strip()
    if not text:
        return False, "no code was returned"
    if len(text) > MAX_CODE_CHARS:
        return False, f"the code exceeds the {MAX_CODE_CHARS}-character limit"
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return False, f"the answer is not valid Python: {exc.msg}"
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != "add":
        return False, "the answer must define exactly one function named add"
    function = functions[0]
    if len(function.args.args) != 2 or function.args.vararg or function.args.kwarg:
        return False, "add must take exactly two positional arguments"
    if function.decorator_list:
        return False, "decorators are not allowed in the benchmark"
    try:
        _SafetyVisitor().visit(tree)
    except _UnsafeSource as exc:
        return False, str(exc)
    return True, "generated source passed the safe-AST check"


def _default_runner(code: str, timeout: float) -> tuple[bool, str]:
    """Execute the validated function in a disposable directory."""
    with tempfile.TemporaryDirectory(prefix="giscode-benchmark-") as directory:
        root = Path(directory)
        (root / "solution.py").write_text(code.rstrip() + "\n", encoding="utf-8")
        script = (
            "import solution\n"
            "assert solution.add(2, 3) == 5, solution.add(2, 3)\n"
            "assert solution.add(-1, 1) == 0, solution.add(-1, 1)\n"
            "assert solution.add(0, 0) == 0, solution.add(0, 0)\n"
            "print('BENCHMARK_OK')\n"
        )
        try:
            completed = subprocess.run(
                # ``-S`` avoids site packages. ``-I`` is deliberately not used:
                # it also removes the working directory, so ``import solution``
                # could never see the disposable file. The AST allowlist, not
                # the interpreter flags, is what makes the source safe.
                [sys.executable, "-S", "-c", script],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(5.0, float(timeout)),
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return False, f"the deterministic check exceeded {timeout:g}s"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"the deterministic check could not run: {type(exc).__name__}: {exc}"
        output = (completed.stdout or "").strip() or (completed.stderr or "").strip()
        if completed.returncode == 0 and "BENCHMARK_OK" in (completed.stdout or ""):
            return True, "add(2, 3) == 5, add(-1, 1) == 0 and add(0, 0) == 0"
        return False, (output or f"exit code {completed.returncode}")[:400]


def _ollama_model_memory(provider, model: str) -> tuple[Optional[int], Optional[int]]:
    """Ask Ollama's own process list for the loaded model's memory, if exposed."""
    endpoint = str(getattr(provider, "endpoint", "") or "").rstrip("/")
    if not endpoint:
        return None, None
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(endpoint + "/api/ps", timeout=3.0) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None, None
    entries = body.get("models") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return None, None
    wanted = (model or "").strip().lower()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("model") or "").strip().lower()
        if name != wanted and not (wanted and name.startswith(wanted + ":")):
            continue
        try:
            size = int(entry.get("size") or 0) or None
            vram = int(entry.get("size_vram") or 0) or None
        except (TypeError, ValueError):
            return None, None
        return size, vram
    return None, None


def _memory_snapshot() -> tuple[Optional[float], Optional[float]]:
    try:
        import psutil

        process = psutil.Process()
        memory = psutil.virtual_memory()
        return (
            float(process.memory_info().rss) / (1024 ** 2),
            float(memory.available) / (1024 ** 2),
        )
    except Exception:
        return None, None


def _model_ids(models) -> set[str]:
    result: set[str] = set()
    for item in models or ():
        value = getattr(item, "id", item)
        name = str(value or "").strip()
        if name:
            result.add(name)
    return result


def _exception_verdict(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(word in text for word in ("out of memory", "oom", "too large", "memory")):
        return TOO_LARGE
    return FAILED


def _call_stream(provider, model, messages, timeout):
    """Yield text deltas from a provider, or the one-shot reply as one delta."""
    stream = getattr(provider, "stream_chat", None)
    if callable(stream):
        for delta in stream(
            model,
            messages,
            # Room for a thinking model to finish its internal reasoning and
            # still emit the short answer; 256 was entirely consumed by
            # thinking on one locally installed model.
            max_tokens=512,
            temperature=0.0,
            timeout=timeout,
        ):
            yield delta
        return
    chat = getattr(provider, "chat", None)
    if not callable(chat):
        raise ProviderError("The provider has no way to answer a coding prompt.")
    reply = chat(
        model,
        messages,
        max_tokens=512,
        temperature=0.0,
        timeout=timeout,
    )
    from giscode.providers.streaming import StreamDelta

    yield StreamDelta(text=str(reply or ""))


def run_benchmark(
    provider,
    model: str,
    *,
    available_models=None,
    stream=None,
    runner: Optional[Callable[[str, float], tuple[bool, str]]] = None,
    timeout: float = 180.0,
    verify_timeout: float = 20.0,
) -> BenchmarkResult:
    """Measure load, latency, speed and deterministic correctness.

    ``provider`` is any object with ``status``/``list_models``/``stream_chat``
    (or ``chat``); the qualification suite supplies a fake, so no model or
    network is needed for tests. ``runner`` can replace the isolated execution
    step for a deterministic test.
    """
    name = (model or "").strip()
    if not name:
        return BenchmarkResult(
            ok=False,
            verdict=FAILED,
            model="",
            detail="No model was selected, so the benchmark cannot run.",
            checks=(_check("model_selected", False, "no model selected"),),
        )

    started = time.monotonic()
    try:
        status = provider.status()
    except Exception as exc:
        return BenchmarkResult(
            ok=False,
            verdict=FAILED,
            model=name,
            detail=f"The provider could not be asked whether it is running: {type(exc).__name__}: {exc}",
            checks=(_check("provider_reachable", False, str(exc)),),
        )
    if not getattr(status, "available", False):
        return BenchmarkResult(
            ok=False,
            verdict=FAILED,
            model=name,
            detail=f"Ollama is not answering: {getattr(status, 'detail', '') or 'no response'}",
            checks=(_check("provider_reachable", False, getattr(status, "detail", "")),),
        )

    if available_models is None:
        try:
            available_models = provider.list_models()
        except Exception:
            available_models = ()
    present = model_installed(name, _model_ids(available_models))
    checks: list[dict] = [
        _check("provider_reachable", True, "Ollama answered"),
        _check("model_present", present, f"{name} is installed" if present else f"{name} is not installed"),
    ]
    if not present:
        return BenchmarkResult(
            ok=False,
            verdict=TOO_LARGE,
            model=name,
            detail=f"{name} is not installed, so it cannot be loaded for the benchmark.",
            checks=tuple(checks),
        )

    messages = [{"role": "user", "content": BENCH_PROMPT}]
    pieces: list[str] = []
    usage: dict = {}
    first_token: Optional[float] = None
    completion_tokens = 0
    call = stream or _call_stream
    try:
        for delta in call(provider, name, messages, timeout):
            now = time.monotonic()
            text = str(getattr(delta, "text", "") or "")
            if text:
                if first_token is None:
                    first_token = now - started
                pieces.append(text)
            reported_usage = getattr(delta, "usage", None) or {}
            if isinstance(reported_usage, dict) and reported_usage:
                usage.update(reported_usage)
            if getattr(delta, "tokens", 0):
                completion_tokens = max(completion_tokens, int(delta.tokens))
            if getattr(delta, "done", False):
                break
    except Exception as exc:
        elapsed = time.monotonic() - started
        verdict = _exception_verdict(exc)
        return BenchmarkResult(
            ok=False,
            verdict=verdict,
            model=name,
            detail=(
                f"{verdict}: the model could not complete the benchmark: "
                f"{type(exc).__name__}: {exc}"
            ),
            load_ok=False,
            total_seconds=elapsed,
            checks=tuple(checks + [_check("inference", False, str(exc))]),
        )
    total = time.monotonic() - started
    reply = "".join(pieces)
    load_ok = bool(reply.strip()) and first_token is not None
    checks.append(
        _check(
            "load_and_first_token",
            load_ok,
            f"first token in {first_token:.2f}s" if first_token is not None else "no token arrived",
        )
    )
    if not load_ok:
        # A request that completed without a token is not evidence of an
        # oversized model. Only a measured memory/load error earns TOO_LARGE.
        return BenchmarkResult(
            ok=False,
            verdict=FAILED,
            model=name,
            detail="The model completed the request without producing a first token.",
            total_seconds=total,
            checks=tuple(checks),
        )

    completion_tokens = int(usage.get("completion_tokens") or completion_tokens or 0)
    estimated = completion_tokens <= 0
    if estimated:
        # A coarse fallback, explicitly labelled; nothing is presented as a
        # provider-reported token count when Ollama did not supply one.
        completion_tokens = max(1, int(len(reply) / 4))
    eval_ns = usage.get("eval_duration_ns") or usage.get("eval_duration") or 0
    if isinstance(eval_ns, (int, float)) and eval_ns > 0:
        generation_seconds = float(eval_ns) / 1_000_000_000
        speed_source = "ollama_eval"
    else:
        generation_seconds = max(0.0, total - (first_token or 0.0))
        speed_source = "wall_clock"
    tokens_per_second = (
        completion_tokens / generation_seconds if generation_seconds > 0 else None
    )

    code = extract_code(reply)
    safe, safety_detail = validate_generated_source(code)
    checks.append(_check("safe_code", safe, safety_detail if safe else f"not executed: {safety_detail}"))
    if not safe:
        return BenchmarkResult(
            ok=False,
            verdict=FAILED,
            model=name,
            detail=f"FAILED: {safety_detail}. The generated code was not executed.",
            load_ok=True,
            correct=False,
            first_token_seconds=first_token,
            generation_seconds=generation_seconds,
            total_seconds=total,
            completion_tokens=completion_tokens,
            tokens_per_second=tokens_per_second,
            tokens_estimated=estimated,
            speed_source=speed_source,
            checks=tuple(checks),
        )

    execute = runner or _default_runner
    try:
        correct, correctness_detail = execute(code, verify_timeout)
    except Exception as exc:
        correct, correctness_detail = False, f"check raised {type(exc).__name__}: {exc}"
    checks.append(_check("deterministic_test", correct, correctness_detail))

    rss, system_available = _memory_snapshot()
    model_memory, model_vram = _ollama_model_memory(provider, name)
    speed = tokens_per_second
    if not correct:
        verdict = FAILED
        detail = f"FAILED: the code did not pass the deterministic check: {correctness_detail}"
    elif (
        speed is not None
        and speed >= READY_MIN_TOKENS_PER_SECOND
        and first_token is not None
        and first_token <= READY_MAX_FIRST_TOKEN_SECONDS
    ):
        verdict = READY
        detail = (
            f"READY: {name} produced correct code at {speed:.1f} tokens/second; "
            f"first token in {first_token:.2f}s, total {total:.2f}s."
        )
    else:
        verdict = SLOW
        detail = (
            f"SLOW: {name} produced correct code, but measured performance is below "
            f"the target ({speed:.1f} tokens/second, first token "
            f"{first_token:.2f}s)."
            if speed is not None and first_token is not None
            else f"SLOW: {name} produced correct code, but token speed could not be measured."
        )
    return BenchmarkResult(
        # ``ok`` means the model produced correct code. SLOW is still a usable
        # answer; the separate verdict records the performance judgement.
        ok=bool(correct),
        verdict=verdict,
        model=name,
        detail=detail,
        load_ok=True,
        correct=bool(correct),
        first_token_seconds=first_token,
        generation_seconds=generation_seconds,
        total_seconds=total,
        completion_tokens=completion_tokens,
        tokens_per_second=speed,
        tokens_estimated=estimated,
        speed_source=speed_source,
        memory_rss_mb=rss,
        system_available_mb=system_available,
        model_memory_bytes=model_memory,
        model_vram_bytes=model_vram,
        checks=tuple(checks),
    )
