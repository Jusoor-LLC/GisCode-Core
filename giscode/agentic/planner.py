"""Planer for the coding agent.

This module provides :class:`Planner` (the production
LLM-backed planner), :class:`MockPlanner` (a deterministic
test-only convenience), and a small structured-output parser
(:class:`PlanningOutputParser`) used to convert the model's
JSON response into :class:`ExecutionPlan` /
:func:`giscode.agentic.types.ExecutionStep` objects.

Design contract
---------------

* The **production** planner is :class:`Planner`, which uses a
  real :class:`giscode.inference.api.InferenceAPI` to ask the
  configured LLM for a structured execution plan.
* The planner asks the model to return **JSON** describing the
  steps to perform. JSON is wrapped in a fenced code block by
  the model in many cases; the parser handles both fenced and
  unfenced JSON.
* The parser validates the structure (required keys, allowed
  action types, file-path safety) and produces a
  :class:`ExecutionPlan`. Malformed / unsafe responses raise
  :class:`PlanningError`, which the loop translates into a
  :attr:`TaskResult.status` of ``FAILED`` with a clear
  ``error`` field.
* The planner never executes actions, never opens files, and
  never bypasses :class:`WorkspaceActionExecutor`. It only
  produces a plan.
* :class:`MockPlanner` is preserved as a deterministic,
  test-only convenience. It is **not** wired into production.

Allowed action types
--------------------

The planner only emits actions that the existing
:class:`WorkspaceActionExecutor` understands:

* ``"create"`` — write a new file (the executor will reject if
  the file already exists).
* ``"modify"`` — overwrite an existing file.
* ``"read"``   — read a file's content (does not change
  state).

Any other action type is rejected by the parser.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional

from giscode.agentic.types import (
    ActionType,
    ExecutionPlan,
    ExecutionStep,
    TaskRequest,
)
from giscode.inference.api import InferenceAPI
from giscode.inference.runtime import ChatMessage, ChatRequest, ChatResponse


# ----------------------------------------------------------------- errors


class PlanningError(Exception):
    """Raised when the planner cannot produce a valid
    :class:`ExecutionPlan`.

    Subclasses / uses include:

    * the inference backend is unavailable;
    * the LLM returned an empty / unparseable response;
    * the LLM response failed validation;
    * the LLM emitted a path that escapes the workspace.
    """


# ----------------------------------------------------------------- parser


# The action type strings the model is allowed to emit, mapped
# to the enum the executor understands. Anything else is rejected.
_ALLOWED_ACTION_TYPES: dict[str, ActionType] = {
    "create": ActionType.CREATE,
    "modify": ActionType.MODIFY,
    "read": ActionType.READ,
}

# Reserved keys the parser accepts. Anything else in the top-
# level JSON object is ignored (defence in depth against the
# model trying to smuggle in extra commands).
_RESERVED_TOP_KEYS = {
    "objective",
    "assumptions",
    "files_expected_to_change",
    "steps",
    "verification_steps",
    "explanation",
}

_RESERVED_STEP_KEYS = {
    "step_id",
    "description",
    "action_type",
    "target_path",
    "content",
    "expected_result",
}


@dataclass
class PlanningOutputParser:
    """Robust parser for the LLM's JSON plan response.

    The parser is intentionally strict about the schema it
    accepts. Anything malformed is reported as a
    :class:`PlanningError` with a clear, actionable message.
    """

    workspace_root: Optional[Any] = None
    """Optional resolved :class:`pathlib.Path` of the workspace
    root, used to reject paths that would escape the workspace.
    When ``None`` the parser still rejects absolute paths and
    ``..`` traversal but does not enforce containment.
    """

    max_steps: int = 64
    """Safety cap on the number of accepted steps. Prevents a
    pathological LLM response from generating thousands of
    actions in a single plan."""

    # ----------------------------------------------------------------- API

    def parse(self, raw_response: str) -> ExecutionPlan:
        """Parse ``raw_response`` (the model's text output) and
        return a fully-validated :class:`ExecutionPlan`.

        Raises :class:`PlanningError` on any failure.
        """
        if raw_response is None:
            raise PlanningError("planner received empty response from model")
        text = raw_response.strip()
        if not text:
            raise PlanningError("planner received empty response from model")

        data = self._extract_json_object(text)
        if not isinstance(data, dict):
            raise PlanningError(
                "planner response was not a JSON object "
                f"(got {type(data).__name__})"
            )

        # Drop anything we don't recognise (defence in depth).
        clean = {k: v for k, v in data.items() if k in _RESERVED_TOP_KEYS}
        if "steps" not in clean:
            raise PlanningError(
                "planner response is missing the required 'steps' key"
            )
        if not isinstance(clean["steps"], list):
            raise PlanningError(
                "planner 'steps' must be a list "
                f"(got {type(clean['steps']).__name__})"
            )
        if len(clean["steps"]) > self.max_steps:
            raise PlanningError(
                f"planner response has {len(clean['steps'])} steps, "
                f"exceeding the safety cap of {self.max_steps}"
            )

        steps = [self._parse_step(i, s) for i, s in enumerate(clean["steps"])]
        # Drop empty steps that were placeholders / comments
        # (we still require at least one valid step, but allow
        # the model to emit explanatory text without it being
        # a step).
        steps = [s for s in steps if s is not None]
        # Actually: we have already filtered out non-dicts. If
        # the model emitted a list with only placeholders, we
        # still need at least one actionable step.
        if not steps:
            raise PlanningError(
                "planner response contained no actionable steps"
            )

        files_expected = self._parse_string_list(
            clean.get("files_expected_to_change", []),
            key="files_expected_to_change",
        )
        verification_steps = self._parse_string_list(
            clean.get("verification_steps", []),
            key="verification_steps",
        )
        assumptions = self._parse_string_list(
            clean.get("assumptions", []), key="assumptions"
        )

        objective = clean.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            # Fall back to the original request description; the
            # caller already knows it, but we want a non-empty
            # objective on the plan.
            objective = ""

        return ExecutionPlan(
            objective=objective.strip(),
            assumptions=assumptions,
            files_expected_to_change=files_expected,
            steps=steps,
            verification_steps=verification_steps
            or [
                "Verify created/modified files exist",
                "Verify Python files can be parsed",
            ],
        )

    # ----------------------------------------------------------------- internals

    def _extract_json_object(self, text: str) -> Any:
        """Find a JSON object in ``text`` and return it parsed.

        Handles both unfenced JSON and JSON wrapped in
        ``\\`\\`\\`json ... \\`\\`\\`` fences. Falls back to
        scanning for the first balanced ``{...}`` object.
        """
        # 1. Try fenced ```json ... ``` (case-insensitive on the
        # language tag).
        fence = re.search(
            r"```(?:json|JSON)?\s*(\{.*?\})\s*```", text, re.DOTALL
        )
        if fence:
            try:
                return json.loads(fence.group(1))
            except json.JSONDecodeError:
                pass  # fall through

        # 2. Try fenced ``` ... ``` without explicit language.
        fence2 = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence2:
            try:
                return json.loads(fence2.group(1))
            except json.JSONDecodeError:
                pass

        # 3. Try the whole text as JSON.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 4. Scan for the first balanced {...} block. This is a
        # best-effort fallback for models that wrap the JSON in
        # prose without fences.
        return self._first_balanced_json_object(text)

    def _first_balanced_json_object(self, text: str) -> Any:
        depth = 0
        start = -1
        in_string = False
        escape = False
        for i, ch in enumerate(text):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        candidate = text[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            start = -1
                            continue
        raise PlanningError(
            "planner response did not contain a parseable JSON object"
        )

    def _parse_step(self, index: int, raw: Any) -> Optional[ExecutionStep]:
        if not isinstance(raw, dict):
            # Allow plain-string placeholder comments in the
            # step list; the model occasionally emits them.
            if isinstance(raw, str):
                return None
            raise PlanningError(
                f"step[{index}] is not an object (got {type(raw).__name__})"
            )

        # Drop unknown keys; keep only recognised ones.
        clean = {k: v for k, v in raw.items() if k in _RESERVED_STEP_KEYS}
        if not clean:
            return None

        action_raw = clean.get("action_type")
        if not isinstance(action_raw, str):
            raise PlanningError(
                f"step[{index}] is missing a string 'action_type'"
            )
        key = action_raw.strip().lower()
        if key not in _ALLOWED_ACTION_TYPES:
            raise PlanningError(
                f"step[{index}] has unsupported action_type "
                f"{action_raw!r}; allowed: "
                f"{sorted(_ALLOWED_ACTION_TYPES)}"
            )
        action_type = _ALLOWED_ACTION_TYPES[key]

        target_path = clean.get("target_path")
        if not isinstance(target_path, str) or not target_path.strip():
            raise PlanningError(
                f"step[{index}] is missing a non-empty 'target_path'"
            )
        target_path = target_path.strip()
        # Absolute paths that live inside the authorized project root
        # are normalized to project-relative paths before validation
        # (the executor/verifier already accept absolute paths inside
        # the root). Anything else absolute — no known root, or outside
        # the root — is still rejected. Parent traversal stays rejected.
        if target_path.startswith(("/", "\\")) or (
            len(target_path) >= 2 and target_path[1] == ":"
        ):
            if self.workspace_root is None:
                raise PlanningError(
                    f"step[{index}] target_path {target_path!r} is absolute"
                )
            try:
                from pathlib import Path
                root = Path(self.workspace_root).resolve()
                rel = Path(target_path).resolve().relative_to(root)
            except (ValueError, OSError):
                raise PlanningError(
                    f"step[{index}] target_path {target_path!r} is absolute "
                    "and escapes the workspace"
                )
            target_path = rel.as_posix()
        if ".." in target_path.replace("\\", "/").split("/"):
            raise PlanningError(
                f"step[{index}] target_path {target_path!r} uses "
                "parent traversal"
            )
        # If we know the workspace root, ensure the resolved
        # path stays inside it.
        if self.workspace_root is not None:
            try:
                from pathlib import Path
                resolved = (Path(self.workspace_root) / target_path).resolve()
                resolved.relative_to(Path(self.workspace_root).resolve())
            except ValueError as exc:
                raise PlanningError(
                    f"step[{index}] target_path {target_path!r} escapes "
                    f"the workspace: {exc}"
                ) from exc
            except Exception:
                pass  # best-effort containment check

        # A ``modify`` step whose target is not on disk is a fabricated path:
        # a small model invents a plausible package layout (for a Kotlin
        # project, ``src/commonMain/kotlin/com/example/...``). Reject it here,
        # before anything executes, naming real files to use instead. Letting
        # it through only produced "File does not exist" after the model call,
        # which failed the whole task with nothing to correct against.
        if action_type == ActionType.MODIFY and self.workspace_root is not None:
            try:
                from pathlib import Path

                exists = (Path(self.workspace_root) / target_path).is_file()
            except Exception:  # noqa: BLE001 - cannot tell: keep old behaviour
                exists = True
            if not exists:
                candidates = _existing_candidates(self.workspace_root)
                raise PlanningError(
                    f"step[{index}] target_path {target_path!r} does not exist "
                    f"in the workspace"
                    + (f"; real source files: {candidates}" if candidates else "")
                )

        description = clean.get("description")
        if not isinstance(description, str) or not description.strip():
            description = f"{action_type.value} {target_path}"

        content = clean.get("content")
        if content is not None and not isinstance(content, str):
            raise PlanningError(
                f"step[{index}] 'content' must be a string when present"
            )

        expected = clean.get("expected_result")
        if expected is not None and not isinstance(expected, str):
            raise PlanningError(
                f"step[{index}] 'expected_result' must be a string when present"
            )

        step_id = clean.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            step_id = f"step-{index + 1}-{uuid.uuid4().hex[:6]}"

        return ExecutionStep(
            step_id=step_id,
            description=description.strip(),
            action_type=action_type,
            target_path=target_path,
            content=content,
            expected_result=expected,
        )

    def _parse_string_list(self, value: Any, *, key: str) -> List[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise PlanningError(
                f"planner '{key}' must be a list (got {type(value).__name__})"
            )
        out: List[str] = []
        for item in value:
            if not isinstance(item, str):
                raise PlanningError(
                    f"planner '{key}' entries must be strings "
                    f"(got {type(item).__name__})"
                )
            if item.strip():
                out.append(item.strip())
        return out


# ----------------------------------------------------------------- planner


# Output budget for one planning reply. The previous default (1500 tokens)
# truncated a plan mid-string on a small model, which then surfaced as an
# unparseable planner response rather than as a budget problem. The value is
# clamped so a configuration mistake cannot ask for an unbounded completion.
DEFAULT_PLAN_MAX_TOKENS = 4096
MAX_PLAN_MAX_TOKENS = 16384


def _clamp_max_tokens(value: Any) -> int:
    """Validate a configured output budget; unusable values fall back safely."""
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PLAN_MAX_TOKENS
    if tokens <= 0:
        return DEFAULT_PLAN_MAX_TOKENS
    return min(tokens, MAX_PLAN_MAX_TOKENS)


# System prompt that instructs the LLM to produce a structured
# JSON plan. Kept deliberately compact and strict to maximise
# the chance of a parseable response from small models.
_PLANNER_SYSTEM_PROMPT = (
    "You are a coding-agent planner. Convert the user's request "
    "into a structured execution plan.\n\n"
    "Respond with a single JSON object (optionally inside a "
    "```json ... ``` code fence) with exactly this schema:\n"
    "{\n"
    '  "objective": "<one short sentence>",\n'
    '  "assumptions": ["..."],\n'
    '  "files_expected_to_change": ["path/in/workspace", ...],\n'
    '  "steps": [\n'
    "    {\n"
    '      "step_id": "step-1",\n'
    '      "description": "<short>",\n'
    '      "action_type": "create" | "modify" | "read",\n'
    '      "target_path": "<relative path inside the workspace>",\n'
    '      "content": "<full file content; required for create/modify>",\n'
    '      "expected_result": "<optional short>"\n'
    "    }, ...\n"
    "  ],\n"
    '  "verification_steps": ["..."]\n'
    "}\n\n"
    "Rules:\n"
    "- Use action_type \"create\" for new files, \"modify\" for "
    "existing files, \"read\" to inspect.\n"
    "- target_path MUST be a relative path inside the workspace "
    "(no leading /, no .., no drive letters).\n"
    "- For \"create\" / \"modify\", include the full intended file "
    "content in the \"content\" field.\n"
    "- target_path must come from the project source files listed "
    "in the request. Use \"modify\"/\"read\" only for a path that is "
    "listed, and \"create\" only for a new file directly inside a "
    "listed directory. Never invent a package layout of your own.\n"
    "- Do not include any prose outside the JSON. Do not include "
    "shell commands, only file actions."
)


def _source_block(workspace_root: Optional[Any]) -> str:
    """A bounded list of the real source paths, or ``""`` when unavailable."""
    if not workspace_root:
        return ""
    try:
        from giscode.agentic.repo_inventory import render_source_block

        return render_source_block(workspace_root)
    except Exception:  # noqa: BLE001 - the prompt must never fail
        return ""


def _existing_candidates(workspace_root: Any, limit: int = 5) -> str:
    """A short list of real source paths, for a rejection message."""
    try:
        from giscode.agentic.repo_inventory import list_source_files

        return ", ".join(list_source_files(workspace_root, limit=limit))
    except Exception:  # noqa: BLE001 - message decoration only
        return ""


def _user_prompt_for(
    request: TaskRequest, workspace_root: Optional[Any] = None
) -> str:
    parts: List[str] = [f"User request:\n{request.description.strip()}"]
    if request.completion_criteria:
        try:
            parts.append(
                "Completion criteria:\n"
                + json.dumps(request.completion_criteria, indent=2)
            )
        except TypeError:
            parts.append(f"Completion criteria: {request.completion_criteria!r}")
    # The model used to receive the request alone, so it invented a plausible
    # package layout (``src/commonMain/kotlin/com/example/...``) and the plan
    # died on "File does not exist". Real paths are listed here instead.
    source_block = _source_block(workspace_root)
    if source_block:
        parts.append(source_block)
    parts.append(
        "Return the JSON plan only. Use action_type values "
        '"create", "modify", or "read". '
        "Include the complete file content in the "
        '"content" field of every create/modify step.'
    )
    return "\n\n".join(parts)


class Planner:
    """Production LLM-backed planner.

    The planner sends a structured prompt to the supplied
    :class:`InferenceAPI`, parses the JSON response into an
    :class:`ExecutionPlan`, and returns it. On any failure
    (inference unavailable, empty response, malformed JSON,
    unsafe path) it raises :class:`PlanningError`.

    The class name :class:`Planner` is preserved so that
    existing call sites that pass ``planner=Planner(...)`` keep
    working.

    Parameters
    ----------
    inference:
        The :class:`InferenceAPI` to use. Required.
    workspace_root:
        Optional resolved :class:`pathlib.Path` used to enforce
        workspace containment on every emitted path. When
        ``None`` the parser still rejects absolute paths and
        ``..`` traversal.
    max_tokens, temperature, max_steps:
        Inference / parser tuning knobs. ``max_tokens`` defaults
        to 4096, which fits a 5-8 step plan written by a small model.
    """

    def __init__(
        self,
        inference: Optional[InferenceAPI] = None,
        *,
        workspace_root: Optional[Any] = None,
        max_tokens: int = DEFAULT_PLAN_MAX_TOKENS,
        temperature: float = 0.2,
        max_steps: int = 64,
        reasoning_budget_fn: Optional[Callable[[], str]] = None,
    ) -> None:
        # ``inference`` is required for the LLM-backed planner in
        # production. It is allowed to be ``None`` so that
        # existing test subclasses (e.g. ``SlowPlanner`` in
        # tests/test_agentic.py) can call ``super().__init__()``
        # without arguments. The production wiring in
        # ``CodingAgentLoop`` always passes a real inference.
        self._inference = inference
        self._workspace_root = workspace_root
        self._max_tokens = _clamp_max_tokens(max_tokens)
        self._temperature = temperature
        self._max_steps = max_steps
        self._reasoning_budget_fn = reasoning_budget_fn

    def _reasoning_budget(self) -> str:
        """Thinking mode for planning calls.

        Defaults to ``"off"``: reasoning models (e.g. Qwen3) otherwise spend the
        whole token budget thinking and return empty content, which the planner
        reports as "empty response from the model".
        """
        if self._reasoning_budget_fn is None:
            return "off"
        try:
            value = str(self._reasoning_budget_fn() or "off").strip().lower()
        except Exception:
            return "off"
        return value if value in ("off", "low", "default") else "off"

    # ----------------------------------------------------------------- API

    def create_plan(self, request: TaskRequest) -> ExecutionPlan:
        if self._inference is None:
            raise PlanningError(
                "Planner requires an InferenceAPI; "
                "no inference backend was provided"
            )
        try:
            health = self._inference.get_health()
        except Exception as exc:  # pragma: no cover - defensive
            raise PlanningError(
                f"inference health check failed: {type(exc).__name__}: {exc}"
            ) from exc
        if not health.get("model_loaded"):
            raise PlanningError(
                "cannot produce an execution plan: the inference "
                "backend has no model loaded "
                f"(health={health!r})"
            )

        messages = [
            ChatMessage(role="system", content=_PLANNER_SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=_user_prompt_for(request, self._workspace_root),
            ),
        ]
        chat_req = ChatRequest(
            messages=messages,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            reasoning_budget=self._reasoning_budget(),
        )
        try:
            response: ChatResponse = self._inference.chat_generate(chat_req)
        except Exception as exc:
            raise PlanningError(
                f"planner inference call failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        text = (response.text or "").strip()
        if not text:
            raise PlanningError(
                "planner received an empty response from the model"
            )

        parser = PlanningOutputParser(
            workspace_root=self._workspace_root,
            max_steps=self._max_steps,
        )
        try:
            return parser.parse(text)
        except PlanningError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise PlanningError(
                f"planner parser crashed: {type(exc).__name__}: {exc}"
            ) from exc


class MockPlanner(Planner):
    """Deterministic planner used **only** by tests.

    Returns a hardcoded "create src/example.py" plan. Preserved
    so existing tests can exercise the loop with a known-good
    plan without requiring a model.

    This class is *not* wired into the production
    :class:`CodingAgentLoop` default; production uses
    :class:`Planner` (the LLM-backed planner).
    """

    def __init__(self, sleep_seconds: float = 0.0) -> None:
        # The base class requires an InferenceAPI; we override
        # ``create_plan`` to never use it. We pass a placeholder
        # inference that is never called.
        from giscode.inference.mocks import MockLlamaCppRuntime
        placeholder = InferenceAPI(runtime=MockLlamaCppRuntime(available=False))
        super().__init__(inference=placeholder)
        self._sleep_seconds = sleep_seconds

    def create_plan(self, request: TaskRequest) -> ExecutionPlan:
        steps = [
            ExecutionStep(
                step_id="step-1",
                description="Create example file",
                action_type=ActionType.CREATE,
                target_path="src/example.py",
                content="# example\n",
                expected_result="File created successfully",
            )
        ]
        if self._sleep_seconds > 0:
            steps.append(
                ExecutionStep(
                    step_id="step-delay",
                    description="Delay step for timeout testing",
                    action_type=ActionType.READ,
                    target_path="src/example.py",
                )
            )
        return ExecutionPlan(
            objective=f"Mock plan for: {request.description}",
            assumptions=["Mock assumption"],
            files_expected_to_change=["src/example.py"],
            steps=steps,
            verification_steps=["Verify src/example.py exists"],
        )
