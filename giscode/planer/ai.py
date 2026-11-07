"""AI-backed Planer layer.

The :class:`PlanerAI` class wraps the deterministic
:class:`PlanerEngine` and adds LLM-driven planning assistance via
the **existing** :class:`giscode.inference.api.InferenceAPI`
``chat_generate`` endpoint (the same /v1/chat/completions path
introduced in Task 1). It does not introduce a second inference
system, a second model runtime, or a second chat-storage
mechanism.

Design:

* The system prompt (see :mod:`giscode.planer.prompts`) tells the
  model to respond with a single JSON object following a strict
  contract.
* :func:`parse_planer_json` is a tolerant parser: it locates the
  first JSON object in the model's text (even if wrapped in
  prose/markdown) and discards everything else. Unknown fields are
  ignored. Malformed JSON returns ``None``.
* On parse failure or empty response, the engine state is left
  **untouched** and the caller gets a :class:`PlanerAIResult` with
  ``success=False`` and a clear error message.
* The parsed JSON is applied to the deterministic engine through
  its existing public API (``add_feature``, ``add_screen``,
  ``set_branding``, etc.). No new code-execution path is opened.
* :class:`PlanerAI` is fully usable with a fake inference backend;
  the test suite does not depend on a real model.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

from giscode.inference.runtime import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    InferenceError,
)
from giscode.planer.engine import PlanerEngine, PlanerPhase
from giscode.planer.prompts import (
    PLANER_SYSTEM_PROMPT,
    build_planning_user_prompt,
    plan_to_summary,
)
from giscode.planer.research import OfflineResearchProvider, ResearchProvider
from giscode.planer.types import (
    PlanerQuestion,
    _new_id,
    _utc_now_iso,
)
from giscode.vision.images import (
    ImageDescriptionError,
    ImageLoadError,
    ImageStage,
    build_attachment_dicts,
    describe_images,
    format_observation,
    load_images,
)


# A list of JSON-object-looking patterns the parser will hunt for.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


# ----------------------------------------------------------------- inference protocol


class _InferenceChatLike(Protocol):
    """Minimal contract :class:`PlanerAI` needs from an inference
    backend. The real :class:`giscode.inference.api.InferenceAPI`
    satisfies this; tests can use a fake."""

    def chat_generate(self, request: ChatRequest) -> ChatResponse:  # pragma: no cover - protocol
        ...


# ----------------------------------------------------------------- result


@dataclass
class PlanerAIResult:
    success: bool
    assistant_message: str = ""
    parse_error: str = ""
    inference_error: str = ""
    raw_response: str = ""
    applied: dict = field(default_factory=dict)
    questions_asked: list[dict] = field(default_factory=list)
    plan_changed: bool = False
    # How far an attached image actually got, as an ``ImageStage`` value.
    # ``image_status`` is "interpreted" only when a real vision model
    # described the real picture; see :mod:`giscode.vision.images`.
    image_status: str = ImageStage.NONE.value
    image_report: str = ""
    image_error: str = ""
    image_model: str = ""
    images: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "assistant_message": self.assistant_message,
            "parse_error": self.parse_error,
            "inference_error": self.inference_error,
            "raw_response": self.raw_response,
            "applied": dict(self.applied),
            "questions_asked": list(self.questions_asked),
            "plan_changed": self.plan_changed,
            "image_status": self.image_status,
            "image_report": self.image_report,
            "image_error": self.image_error,
            "image_model": self.image_model,
            "images": [dict(image) for image in self.images],
        }


# ----------------------------------------------------------------- parser


def _extract_json_object(text: str) -> Optional[str]:
    """Find the first balanced ``{...}`` block in ``text``."""
    if not text:
        return None
    # First try a strict whole-string parse.
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    # Otherwise, scan for the first '{' and try to walk a balanced
    # object from there. This is intentionally simple: it looks for
    # the matching '}' at depth 0.
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def parse_planer_json(text: str) -> Optional[dict]:
    """Parse the model's text into a dict, or return ``None``.

    The parser is intentionally tolerant: it ignores surrounding
    prose, markdown fences, and unknown fields. It returns ``None``
    only if no JSON object can be located or the located object
    cannot be parsed.
    """
    if not text:
        return None
    candidate = _extract_json_object(text)
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


# ----------------------------------------------------------------- safe applier


_KNOWN_TOP_KEYS = frozenset({
    "assistant_message",
    "updated_understanding",
    "questions",
    "answers_consumed",
    "features",
    "functional_requirements",
    "non_functional_requirements",
    "design",
    "screens",
    "branding",
    "roadmap",
    "assumptions",
    "research_requests",
    "ready_for_review",
})


def _filter_known(data: dict) -> dict:
    """Drop any top-level key not in the known contract. Defense in
    depth against prompt-injection that tries to smuggle in
    `agent`, `approve`, `start_building`, etc."""
    return {k: v for k, v in data.items() if k in _KNOWN_TOP_KEYS}


def _coerce_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_list_of_str(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and str(v).strip()]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(value)]


def _coerce_priority(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s if s in ("low", "medium", "high") else "medium"


def _attachment_paths(attachments: Optional[Iterable[Any]]) -> list[str]:
    """Normalise attachments to a list of image paths.

    Accepts plain path strings, :class:`os.PathLike`, or any object
    exposing a ``path`` attribute (including
    :class:`giscode.gisai.types.Attachment`). ``None`` and empty inputs
    yield an empty list, which keeps the text-only call path identical
    to before.
    """
    if not attachments:
        return []
    if isinstance(attachments, (str, bytes)) or hasattr(attachments, "__fspath__"):
        attachments = [attachments]

    paths: list[str] = []
    for item in attachments:
        if item is None:
            continue
        if isinstance(item, (str, bytes)):
            text = os.fsdecode(item)
        elif hasattr(item, "__fspath__"):
            text = os.fspath(item)
        else:
            raw = getattr(item, "path", None)
            if raw is None:
                raise TypeError(
                    f"Attachment {item!r} has no 'path' attribute and is not a path."
                )
            text = os.fspath(raw) if hasattr(raw, "__fspath__") else str(raw)
        text = str(text).strip()
        if text:
            paths.append(text)
    return paths


# ----------------------------------------------------------------- AI layer


class PlanerAI:
    """AI-assisted planning over the existing :class:`PlanerEngine`.

    The class is intentionally small. It holds:

    * a reference to the deterministic :class:`PlanerEngine`,
    * an inference backend that satisfies the chat contract,
    * an optional :class:`ResearchProvider`,
    * a short in-memory conversation transcript (not persisted —
      the future GUI will persist via :class:`ChatStore`).

    Every call to :meth:`chat` is independent: a fresh
    :class:`ChatRequest` is built from the current engine state, so
    the model always sees the most up-to-date plan.
    """

    def __init__(
        self,
        engine: PlanerEngine,
        inference: Optional[_InferenceChatLike] = None,
        research_provider: Optional[ResearchProvider] = None,
        max_tokens: int = 800,
        temperature: float = 0.4,
        max_questions: int = 4,
    ) -> None:
        self._engine = engine
        # If the caller did not configure a research provider on the
        # engine, give the engine a reference to ours so
        # ``request_research`` actually consults it.
        if research_provider is not None:
            self._engine._research = research_provider  # noqa: SLF001 — internal, scoped to package
        self._inference = inference
        self._research = research_provider or OfflineResearchProvider()
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_questions = max_questions
        self._transcript: list[dict] = []

    # ------------------------------------------------------------------ props

    @property
    def engine(self) -> PlanerEngine:
        return self._engine

    @property
    def transcript(self) -> list[dict]:
        return list(self._transcript)

    # ------------------------------------------------------------------ chat

    def chat(
        self,
        user_message: str,
        attachments: Optional[Iterable[Any]] = None,
        vision_service: Optional[Any] = None,
    ) -> PlanerAIResult:
        """Send the user's message through the model and apply the
        structured update to the engine.

        * On any failure (no inference, model error, empty response,
          malformed JSON), the engine state is left unchanged and the
          returned :class:`PlanerAIResult` has ``success=False``.
        * On success, the engine has been updated and the result
          describes what was applied.

        ``attachments`` are image paths (or objects with a ``path``
        attribute). When present they are really read and really
        described through ``vision_service`` before the planner is
        called, and the resulting observation is placed in the planner
        prompt. If the image cannot be read or described, the call
        fails honestly with ``success=False`` and a populated
        ``image_error``; the planner is never asked to answer as though
        it had seen a picture it did not see.
        """
        if user_message is None:
            user_message = ""
        user_message = str(user_message)
        if not user_message.strip():
            return PlanerAIResult(
                success=False,
                parse_error="empty user message",
                raw_response="",
            )

        image_paths = _attachment_paths(attachments)

        # Real image reading happens before any planner prompt is built.
        observation = ""
        image_meta: list[dict] = []
        image_error = ""
        image_model = ""
        image_stage = ImageStage.NONE.value
        if image_paths:
            image_stage = ImageStage.ATTACHED.value
            try:
                loaded = load_images(image_paths)
                image_meta = build_attachment_dicts(loaded)
                image_stage = ImageStage.LOADED.value
            except ImageLoadError as exc:
                image_stage = ImageStage.FAILED.value
                image_error = str(exc)
                return PlanerAIResult(
                    success=False,
                    parse_error=image_error,
                    image_status=image_stage,
                    image_error=image_error,
                )

            if vision_service is None:
                image_stage = ImageStage.UNAVAILABLE.value
                image_error = (
                    "An image is attached but no vision service is available, so its "
                    "contents are unknown. The image was NOT described and was not "
                    "guessed at. Start a local vision-capable model and try again."
                )
                return PlanerAIResult(
                    success=False,
                    parse_error=image_error,
                    image_status=image_stage,
                    image_error=image_error,
                    images=image_meta,
                )

            try:
                description = describe_images(
                    image_paths,
                    vision_service,
                    request_id=f"planer-{len(self._transcript)}",
                )
            except ImageDescriptionError as exc:
                image_stage = ImageStage.UNAVAILABLE.value
                image_error = str(exc)
                return PlanerAIResult(
                    success=False,
                    parse_error=image_error,
                    image_status=image_stage,
                    image_error=image_error,
                    images=image_meta,
                )
            except ImageLoadError as exc:
                image_stage = ImageStage.FAILED.value
                image_error = str(exc)
                return PlanerAIResult(
                    success=False,
                    parse_error=image_error,
                    image_status=image_stage,
                    image_error=image_error,
                    images=image_meta,
                )
            except Exception as exc:
                # An unexpected failure in the vision stack must still be
                # reported as an image failure, not allowed to look like a
                # successful text-only answer.
                image_stage = ImageStage.FAILED.value
                image_error = (
                    f"The image could not be read: {type(exc).__name__}: {exc}. "
                    "The image was NOT described and its contents were not guessed."
                )
                return PlanerAIResult(
                    success=False,
                    parse_error=image_error,
                    image_status=image_stage,
                    image_error=image_error,
                    images=image_meta,
                )

            if not description.interpreted:
                from giscode.vision.images import stage_for

                image_stage = stage_for(description).value
                image_error = (
                    description.error
                    or "The image could not be described by a local vision model."
                )
                return PlanerAIResult(
                    success=False,
                    parse_error=image_error,
                    image_status=image_stage,
                    image_error=image_error,
                    images=image_meta,
                )

            image_stage = ImageStage.INTERPRETED.value
            observation = format_observation(description)
            image_model = description.model_used or ""

        # Record the user turn in the local transcript.
        self._transcript.append({"role": "user", "content": user_message})

        # Always record the user message into the engine's history so
        # the deterministic plan (chat history) is updated even if
        # the model call fails.
        self._engine._append_history("user", user_message)  # noqa: SLF001 — internal but stable

        if self._inference is None:
            return PlanerAIResult(
                success=False,
                inference_error="no inference backend configured",
                raw_response="",
                image_status=image_stage,
                image_report=observation,
                images=image_meta,
            )

        messages = self._build_messages(user_message, image_observation=observation)
        try:
            response = self._inference.chat_generate(
                ChatRequest(
                    messages=messages,
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                )
            )
        except InferenceError as e:
            return PlanerAIResult(
                success=False,
                inference_error=f"{type(e).__name__}: {e}",
                raw_response="",
                image_status=image_stage,
                image_report=observation,
                images=image_meta,
            )
        except Exception as e:  # pragma: no cover — defensive
            return PlanerAIResult(
                success=False,
                inference_error=f"{type(e).__name__}: {e}",
                raw_response="",
                image_status=image_stage,
                image_report=observation,
                images=image_meta,
            )

        raw = (response.text or "").strip()
        if not raw:
            return PlanerAIResult(
                success=False,
                inference_error="model returned empty response",
                raw_response="",
                image_status=image_stage,
                image_report=observation,
                images=image_meta,
            )

        parsed = parse_planer_json(raw)
        if parsed is None:
            # Preserve the previous plan; surface the failure.
            return PlanerAIResult(
                success=False,
                parse_error="model response was not valid JSON or did not contain a JSON object",
                raw_response=raw,
                image_status=image_stage,
                image_report=observation,
                images=image_meta,
            )

        # Defense in depth: drop any unknown top-level key the model
        # might have invented.
        parsed = _filter_known(parsed)
        if not parsed:
            return PlanerAIResult(
                success=False,
                parse_error="model response contained no recognised keys",
                raw_response=raw,
                image_status=image_stage,
                image_report=observation,
                images=image_meta,
            )

        applied = self._apply_to_engine(parsed)
        assistant_message = _coerce_str(parsed.get("assistant_message", ""))
        if assistant_message:
            self._transcript.append({"role": "assistant", "content": assistant_message})
            self._engine._append_history("assistant", assistant_message)  # noqa: SLF001

        result = PlanerAIResult(
            success=True,
            assistant_message=assistant_message,
            raw_response=raw,
            applied=applied,
            questions_asked=applied.get("questions", []),
            plan_changed=bool(applied.get("plan_changed", False)),
            image_status=image_stage,
            image_report=observation,
            image_error=image_error,
            image_model=image_model,
            images=image_meta,
        )

        # If the model said the plan is ready, attempt to mark REVIEW_READY.
        if parsed.get("ready_for_review") is True:
            self._engine.mark_review_ready()
        return result

    # ------------------------------------------------------------------ helpers

    def _build_messages(
        self,
        user_message: str,
        image_observation: str = "",
    ) -> list[ChatMessage]:
        summary = plan_to_summary(self._engine.plan)
        open_questions = [
            {"id": q.id, "category": q.category, "text": q.text, "answered": q.answered}
            for q in self._engine.plan.open_questions
        ]
        # Keep the recent transcript short to stay within context.
        recent = self._transcript[-6:]
        user_payload = build_planning_user_prompt(
            user_message=user_message,
            plan_summary=summary,
            open_questions=open_questions,
            recent_turns=recent,
        )
        if image_observation:
            # Placed after the user message so the planner reads the
            # observed image content as part of the request.
            user_payload = f"{user_payload}\n\n{image_observation}"
        return [
            ChatMessage(role="system", content=PLANER_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_payload),
        ]

    def _apply_to_engine(self, parsed: dict) -> dict:
        """Apply the parsed JSON to the deterministic engine. Every
        call below is a typed, bounded method on :class:`PlanerEngine`
        — model output is never executed, only mapped to known
        operations."""
        applied: dict = {"plan_changed": False}

        # updated_understanding → only set fields that are present and non-empty.
        u = parsed.get("updated_understanding") or {}
        if isinstance(u, dict):
            if u.get("refined_description"):
                self._engine.plan.understanding.refined_description = _coerce_str(u["refined_description"])
                applied["plan_changed"] = True
            for piece in _coerce_list_of_str(u.get("goals")):
                if piece not in self._engine.plan.understanding.goals:
                    self._engine.plan.understanding.goals.append(piece)
                    applied["plan_changed"] = True
            for piece in _coerce_list_of_str(u.get("target_users")):
                if piece not in self._engine.plan.understanding.target_users:
                    self._engine.plan.understanding.target_users.append(piece)
                    applied["plan_changed"] = True
            for piece in _coerce_list_of_str(u.get("assumptions")):
                if piece not in self._engine.plan.understanding.assumptions:
                    self._engine.plan.understanding.assumptions.append(piece)
                    applied["plan_changed"] = True
            for piece in _coerce_list_of_str(u.get("constraints")):
                if piece not in self._engine.plan.understanding.constraints:
                    self._engine.plan.understanding.constraints.append(piece)
                    applied["plan_changed"] = True

        # answers_consumed → fold into engine (mark questions answered).
        for ans in parsed.get("answers_consumed") or []:
            if not isinstance(ans, dict):
                continue
            cat = _coerce_str(ans.get("category"))
            text = _coerce_str(ans.get("text"))
            if not cat or not text:
                continue
            # Find the first open question in that category and answer it.
            for q in self._engine.plan.open_questions:
                if not q.answered and q.category == cat:
                    self._engine.answer_question(q.id, text)
                    applied["plan_changed"] = True
                    break

        # questions → add new questions (capped).
        added_qs: list[dict] = []
        for q in parsed.get("questions") or []:
            if not isinstance(q, dict):
                continue
            text = _coerce_str(q.get("text"))
            if not text:
                continue
            if any(existing.text == text for existing in self._engine.plan.open_questions):
                continue
            if len(added_qs) >= self._max_questions:
                break
            new_q = PlanerQuestion(
                id=_new_id("q"),
                category=_coerce_str(q.get("category"), "general"),
                text=text,
                rationale=_coerce_str(q.get("rationale")),
            )
            self._engine.plan.open_questions.append(new_q)
            added_qs.append({"id": new_q.id, "category": new_q.category, "text": new_q.text})
        if added_qs:
            applied["questions"] = added_qs
            applied["plan_changed"] = True
            self._engine._state.phase = PlanerPhase.QUESTIONS  # noqa: SLF001
            self._engine._state.updated_at = _utc_now_iso()  # noqa: SLF001

        # features
        added_feats = 0
        for f in parsed.get("features") or []:
            if not isinstance(f, dict):
                continue
            name = _coerce_str(f.get("name"))
            if not name:
                continue
            if any(existing.name == name for existing in self._engine.plan.requirements.features):
                continue
            self._engine.add_feature(
                name=name,
                description=_coerce_str(f.get("description")),
                priority=_coerce_priority(f.get("priority")),
                rationale=_coerce_str(f.get("rationale")),
            )
            added_feats += 1
        if added_feats:
            applied["features_added"] = added_feats
            applied["plan_changed"] = True

        # requirements
        for line in _coerce_list_of_str(parsed.get("functional_requirements")):
            if line and line not in self._engine.plan.requirements.functional:
                self._engine.plan.requirements.functional.append(line)
                applied["plan_changed"] = True
        for line in _coerce_list_of_str(parsed.get("non_functional_requirements")):
            if line and line not in self._engine.plan.requirements.non_functional:
                self._engine.plan.requirements.non_functional.append(line)
                applied["plan_changed"] = True

        # design
        d = parsed.get("design") or {}
        if isinstance(d, dict):
            if d.get("application_type"):
                self._engine.set_application_type(_coerce_str(d["application_type"]))
                applied["plan_changed"] = True
            for piece in _coerce_list_of_str(d.get("major_components")):
                self._engine.add_component(piece)
                applied["plan_changed"] = True
            if d.get("architecture_notes"):
                self._engine.plan.design.architecture_notes = _coerce_str(d["architecture_notes"])
                applied["plan_changed"] = True
            for piece in _coerce_list_of_str(d.get("integrations")):
                if piece not in self._engine.plan.design.integrations:
                    self._engine.plan.design.integrations.append(piece)
                    applied["plan_changed"] = True
            for piece in _coerce_list_of_str(d.get("dependencies")):
                if piece not in self._engine.plan.design.dependencies:
                    self._engine.plan.design.dependencies.append(piece)
                    applied["plan_changed"] = True

        # screens
        for s in parsed.get("screens") or []:
            if not isinstance(s, dict):
                continue
            name = _coerce_str(s.get("name"))
            if not name:
                continue
            if any(existing.name == name for existing in self._engine.plan.screens):
                continue
            self._engine.add_screen(
                name=name,
                purpose=_coerce_str(s.get("purpose")),
                major_elements=_coerce_list_of_str(s.get("major_elements")),
                navigation_to=_coerce_list_of_str(s.get("navigation_to")),
            )
            applied["plan_changed"] = True

        # branding (replace, not merge — but only fields explicitly supplied)
        b = parsed.get("branding") or {}
        if isinstance(b, dict) and b:
            self._engine.set_branding(
                name=_coerce_str(b.get("name")),
                tagline=_coerce_str(b.get("tagline")),
                logo_concept=_coerce_str(b.get("logo_concept")),
                visual_direction=_coerce_str(b.get("visual_direction")),
                typography_notes=_coerce_str(b.get("typography_notes")),
                color_notes=_coerce_str(b.get("color_notes")),
            )
            applied["plan_changed"] = True

        # roadmap
        for r in parsed.get("roadmap") or []:
            if not isinstance(r, dict):
                continue
            title = _coerce_str(r.get("title"))
            if not title:
                continue
            if any(existing.title == title for existing in self._engine.plan.roadmap):
                continue
            self._engine.add_roadmap_item(
                title=title,
                description=_coerce_str(r.get("description")),
                priority=_coerce_priority(r.get("priority")),
                dependencies=_coerce_list_of_str(r.get("dependencies")),
            )
            applied["plan_changed"] = True

        # assumptions
        for line in _coerce_list_of_str(parsed.get("assumptions")):
            if line:
                self._engine.add_assumption(line)
                applied["plan_changed"] = True

        # research_requests → only if provider is available
        research_done: list[dict] = []
        for r in parsed.get("research_requests") or []:
            if not isinstance(r, dict):
                continue
            query = _coerce_str(r.get("query"))
            if not query:
                continue
            ref = self._engine.request_research(query, purpose=_coerce_str(r.get("purpose")))
            if ref is not None:
                research_done.append({"query": ref.query, "summary": ref.summary})
        if research_done:
            applied["research"] = research_done
            applied["plan_changed"] = True

        return applied


__all__ = [
    "PlanerAI",
    "PlanerAIResult",
    "parse_planer_json",
]
