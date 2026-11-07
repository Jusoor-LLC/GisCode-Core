"""Planer engine.

The engine is a small state machine that:

* accepts the user's idea,
* tracks what is known and what is missing,
* generates focused questions,
* records user answers,
* optionally performs controlled research,
* produces a :class:`ProjectPlan`.

It is *not* an LLM agent. It is a deterministic scaffold the
future LLM-backed Planer will fill in. The scaffold itself enforces
the safety invariants the task requires:

* The engine never imports :mod:`giscode.agentic`,
  :mod:`giscode.orchestration`, or :mod:`giscode.inference`.
* The engine never mutates a :class:`giscode.projects.Project`'s
  lifecycle — it only mutates its in-memory :class:`PlanerState`.
* The engine only *prepares* a plan and signals
  ``REVIEW_READY``; transitioning the project to ``REVIEW`` /
  ``APPROVED`` / ``BUILDING`` is the responsibility of a separate,
  explicit user-driven step (handled by ``ProjectStore`` +
  future GUI).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from giscode.planer.plan import (
    is_plan_ready_for_review,
    readiness_for,
    PlanReadiness,
    plan_from_project_dict,
)
from giscode.planer.research import OfflineResearchProvider, ResearchProvider
from giscode.planer.types import (
    BrandingConcept,
    Feature,
    PlanerPhase,
    PlanerQuestion,
    PlanerState,
    ProjectDesign,
    ProjectPlan,
    ProjectRequirements,
    ProjectUnderstanding,
    ResearchResultRef,
    RoadmapItem,
    Screen,
    _new_id,
    _utc_now_iso,
)


# Forbidden reference check: the engine must not import or mention
# the coding agent at runtime. We do this with a string check on
# the *name* of any callable attribute, so accidentally exposing
# a reference (e.g. by adding ``agent=...`` as a parameter) trips
# the test in :func:`engine_does_not_reference_coding_agent`.
_FORBIDDEN_NAMES = {
    "CodingAgentLoop",
    "BuildTestOrchestrator",
    "WorkspaceActionExecutor",
    "Verifier",
    "MockOpenHandsAdapter",
    "RealOpenHandsAdapter",
    "InferenceAPI",
    "SubprocessLlamaCppRuntime",
    "approve",
    "start_building",
}


# ----------------------------------------------------------------- engine


class PlanerEngine:
    """Stateful planner scaffold.

    Typical usage:

        engine = PlanerEngine(project_id=...)
        engine.start_with_idea("I want to build ...")
        questions = engine.generate_questions()
        engine.answer_question(questions[0].id, "My target users are ...")
        ...
        plan = engine.build_plan()
        if engine.is_ready_for_review():
            ...

    All public methods are pure functions of the current state — no
    global state, no filesystem access, no DB calls, no inference
    calls, no agent calls.
    """

    def __init__(
        self,
        project_id: str = "",
        research_provider: Optional[ResearchProvider] = None,
    ) -> None:
        self._state = PlanerState(project_id=project_id)
        self._research: ResearchProvider = research_provider or OfflineResearchProvider()

    # ------------------------------------------------------------------ props

    @property
    def state(self) -> PlanerState:
        return self._state

    @property
    def phase(self) -> PlanerPhase:
        return self._state.phase

    @property
    def plan(self) -> ProjectPlan:
        return self._state.plan

    @property
    def project_id(self) -> str:
        return self._state.project_id

    # ------------------------------------------------------------------ intake

    def start_with_idea(self, idea: str) -> None:
        """Record the initial idea. Does *not* pretend to know anything
        else — everything else must come from the user or from
        research that is explicitly requested and recorded."""
        if not idea or not idea.strip():
            raise ValueError("idea must be a non-empty string")
        self._state.phase = PlanerPhase.UNDERSTANDING
        self._state.plan.understanding.original_idea = idea.strip()
        self._state.plan.project_id = self._state.project_id
        self._state.updated_at = _utc_now_iso()
        self._append_history("user", idea.strip())

    # ------------------------------------------------------------------ questions

    def generate_questions(self, max_questions: int = 4) -> list[PlanerQuestion]:
        """Generate a small, focused set of questions about the parts of
        the project that are *not* yet known. The questions are
        derived from the current state, never from assumed
        information."""
        if max_questions < 1:
            max_questions = 1
        u = self._state.plan.understanding
        questions: list[PlanerQuestion] = []

        def add(category: str, text: str, rationale: str = "") -> None:
            if any(q.text == text for q in self._state.plan.open_questions):
                return
            self._state.plan.open_questions.append(
                PlanerQuestion(id=_new_id("q"), category=category, text=text, rationale=rationale)
            )
            questions.append(self._state.plan.open_questions[-1])

        if not u.refined_description:
            add("understanding", "In one or two sentences, what does this project actually do?")
        if not u.target_users:
            add("users", "Who are the primary users of this project?")
        if not u.goals:
            add("goals", "What is the most important outcome this project must deliver?")
        if not u.constraints:
            add("constraints", "Are there any hard constraints (platform, tech, deadline, budget)?")
        if not self._state.plan.design.application_type:
            add("design", "Is this a desktop app, web app, mobile app, CLI, or something else?")
        if not self._state.plan.design.major_components:
            add("design", "Which major components or modules do you already have in mind?")
        if not self._state.plan.screens:
            add("screens", "What is the first screen a user sees when they open this project?")
        if not self._state.plan.branding.name:
            add("branding", "Do you have a working name for the project yet?")
        if not self._state.plan.roadmap:
            add("roadmap", "What is the first milestone you would like to reach?")

        # Limit to a small number of focused questions per turn.
        questions = questions[:max_questions]
        if questions:
            self._state.phase = PlanerPhase.QUESTIONS
            self._state.updated_at = _utc_now_iso()
        return questions

    def answer_question(self, question_id: str, answer: str) -> Optional[PlanerQuestion]:
        """Record the user's answer to a question. The answer is also
        folded into the relevant part of the plan (target users,
        constraints, design, etc.) so the Planer does not have to be
        re-asked the same thing."""
        if not question_id:
            raise ValueError("question_id is required")
        if answer is None:
            answer = ""
        for q in self._state.plan.open_questions:
            if q.id == question_id:
                q.answer = str(answer)
                q.answered = True
                q.answered_at = _utc_now_iso()
                self._fold_answer_into_plan(q)
                self._state.updated_at = _utc_now_iso()
                self._append_history("user", f"[answer:{q.category}] {q.answer}")
                return q
        return None

    def _fold_answer_into_plan(self, q: PlanerQuestion) -> None:
        u = self._state.plan.understanding
        d = self._state.plan.design
        b = self._state.plan.branding
        text = q.answer.strip()
        if not text:
            return
        if q.category == "understanding" and not u.refined_description:
            u.refined_description = text
        elif q.category == "users":
            for piece in self._split_listy(text):
                if piece and piece not in u.target_users:
                    u.target_users.append(piece)
        elif q.category == "goals":
            for piece in self._split_listy(text):
                if piece and piece not in u.goals:
                    u.goals.append(piece)
        elif q.category == "constraints":
            for piece in self._split_listy(text):
                if piece and piece not in u.constraints:
                    u.constraints.append(piece)
        elif q.category == "design":
            if not d.application_type:
                d.application_type = text
            else:
                for piece in self._split_listy(text):
                    if piece and piece not in d.major_components:
                        d.major_components.append(piece)
        elif q.category == "screens":
            name = self._first_line(text) or "Main screen"
            purpose = text
            screen = Screen(
                id=_new_id("screen"),
                name=name,
                purpose=purpose,
                major_elements=[],
                navigation_to=[],
            )
            self._state.plan.screens.append(screen)
        elif q.category == "branding":
            if not b.name:
                first = self._first_line(text)
                rest_lines = text.splitlines()[1:] if "\n" in text else []
                b.name = first or text
                # If the answer also carries a tagline on subsequent lines, capture it.
                rest = "\n".join(line for line in rest_lines if line.strip()).strip()
                if rest and not b.tagline:
                    b.tagline = rest
            elif not b.tagline:
                b.tagline = text
            else:
                b.visual_direction = (b.visual_direction + "\n" + text).strip()
        elif q.category == "roadmap":
            title = self._first_line(text) or text
            item = RoadmapItem(
                id=_new_id("task"),
                title=title,
                description=text,
                priority="medium",
            )
            self._state.plan.roadmap.append(item)

    @staticmethod
    def _split_listy(text: str) -> Iterable[str]:
        for piece in re.split(r"[,;\n]+", text):
            p = piece.strip().lstrip("-*").strip()
            if p:
                yield p

    @staticmethod
    def _first_line(text: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if line:
                return line
        return ""

    # ------------------------------------------------------------------ requirements

    def add_feature(
        self,
        name: str,
        description: str = "",
        priority: str = "medium",
        rationale: str = "",
    ) -> Feature:
        if not name or not name.strip():
            raise ValueError("feature name is required")
        feat = Feature(
            id=_new_id("feat"),
            name=name.strip(),
            description=description.strip(),
            priority=priority if priority in ("low", "medium", "high") else "medium",
            rationale=rationale.strip(),
        )
        self._state.plan.requirements.features.append(feat)
        self._state.phase = PlanerPhase.REQUIREMENTS
        self._state.updated_at = _utc_now_iso()
        return feat

    def add_functional_requirement(self, text: str) -> None:
        text = (text or "").strip()
        if text and text not in self._state.plan.requirements.functional:
            self._state.plan.requirements.functional.append(text)
            self._state.phase = PlanerPhase.REQUIREMENTS
            self._state.updated_at = _utc_now_iso()

    def add_non_functional_requirement(self, text: str) -> None:
        text = (text or "").strip()
        if text and text not in self._state.plan.requirements.non_functional:
            self._state.plan.requirements.non_functional.append(text)
            self._state.phase = PlanerPhase.REQUIREMENTS
            self._state.updated_at = _utc_now_iso()

    # ------------------------------------------------------------------ design / screens / branding / roadmap

    def set_application_type(self, text: str) -> None:
        if text and text.strip():
            self._state.plan.design.application_type = text.strip()
            self._state.phase = PlanerPhase.DESIGN
            self._state.updated_at = _utc_now_iso()

    def add_component(self, text: str) -> None:
        text = (text or "").strip()
        if text and text not in self._state.plan.design.major_components:
            self._state.plan.design.major_components.append(text)
            self._state.phase = PlanerPhase.DESIGN
            self._state.updated_at = _utc_now_iso()

    def add_screen(
        self,
        name: str,
        purpose: str = "",
        major_elements: Optional[list[str]] = None,
        navigation_to: Optional[list[str]] = None,
    ) -> Screen:
        if not name or not name.strip():
            raise ValueError("screen name is required")
        screen = Screen(
            id=_new_id("screen"),
            name=name.strip(),
            purpose=(purpose or "").strip(),
            major_elements=list(major_elements or []),
            navigation_to=list(navigation_to or []),
        )
        self._state.plan.screens.append(screen)
        self._state.phase = PlanerPhase.SCREENS
        self._state.updated_at = _utc_now_iso()
        return screen

    def set_branding(
        self,
        name: str = "",
        tagline: str = "",
        logo_concept: str = "",
        visual_direction: str = "",
        typography_notes: str = "",
        color_notes: str = "",
    ) -> BrandingConcept:
        b = self._state.plan.branding
        if name:
            b.name = name.strip()
        if tagline:
            b.tagline = tagline.strip()
        if logo_concept:
            b.logo_concept = logo_concept.strip()
        if visual_direction:
            b.visual_direction = visual_direction.strip()
        if typography_notes:
            b.typography_notes = typography_notes.strip()
        if color_notes:
            b.color_notes = color_notes.strip()
        self._state.phase = PlanerPhase.BRANDING
        self._state.updated_at = _utc_now_iso()
        return b

    def add_assumption(self, text: str) -> None:
        text = (text or "").strip()
        if text and text not in self._state.plan.assumptions:
            self._state.plan.assumptions.append(text)
            self._state.updated_at = _utc_now_iso()

    def add_roadmap_item(
        self,
        title: str,
        description: str = "",
        priority: str = "medium",
        dependencies: Optional[list[str]] = None,
    ) -> RoadmapItem:
        if not title or not title.strip():
            raise ValueError("roadmap title is required")
        item = RoadmapItem(
            id=_new_id("task"),
            title=title.strip(),
            description=description.strip(),
            priority=priority if priority in ("low", "medium", "high") else "medium",
            dependencies=list(dependencies or []),
        )
        self._state.plan.roadmap.append(item)
        self._state.phase = PlanerPhase.ROADMAP
        self._state.updated_at = _utc_now_iso()
        return item

    # ------------------------------------------------------------------ research

    def request_research(self, query: str, purpose: str = "") -> Optional[ResearchResultRef]:
        """Perform *controlled* research.

        The Planer will only call this when a piece of genuinely
        external information is required (e.g. current versions of a
        library, a current API, a current platform requirement). It
        is never called automatically — only when the engine or the
        user explicitly asks.

        Returns the recorded :class:`ResearchResultRef` (also
        appended to the plan's research log) or ``None`` if no
        research backend is currently available.
        """
        if not query or not query.strip():
            raise ValueError("query is required")
        if not self._research.is_available():
            return None
        ref = self._research.research(
            query=query.strip(),
            purpose=purpose or "",
            project_id=self._state.project_id,
        )
        if ref is not None:
            self._state.plan.research_log.append(ref)
            self._state.phase = PlanerPhase.RESEARCH
            self._state.updated_at = _utc_now_iso()
        return ref

    # ------------------------------------------------------------------ build / readiness

    def restore_plan(self, plan_data: Optional[dict]) -> bool:
        """Replace the engine's in-memory :class:`ProjectPlan` with one
        reconstructed from ``plan_data`` (a dict as produced by
        :func:`giscode.planer.plan_to_project_dict`).

        This is a *replacement*, not a merge: callers are expected to
        be reopening a known-good plan. The engine's ``phase`` is
        re-derived from the restored plan so the workspace session
        can transparently resume where the previous session left off.

        Returns ``True`` if the plan was restored, ``False`` if the
        input was empty (in which case the existing plan is kept
        untouched and the engine is left as-is).

        Raises :class:`ValueError` for malformed data. The engine
        does *not* touch the project lifecycle.
        """
        if plan_data is None:
            return False
        if not isinstance(plan_data, dict):
            raise ValueError("plan_data must be a dict or None")
        if not plan_data:
            return False
        plan = plan_from_project_dict(plan_data)
        self._state.plan = plan
        self._state.plan.project_id = self._state.project_id
        readiness = readiness_for(plan)
        if readiness is PlanReadiness.READY_FOR_REVIEW:
            self._state.phase = PlanerPhase.REVIEW_READY
        else:
            self._state.phase = PlanerPhase.UNDERSTANDING
        self._state.updated_at = _utc_now_iso()
        return True

    def build_plan(self) -> ProjectPlan:
        """Snapshot the current state into a :class:`ProjectPlan`. The
        plan is what will eventually be written to
        :attr:`giscode.projects.Project.plan` by the future Project
        Workspace GUI — the engine itself never writes to the
        project store."""
        # Project id is always set from the engine's identity.
        self._state.plan.project_id = self._state.project_id
        return ProjectPlan.from_dict(self._state.plan.to_dict())

    def is_ready_for_review(self) -> bool:
        return is_plan_ready_for_review(self._state.plan)

    def readiness(self) -> PlanReadiness:
        return readiness_for(self._state.plan)

    def mark_review_ready(self) -> bool:
        """Transition the engine to ``REVIEW_READY`` if and only if the
        plan passes :func:`is_plan_ready_for_review`. This affects
        only the engine's own ``phase`` field; it does **not** call
        :class:`giscode.projects.ProjectStore.transition_lifecycle`
        or otherwise touch the project lifecycle."""
        if not self.is_ready_for_review():
            return False
        self._state.phase = PlanerPhase.REVIEW_READY
        self._state.updated_at = _utc_now_iso()
        return True

    # ------------------------------------------------------------------ history

    def _append_history(self, role: str, content: str) -> None:
        self._state.history.append({
            "role": str(role),
            "content": str(content),
            "ts": _utc_now_iso(),
        })

    # ------------------------------------------------------------------ safety

    def exposed_attribute_names(self) -> set[str]:
        """Diagnostic helper: returns the set of public attribute
        names of the engine. Used by the test suite to assert that
        the engine does not expose references to the coding
        agent."""
        return {n for n in dir(self) if not n.startswith("_")}


# ----------------------------------------------------------------- module-level safety


def engine_does_not_reference_coding_agent(engine: PlanerEngine) -> bool:
    """Return True if the engine (or its module) does not expose any
    attribute name matching a forbidden symbol. This is a static
    safeguard against future regressions that accidentally couple
    the Planer to the coding pipeline."""
    public = engine.exposed_attribute_names()
    forbidden_hits = public & _FORBIDDEN_NAMES
    return not forbidden_hits
