"""Real inference and coding readiness for a loaded AI module.

Nothing here simulates a model.  A verdict is produced only after the runtime
answers a chat request.  The coding check is a separate result: a model can be
fast and still fail to produce correct code, so performance and correctness are
never collapsed into one number.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

READY = "READY"
READY_SLOW = "READY - SLOW"
TOO_LARGE = "TOO LARGE FOR THIS PC"
FAILED = "FAILED"

SLOW_FIRST_TOKEN_SECONDS = 20.0
SLOW_TOKENS_PER_SECOND = 2.0

CODING_PROMPT = (
    "Write a Python function named add that takes two numbers and returns their "
    "sum. Return only the code, with no explanation."
)

_ALLOWED_AST = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Pass,
)


@dataclass(frozen=True)
class BenchmarkResult:
    status: str
    ok: bool
    detail: str
    model: str = ""
    first_token_seconds: Optional[float] = None
    tokens_per_second: Optional[float] = None
    completion_tokens: int = 0
    total_seconds: float = 0.0
    coding_ok: bool = False
    coding_detail: str = ""
    ram_before_bytes: int = 0
    ram_after_bytes: int = 0
    vram_before_bytes: int = 0
    vram_after_bytes: int = 0
    runtime_ram_bytes: int = 0
    runtime_vram_bytes: int = 0
    checks: tuple[dict, ...] = ()
    code: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "detail": self.detail,
            "model": self.model,
            "first_token_seconds": self.first_token_seconds,
            "tokens_per_second": self.tokens_per_second,
            "completion_tokens": self.completion_tokens,
            "total_seconds": self.total_seconds,
            "coding_ok": self.coding_ok,
            "coding_detail": self.coding_detail,
            "ram_before_bytes": self.ram_before_bytes,
            "ram_after_bytes": self.ram_after_bytes,
            "vram_before_bytes": self.vram_before_bytes,
            "vram_after_bytes": self.vram_after_bytes,
            "runtime_ram_bytes": self.runtime_ram_bytes,
            "runtime_vram_bytes": self.runtime_vram_bytes,
            "checks": list(self.checks),
            "code": self.code,
        }


def _resources() -> tuple[int, int]:
    try:
        from giscode.inference.resource_budget import SystemResourceDetector

        values = SystemResourceDetector().get_system_resources()
        return int(values.available_ram_bytes or 0), int(values.available_vram_bytes or 0)
    except Exception:
        return 0, 0


def _runtime_usage(inference) -> tuple[int, int]:
    """RSS and VRAM of the owned llama-server process, when measurable."""
    runtime = getattr(inference, "_runtime", None)
    process = getattr(runtime, "_process", None)
    pid = getattr(process, "pid", None)
    if not pid:
        return 0, 0
    ram = 0
    try:
        import psutil

        ram = int(psutil.Process(int(pid)).memory_info().rss)
    except Exception:
        ram = 0
    vram = 0
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in (completed.stdout or "").splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 2 and parts[0] == str(pid):
                vram = int(float(parts[1])) * 1024 * 1024
                break
    except Exception:
        vram = 0
    return ram, vram


def extract_code(reply: str) -> str:
    text = reply or ""
    blocks = re.findall(r"```(?:python|py)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if blocks:
        text = "\n".join(blocks)
    match = re.search(r"(?ms)^\s*def\s+add\s*\(.*", text)
    if match:
        text = match.group(0)
    return text.strip()


def validate_code(code: str) -> tuple[bool, str]:
    if not code.strip():
        return False, "the model did not return code"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"the returned code is not valid Python: {exc.msg}"
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST):
            return False, f"the returned code contains an unsupported construct ({type(node).__name__})"
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if not any(node.name == "add" and len(node.args.args) == 2 for node in functions):
        return False, "the returned code does not define add(a, b)"
    return True, "safe to execute in an isolated process"


def run_coding_check(code: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    safe, detail = validate_code(code)
    if not safe:
        return False, detail
    with tempfile.TemporaryDirectory(prefix="giscode-ai-check-") as directory:
        command = [sys.executable, "-I", "-c", code + "\n\nassert add(2, 3) == 5, '2 + 3'\nassert add(-4, 1) == -3, '-4 + 1'\n"]
        try:
            completed = subprocess.run(
                command,
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
            )
        except subprocess.TimeoutExpired:
            return False, "the generated code timed out"
        except OSError as exc:
            return False, f"the generated code could not be executed: {exc}"
    if completed.returncode == 0:
        return True, "the generated add(a, b) passed both checks"
    error = (completed.stderr or completed.stdout or "").strip().splitlines()
    return False, error[-1][:300] if error else "the generated code failed its checks"


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def run_readiness(
    inference,
    model: str,
    *,
    on_event: Optional[Callable[[str, str], None]] = None,
    timeout: float = 180.0,
) -> BenchmarkResult:
    """Ask the loaded model for code and measure the real response."""
    emit = on_event or (lambda *_: None)
    before_ram, before_vram = _resources()
    started = time.perf_counter()
    try:
        from giscode.inference.runtime import ChatMessage, ChatRequest

        status = inference.get_model_status()
        if status is None:
            return BenchmarkResult(
                status=FAILED,
                ok=False,
                detail="No model is loaded, so there is nothing to test.",
                model=model,
                checks=(_check("model_loaded", False, "no model loaded"),),
            )
        emit("test", "Asking the model to write a small function...")
        request = ChatRequest(
            messages=[
                ChatMessage(
                    role="user",
                    content=CODING_PROMPT,
                )
            ],
            max_tokens=96,
            temperature=0.0,
            reasoning_budget="off",
        )
        first_token: Optional[float] = None
        pieces: list[str] = []
        final_tokens = 0
        cancel_event = threading.Event()
        timer = threading.Timer(max(1.0, timeout), cancel_event.set)
        timer.daemon = True
        timer.start()
        try:
            try:
                stream = inference.chat_generate_stream(request, cancel_event=cancel_event)
            except TypeError:
                stream = inference.chat_generate_stream(request)
            for piece, reason, tokens in stream:
                if cancel_event.is_set():
                    raise TimeoutError("the model did not finish within the test timeout")
                if piece:
                    if first_token is None:
                        first_token = time.perf_counter() - started
                    pieces.append(piece)
                if tokens:
                    final_tokens = int(tokens)
                if reason:
                    break
                if time.perf_counter() - started > timeout:
                    raise TimeoutError("the model did not finish within the test timeout")
        finally:
            timer.cancel()
    except TimeoutError as exc:
        return BenchmarkResult(
            status=FAILED,
            ok=False,
            detail=f"The inference test timed out: {exc}",
            model=model,
            total_seconds=time.perf_counter() - started,
            checks=(_check("inference_timeout", False, str(exc)),),
        )
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        too_large = "insufficient" in text.lower() or "memory" in text.lower()
        return BenchmarkResult(
            status=TOO_LARGE if too_large else FAILED,
            ok=False,
            detail=(
                "The model could not be loaded in the available memory."
                if too_large
                else f"The model did not answer: {text}"
            ),
            model=model,
            total_seconds=time.perf_counter() - started,
            checks=(_check("inference", False, text),),
        )

    total = time.perf_counter() - started
    reply = "".join(pieces)
    if not reply.strip() or first_token is None:
        return BenchmarkResult(
            status=FAILED,
            ok=False,
            detail="The model completed without producing an answer.",
            model=model,
            total_seconds=total,
        )

    completion_tokens = final_tokens or max(1, len(reply) // 4)
    generation_seconds = max(0.001, total - first_token)
    tokens_per_second = completion_tokens / generation_seconds
    code = extract_code(reply)
    coding_ok, coding_detail = run_coding_check(code)
    after_ram, after_vram = _resources()
    runtime_ram, runtime_vram = _runtime_usage(inference)
    checks = (
        _check("runtime_started", True, "the local runtime answered"),
        _check("model_loaded", True, f"{model} is loaded"),
        _check("inference", True, f"first token in {first_token:.2f}s"),
        _check(
            "coding_correctness",
            coding_ok,
            coding_detail,
        ),
    )
    slow = first_token > SLOW_FIRST_TOKEN_SECONDS or tokens_per_second < SLOW_TOKENS_PER_SECOND
    status = READY_SLOW if slow else READY
    if coding_ok:
        detail = (
            f"Local inference works: first token in {first_token:.2f}s, "
            f"{tokens_per_second:.1f} tokens/s. The coding check passed."
        )
    else:
        detail = (
            f"Local inference works: first token in {first_token:.2f}s, "
            f"{tokens_per_second:.1f} tokens/s. The coding check failed, so this "
            "model should not be relied on for code yet."
        )
    return BenchmarkResult(
        status=status,
        ok=True,
        detail=detail,
        model=model,
        first_token_seconds=round(first_token, 3),
        tokens_per_second=round(tokens_per_second, 2),
        completion_tokens=completion_tokens,
        total_seconds=round(total, 3),
        coding_ok=coding_ok,
        coding_detail=coding_detail,
        ram_before_bytes=before_ram,
        ram_after_bytes=after_ram,
        vram_before_bytes=before_vram,
        vram_after_bytes=after_vram,
        runtime_ram_bytes=runtime_ram,
        runtime_vram_bytes=runtime_vram,
        checks=checks,
        code=code,
    )
