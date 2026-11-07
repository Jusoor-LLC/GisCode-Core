"""Planer — the project-understanding and planning agent.

The Planer's job is **only** to:

* understand the user's idea
* ask focused questions
* optionally perform controlled research
* produce a structured project plan

The Planer must **never**:

* approve a project
* start building
* invoke the existing :mod:`giscode.agentic` coding agent
* execute project code
* run tests
* deploy anything

The lifecycle transitions for ``REVIEW → APPROVED → BUILDING`` remain
in the hands of an explicit user action through the future Project
Workspace GUI. The Planer only *prepares* a plan and signals that it
is ready for the user's review.

Package layout:
    * :mod:`.types`     — typed planning data model
    * :mod:`.research`  — thin controlled-research abstraction
    * :mod:`.engine`    — stateful Planer engine
    * :mod:`.plan`      — helpers to produce/validate a Project.plan dict
"""

from giscode.planer.ai import PlanerAI, PlanerAIResult, parse_planer_json
from giscode.planer.chat import (
    PLANER_CHAT_TITLE,
    PersistedTurn,
    PlanerChatPersistence,
    PlanerChatPersistenceError,
)
from giscode.planer.engine import (
    PlanerEngine,
    PlanerPhase,
    engine_does_not_reference_coding_agent,
)
from giscode.planer.plan import (
    PlanReadiness,
    plan_to_project_dict,
    plan_from_project_dict,
    is_plan_ready_for_review,
    readiness_for,
)
from giscode.planer.research import (
    OfflineResearchProvider,
    ResearchProvider,
    ResearchResultRef,
)
from giscode.planer.types import (
    BrandingConcept,
    Feature,
    PlanerQuestion,
    PlanerState,
    ProjectDesign,
    ProjectPlan,
    ProjectRequirements,
    ProjectUnderstanding,
    RoadmapItem,
    Screen,
)

__all__ = [
    "BrandingConcept",
    "Feature",
    "OfflineResearchProvider",
    "PersistedTurn",
    "PlanReadiness",
    "PlanerAI",
    "PlanerAIResult",
    "PlanerChatPersistence",
    "PlanerChatPersistenceError",
    "PlanerEngine",
    "PlanerPhase",
    "PlanerQuestion",
    "PlanerState",
    "ProjectDesign",
    "ProjectPlan",
    "ProjectRequirements",
    "ProjectUnderstanding",
    "ResearchProvider",
    "ResearchResultRef",
    "RoadmapItem",
    "Screen",
    "engine_does_not_reference_coding_agent",
    "is_plan_ready_for_review",
    "parse_planer_json",
    "plan_from_project_dict",
    "plan_to_project_dict",
    "readiness_for",
]
