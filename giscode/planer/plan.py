"""Helpers to produce/validate a JSON-serializable plan and to
convert it to / from :attr:`giscode.projects.Project.plan`.

The plan dict lives at the top level of ``Project.plan`` under the
``"plan"`` key (so the existing :class:`Project` field is
unmodified). All operations are pure functions of the input — no
filesystem or DB access.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from giscode.planer.types import ProjectPlan


class PlanReadiness(str, Enum):
    INCOMPLETE = "incomplete"
    NEEDS_USER_INPUT = "needs_user_input"
    READY_FOR_REVIEW = "ready_for_review"


# Minimum required information to consider a plan "ready for review".
# The Planer can also add stricter checks via additional metadata,
# but these are the floor the engine itself enforces.
_MIN_REQUIRED_FEATURES = 1
_MIN_REQUIRED_SCREENS = 1
_MIN_ROADMAP_ITEMS = 1


def plan_to_project_dict(plan: ProjectPlan) -> dict:
    """Return the dict that should be stored in
    :attr:`giscode.projects.Project.plan`."""
    return {"plan": plan.to_dict()}


def plan_from_project_dict(data: Optional[dict]) -> ProjectPlan:
    """Reverse of :func:`plan_to_project_dict`. Accepts both the
    ``{"plan": {...}}`` envelope and a bare plan dict for
    convenience."""
    if not isinstance(data, dict):
        return ProjectPlan()
    if "plan" in data and isinstance(data["plan"], dict):
        return ProjectPlan.from_dict(data["plan"])
    return ProjectPlan.from_dict(data)


def is_plan_ready_for_review(plan: ProjectPlan) -> bool:
    """The Planer uses this to decide whether it can mark the
    state as ``REVIEW_READY``. Strict — refuses to be ready if any
    of the floor requirements are missing."""
    if not plan.understanding.original_idea and not plan.understanding.refined_description:
        return False
    if len(plan.requirements.features) < _MIN_REQUIRED_FEATURES:
        return False
    if not plan.design.application_type and not plan.design.major_components:
        return False
    if len(plan.screens) < _MIN_REQUIRED_SCREENS:
        return False
    if len(plan.roadmap) < _MIN_ROADMAP_ITEMS:
        return False
    if any(not q.answered for q in plan.open_questions):
        return False
    return True


def readiness_for(plan: ProjectPlan) -> PlanReadiness:
    if is_plan_ready_for_review(plan):
        return PlanReadiness.READY_FOR_REVIEW
    if any(not q.answered for q in plan.open_questions):
        return PlanReadiness.NEEDS_USER_INPUT
    return PlanReadiness.INCOMPLETE
