"""Typed planning data model for the Planer.

All structures are JSON-serializable so they can be stored verbatim
in :attr:`giscode.projects.Project.plan` and reloaded without loss.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time()%1)*1000):03d}Z"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class PlanerPhase(str, Enum):
    """Conceptual phases the Planer walks through. These are **not**
    project-lifecycle states — they live entirely inside the Planer
    and never mutate :class:`giscode.projects.ProjectLifecycle`."""
    INTAKE = "intake"
    UNDERSTANDING = "understanding"
    QUESTIONS = "questions"
    RESEARCH = "research"
    REQUIREMENTS = "requirements"
    DESIGN = "design"
    SCREENS = "screens"
    BRANDING = "branding"
    ROADMAP = "roadmap"
    REVIEW_READY = "review_ready"


# ------------------------------------------------------------------ understanding

@dataclass
class ProjectUnderstanding:
    original_idea: str = ""
    refined_description: str = ""
    goals: list[str] = field(default_factory=list)
    target_users: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ProjectUnderstanding":
        if not data:
            return cls()
        return cls(
            original_idea=str(data.get("original_idea", "") or ""),
            refined_description=str(data.get("refined_description", "") or ""),
            goals=list(data.get("goals", []) or []),
            target_users=list(data.get("target_users", []) or []),
            assumptions=list(data.get("assumptions", []) or []),
            constraints=list(data.get("constraints", []) or []),
            open_questions=list(data.get("open_questions", []) or []),
        )


@dataclass
class PlanerQuestion:
    id: str
    text: str
    rationale: str = ""
    category: str = "general"
    answer: str = ""
    answered: bool = False
    created_at: str = field(default_factory=_utc_now_iso)
    answered_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlanerQuestion":
        return cls(
            id=str(data.get("id") or _new_id("q")),
            text=str(data.get("text", "")),
            rationale=str(data.get("rationale", "")),
            category=str(data.get("category", "general")),
            answer=str(data.get("answer", "")),
            answered=bool(data.get("answered", False)),
            created_at=str(data.get("created_at", _utc_now_iso())),
            answered_at=data.get("answered_at"),
        )


# ------------------------------------------------------------------ requirements

@dataclass
class Feature:
    id: str
    name: str
    description: str = ""
    priority: str = "medium"  # low | medium | high
    rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Feature":
        return cls(
            id=str(data.get("id") or _new_id("feat")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            priority=str(data.get("priority", "medium")),
            rationale=str(data.get("rationale", "")),
        )


@dataclass
class ProjectRequirements:
    functional: list[str] = field(default_factory=list)
    non_functional: list[str] = field(default_factory=list)
    features: list[Feature] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "functional": list(self.functional),
            "non_functional": list(self.non_functional),
            "features": [f.to_dict() for f in self.features],
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ProjectRequirements":
        if not data:
            return cls()
        feats = [Feature.from_dict(f) for f in (data.get("features", []) or [])]
        return cls(
            functional=list(data.get("functional", []) or []),
            non_functional=list(data.get("non_functional", []) or []),
            features=feats,
        )


# ------------------------------------------------------------------ design

@dataclass
class ProjectDesign:
    application_type: str = ""
    major_components: list[str] = field(default_factory=list)
    architecture_notes: str = ""
    integrations: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ProjectDesign":
        if not data:
            return cls()
        return cls(
            application_type=str(data.get("application_type", "") or ""),
            major_components=list(data.get("major_components", []) or []),
            architecture_notes=str(data.get("architecture_notes", "") or ""),
            integrations=list(data.get("integrations", []) or []),
            dependencies=list(data.get("dependencies", []) or []),
        )


# ------------------------------------------------------------------ screens

@dataclass
class Screen:
    id: str
    name: str
    purpose: str = ""
    major_elements: list[str] = field(default_factory=list)
    navigation_to: list[str] = field(default_factory=list)  # ids of other screens

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Screen":
        return cls(
            id=str(data.get("id") or _new_id("screen")),
            name=str(data.get("name", "")),
            purpose=str(data.get("purpose", "")),
            major_elements=list(data.get("major_elements", []) or []),
            navigation_to=list(data.get("navigation_to", []) or []),
        )


# ------------------------------------------------------------------ branding

@dataclass
class BrandingConcept:
    name: str = ""
    tagline: str = ""
    logo_concept: str = ""
    visual_direction: str = ""
    typography_notes: str = ""
    color_notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "BrandingConcept":
        if not data:
            return cls()
        return cls(
            name=str(data.get("name", "") or ""),
            tagline=str(data.get("tagline", "") or ""),
            logo_concept=str(data.get("logo_concept", "") or ""),
            visual_direction=str(data.get("visual_direction", "") or ""),
            typography_notes=str(data.get("typography_notes", "") or ""),
            color_notes=str(data.get("color_notes", "") or ""),
        )


# ------------------------------------------------------------------ roadmap

@dataclass
class RoadmapItem:
    id: str
    title: str
    description: str = ""
    priority: str = "medium"  # low | medium | high
    dependencies: list[str] = field(default_factory=list)  # ids of other items
    status: str = "planned"  # planned | in_progress | done

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RoadmapItem":
        return cls(
            id=str(data.get("id") or _new_id("task")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            priority=str(data.get("priority", "medium")),
            dependencies=list(data.get("dependencies", []) or []),
            status=str(data.get("status", "planned")),
        )


# ------------------------------------------------------------------ research refs

@dataclass
class ResearchResultRef:
    """Lightweight, JSON-serializable reference to a research
    finding. We deliberately do **not** store raw external content
    here — only metadata — so the plan is auditable and reproducible.

    Planer-consumed research is **distinguishable** from
    user-supplied information: every entry has a ``source`` of
    ``"research"``.
    """
    query: str
    summary: str
    sources: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    is_current: bool = False
    retrieved_at: str = field(default_factory=_utc_now_iso)
    purpose: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ResearchResultRef":
        return cls(
            query=str(data.get("query", "")),
            summary=str(data.get("summary", "")),
            sources=list(data.get("sources", []) or []),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            is_current=bool(data.get("is_current", False)),
            retrieved_at=str(data.get("retrieved_at", _utc_now_iso())),
            purpose=str(data.get("purpose", "") or ""),
        )


# ------------------------------------------------------------------ plan

@dataclass
class ProjectPlan:
    """The structured artifact the Planer produces.

    It is fully JSON-serializable so it can round-trip through
    :attr:`giscode.projects.Project.plan` (a ``dict``)."""
    schema_version: int = 1
    project_id: str = ""
    understanding: ProjectUnderstanding = field(default_factory=ProjectUnderstanding)
    requirements: ProjectRequirements = field(default_factory=ProjectRequirements)
    design: ProjectDesign = field(default_factory=ProjectDesign)
    screens: list[Screen] = field(default_factory=list)
    branding: BrandingConcept = field(default_factory=BrandingConcept)
    roadmap: list[RoadmapItem] = field(default_factory=list)
    research_log: list[ResearchResultRef] = field(default_factory=list)
    open_questions: list[PlanerQuestion] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "understanding": self.understanding.to_dict(),
            "requirements": self.requirements.to_dict(),
            "design": self.design.to_dict(),
            "screens": [s.to_dict() for s in self.screens],
            "branding": self.branding.to_dict(),
            "roadmap": [r.to_dict() for r in self.roadmap],
            "research_log": [r.to_dict() for r in self.research_log],
            "open_questions": [q.to_dict() for q in self.open_questions],
            "assumptions": list(self.assumptions),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectPlan":
        if not isinstance(data, dict):
            return cls()
        return cls(
            schema_version=int(data.get("schema_version", 1) or 1),
            project_id=str(data.get("project_id", "") or ""),
            understanding=ProjectUnderstanding.from_dict(data.get("understanding")),
            requirements=ProjectRequirements.from_dict(data.get("requirements")),
            design=ProjectDesign.from_dict(data.get("design")),
            screens=[Screen.from_dict(s) for s in (data.get("screens", []) or [])],
            branding=BrandingConcept.from_dict(data.get("branding")),
            roadmap=[RoadmapItem.from_dict(r) for r in (data.get("roadmap", []) or [])],
            research_log=[ResearchResultRef.from_dict(r) for r in (data.get("research_log", []) or [])],
            open_questions=[PlanerQuestion.from_dict(q) for q in (data.get("open_questions", []) or [])],
            assumptions=list(data.get("assumptions", []) or []),
            metadata=dict(data.get("metadata", {}) or {}),
        )


# ------------------------------------------------------------------ state

@dataclass
class PlanerState:
    """In-memory state the Planer engine maintains across turns."""
    project_id: str = ""
    phase: PlanerPhase = PlanerPhase.INTAKE
    plan: ProjectPlan = field(default_factory=ProjectPlan)
    history: list[dict] = field(default_factory=list)  # {role, content, ts}
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "phase": self.phase.value,
            "plan": self.plan.to_dict(),
            "history": list(self.history),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanerState":
        if not isinstance(data, dict):
            return cls()
        try:
            phase = PlanerPhase(data.get("phase", PlanerPhase.INTAKE.value))
        except ValueError:
            phase = PlanerPhase.INTAKE
        return cls(
            project_id=str(data.get("project_id", "") or ""),
            phase=phase,
            plan=ProjectPlan.from_dict(data.get("plan", {}) or {}),
            history=list(data.get("history", []) or []),
            created_at=str(data.get("created_at", _utc_now_iso())),
            updated_at=str(data.get("updated_at", _utc_now_iso())),
        )
