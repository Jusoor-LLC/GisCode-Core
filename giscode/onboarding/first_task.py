"""The first real coding task - small, bounded and actually verified.

Onboarding is not finished when a model answers a greeting; it is finished when
the user has watched GisCode take a broken file, ask the local model for a fix,
apply it, and *check the result*. That is the smallest end-to-end slice of the
product that is still the product.

Safety boundaries, all enforced here:

* The workspace is a **new disposable directory inside the profile**, never an
  existing project, and nothing outside it is written.
* Exactly **one file** is replaced, it must be the file the task is about, and
  the new content must arrive inside a fenced code block capped at 20 KB.
* The model's text is never executed as a shell command. Verification runs
  GisCode's own assertion with a timeout, and the diff is always recorded so the
  change is reviewable.
* A reply without a code block fails the task and writes nothing - a partial or
  guessed fix is worse than no fix.

Everything is injectable (``chat``, ``verify``) so the qualification suite can
prove the pass and fail paths without a model or a GPU.
"""
from __future__ import annotations

import difflib
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from giscode.config.paths import giscode_home

FILENAME = "greeting.py"

#: The starting file: syntactically valid, behaviourally wrong. A model that
#: only reformats it cannot pass, because the assertion checks the result.
INITIAL_SOURCE = (
    '"""A tiny greeting helper used by the GisCode first-run task."""\n'
    "\n"
    "\n"
    "def greet(name):\n"
    '    return "Hello " + name\n'
)

EXPECTED_CALL = "greet('Ada')"
EXPECTED_VALUE = "Hello, Ada!"

TASK_BRIEF = (
    "The file greeting.py contains a greet(name) function that must return "
    "'Hello, Ada!' when called with 'Ada'. It currently returns 'Hello Ada'. "
    "Return the complete corrected contents of greeting.py in a single "
    "```python code block, with no other commentary."
)

_MAX_REPLY_CHARS = 20_000
_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class FirstTaskResult:
    ok: bool
    detail: str
    workspace: str = ""
    model: str = ""
    prompt: str = ""
    reply_preview: str = ""
    diff: str = ""
    seconds: float = 0.0
    checks: tuple[dict, ...] = field(default_factory=tuple)
    files: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "workspace": self.workspace,
            "model": self.model,
            "prompt": self.prompt,
            "reply_preview": self.reply_preview[:600],
            "diff": self.diff[:4000],
            "seconds": round(self.seconds, 2),
            "checks": [dict(item) for item in self.checks],
            "files": list(self.files),
        }


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:300]}


def extract_code(reply: str) -> str:
    """Return the first fenced block, trimmed; empty when there is none."""
    match = _FENCE_RE.search(reply or "")
    if not match:
        # Some models answer with a bare file. Accept it only when it clearly is
        # Python source, so ordinary prose is never written to disk.
        text = (reply or "").strip()
        if text.startswith(("def ", "import ", '"""', "from ")) and "\n" in text:
            return text
        return ""
    return match.group(1).strip("\n")


def _make_workspace(root: Optional[str]) -> Path:
    base = Path(root) if root else giscode_home() / "onboarding" / "workspaces"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    workspace = base / f"first-task-{stamp}-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(parents=True, exist_ok=False)
    return workspace


def _default_verify(workspace: Path, timeout: float) -> tuple[bool, str]:
    """Run the assertion in a separate interpreter, never through a shell."""
    script = (
        "import greeting, sys\n"
        f"value = greeting.greet('Ada')\n"
        f"assert value == {EXPECTED_VALUE!r}, f'unexpected value: {{value!r}}'\n"
        "print('FIRST_TASK_OK', value)\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=max(5.0, float(timeout)),
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"verification could not run: {type(exc).__name__}: {exc}"
    output = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    if completed.returncode == 0 and "FIRST_TASK_OK" in (completed.stdout or ""):
        return True, output.splitlines()[-1] if output else "assertion passed"
    return False, (output or f"exit code {completed.returncode}")[:400]


def run_first_task(
    provider,
    model: str,
    *,
    root: Optional[str] = None,
    chat: Optional[Callable[..., str]] = None,
    verify: Optional[Callable[[Path, float], tuple[bool, str]]] = None,
    timeout: float = 240.0,
    verify_timeout: float = 60.0,
) -> FirstTaskResult:
    """Ask the local model for one fix, apply it in a sandbox, and verify it."""
    name = (model or "").strip()
    if not name:
        return FirstTaskResult(
            ok=False,
            detail="No model is configured, so the first coding task cannot run.",
            checks=(_check("model_selected", False, "no model"),),
        )

    try:
        workspace = _make_workspace(root)
    except OSError as exc:
        return FirstTaskResult(
            ok=False,
            detail=f"The disposable workspace could not be created: {exc}",
            checks=(_check("workspace", False, str(exc)),),
        )

    target = workspace / FILENAME
    try:
        target.write_text(INITIAL_SOURCE, encoding="utf-8")
    except OSError as exc:
        return FirstTaskResult(
            ok=False,
            detail=f"The task file could not be written: {exc}",
            workspace=str(workspace),
            checks=(_check("workspace", False, str(exc)),),
        )

    call = chat or provider.chat
    started = time.monotonic()
    try:
        reply = call(
            name,
            [{"role": "user", "content": TASK_BRIEF}],
            max_tokens=512,
            temperature=0.0,
            timeout=timeout,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        return FirstTaskResult(
            ok=False,
            detail=f"The model did not answer the task: {type(exc).__name__}: {exc}",
            workspace=str(workspace),
            model=name,
            prompt=TASK_BRIEF,
            seconds=elapsed,
            checks=(
                _check("workspace", True, str(workspace)),
                _check("answer", False, str(exc)),
            ),
            files=(str(target),),
        )
    elapsed = time.monotonic() - started

    text = str(reply or "")
    code = extract_code(text)
    checks: list[dict] = [
        _check("workspace", True, f"disposable workspace at {workspace}"),
        _check("answer", bool(text.strip()), f"{len(text)} characters returned"),
        _check(
            "code_block",
            bool(code),
            "a fenced code block was returned" if code else "the reply had no code block",
        ),
    ]
    if not code:
        return FirstTaskResult(
            ok=False,
            detail=(
                "The model's reply contained no code block, so nothing was changed. "
                "Retry the task; a guessed edit would be worse than none."
            ),
            workspace=str(workspace),
            model=name,
            prompt=TASK_BRIEF,
            reply_preview=text,
            seconds=elapsed,
            checks=tuple(checks),
            files=(str(target),),
        )
    if len(code) > _MAX_REPLY_CHARS:
        checks.append(_check("bounded_output", False, f"{len(code)} characters exceeds the cap"))
        return FirstTaskResult(
            ok=False,
            detail="The suggested file was larger than the task allows, so it was not applied.",
            workspace=str(workspace),
            model=name,
            prompt=TASK_BRIEF,
            reply_preview=text,
            seconds=elapsed,
            checks=tuple(checks),
            files=(str(target),),
        )
    checks.append(_check("bounded_output", True, f"{len(code)} characters"))

    try:
        target.write_text(code.rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        return FirstTaskResult(
            ok=False,
            detail=f"The suggested fix could not be written: {exc}",
            workspace=str(workspace),
            model=name,
            prompt=TASK_BRIEF,
            reply_preview=text,
            seconds=elapsed,
            checks=tuple(checks),
            files=(str(target),),
        )

    diff = "".join(
        difflib.unified_diff(
            INITIAL_SOURCE.splitlines(keepends=True),
            code.rstrip().splitlines(keepends=True),
            fromfile="greeting.py (before)",
            tofile="greeting.py (after)",
        )
    )

    verifier = verify or _default_verify
    try:
        passed, verification_detail = verifier(workspace, verify_timeout)
    except Exception as exc:
        passed, verification_detail = False, f"verification raised {type(exc).__name__}: {exc}"
    checks.append(_check("verification", passed, verification_detail))

    detail = (
        f"The local model fixed {FILENAME} and the check passed: {EXPECTED_CALL} "
        f"returns {EXPECTED_VALUE!r}."
        if passed
        else f"The edit was applied but the check failed: {verification_detail}"
    )
    return FirstTaskResult(
        ok=bool(passed),
        detail=detail,
        workspace=str(workspace),
        model=name,
        prompt=TASK_BRIEF,
        reply_preview=text,
        diff=diff,
        seconds=elapsed,
        checks=tuple(checks),
        files=(str(target),),
    )
