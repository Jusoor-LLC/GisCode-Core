from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from giscode.task_router.types import (
    Attachment,
    CapabilityType,
    IntentCategory,
    TaskIntent,
)


VISION_TRIGGERS = [
    r"analyze this (image|screenshot|photo|screen)",
    r"what is (wrong|broken|the issue) with this (ui|screen|interface|screenshot|image|photo)",
    r"look at this (ui|screen|interface|screenshot|image|photo|error)",
    r"inspect this (diagram|image|screenshot|mockup|design)",
    r"read this (screenshot|image|photo|error|text)",
    r"describe this (image|screenshot|photo|screen)",
    r"what do you see in this (image|screenshot|photo)",
    r"check this (ui|screen|interface|screenshot|image)",
    r"review this (design|mockup|screenshot|image)",
    r"compare these (screens|screenshots|images)",
]

CODING_TRIGGERS = [
    r"implement (this|the)",
    r"write (code|a function|a class|a component)",
    r"create (a|the)",
    r"fix (this|the|it)",
    r"refactor",
    r"add (a|the)",
    r"update (the|this)",
    r"modify (the|this)",
    r"debug",
    r"solve",
]

PLANNING_TRIGGERS = [
    r"plan (this|the)",
    r"how (to|would you|should I)",
    r"design (a|the)",
    r"architecture",
    r"break down",
    r"steps to",
]

PROJECT_INSPECTION_TRIGGERS = [
    r"inspect (my|the) project",
    r"explore (my|the) project",
    r"what (files|modules|components) (are|exist)",
    r"show me (the|my) project",
    r"list (files|modules)",
]

VISION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in VISION_TRIGGERS]
CODING_PATTERNS = [re.compile(p, re.IGNORECASE) for p in CODING_TRIGGERS]
PLANNING_PATTERNS = [re.compile(p, re.IGNORECASE) for p in PLANNING_TRIGGERS]
PROJECT_INSPECTION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in PROJECT_INSPECTION_TRIGGERS]


def detect_intent(text: str, has_image: bool = False, project_active: bool = False) -> TaskIntent:
    text_lower = text.lower()
    scores = {}

    # Vision analysis
    vision_score = 0
    for pattern in VISION_PATTERNS:
        if pattern.search(text):
            vision_score += 2
    if has_image:
        vision_score += 3
    if "vision" in text_lower or "screenshot" in text_lower or "screencast" in text_lower:
        vision_score += 1
    scores[IntentCategory.VISION_ANALYSIS] = vision_score

    # UI analysis (subset of vision with UI focus)
    ui_score = 0
    if any(w in text_lower for w in ["ui", "interface", "screen", "button", "layout", "design", "mockup", "css", "style"]):
        ui_score += 2
    if has_image:
        ui_score += 2
    scores[IntentCategory.UI_ANALYSIS] = ui_score

    # Coding
    coding_score = 0
    for pattern in CODING_PATTERNS:
        if pattern.search(text):
            coding_score += 1
    if "code" in text_lower or "implement" in text_lower or "function" in text_lower or "class" in text_lower:
        coding_score += 1
    scores[IntentCategory.CODE_GENERATION] = coding_score

    # Debugging
    debug_score = 0
    if any(w in text_lower for w in ["error", "bug", "fail", "crash", "exception", "traceback", "log", "issue", "problem", "wrong", "broken"]):
        debug_score += 2
    if has_image and any(w in text_lower for w in ["error", "bug", "fail", "crash"]):
        debug_score += 2
    scores[IntentCategory.DEBUGGING] = debug_score

    # Planning
    planning_score = 0
    for pattern in PLANNING_PATTERNS:
        if pattern.search(text):
            planning_score += 1
    scores[IntentCategory.TASK_PLANNING] = planning_score

    # Project inspection
    inspect_score = 0
    for pattern in PROJECT_INSPECTION_PATTERNS:
        if pattern.search(text):
            inspect_score += 1
    if project_active and any(w in text_lower for w in ["project", "codebase", "repository"]):
        inspect_score += 1
    scores[IntentCategory.PROJECT_INSPECTION] = inspect_score

    # General chat fallback
    scores[IntentCategory.GENERAL_CHAT] = 1

    # Select highest scoring intent
    best = max(scores.items(), key=lambda x: x[1])
    category, score = best
    confidence = min(1.0, score / 5.0)

    # Determine required capabilities
    required = []
    suggested_plan = []

    if category in (IntentCategory.VISION_ANALYSIS, IntentCategory.UI_ANALYSIS):
        if has_image:
            required.append(CapabilityType.VISION)
            suggested_plan.append("Load vision model")
            suggested_plan.append("Analyze image")
        else:
            suggested_plan.append("Wait for image attachment")
        if category == IntentCategory.UI_ANALYSIS:
            required.append(CapabilityType.CODING)
            suggested_plan.append("Identify corresponding code")
    elif category == IntentCategory.DEBUGGING:
        if has_image:
            required.append(CapabilityType.VISION)
            suggested_plan.append("Load vision model")
            suggested_plan.append("Analyze error screenshot")
        required.append(CapabilityType.CODING)
        if project_active:
            required.append(CapabilityType.PROJECT_INSPECTION)
            suggested_plan.append("Inspect project for related code")
        suggested_plan.append("Propose fix")
    elif category == IntentCategory.CODE_GENERATION:
        required.append(CapabilityType.CODING)
        if has_image:
            required.append(CapabilityType.VISION)
            suggested_plan.append("Analyze design screenshot")
        if project_active:
            required.append(CapabilityType.PROJECT_INSPECTION)
            suggested_plan.append("Inspect project structure")
        suggested_plan.append("Generate code")
        suggested_plan.append("Write tests")
    elif category == IntentCategory.TASK_PLANNING:
        required.append(CapabilityType.GENERAL_REASONING)
        if project_active:
            required.append(CapabilityType.PROJECT_INSPECTION)
            suggested_plan.append("Inspect project")
        suggested_plan.append("Create plan")
    elif category == IntentCategory.PROJECT_INSPECTION:
        required.append(CapabilityType.PROJECT_INSPECTION)
        suggested_plan.append("List project files/modules")
    else:
        required.append(CapabilityType.GENERAL_REASONING)
        suggested_plan.append("General chat response")

    return TaskIntent(
        category=category,
        confidence=confidence,
        raw_text=text,
        required_capabilities=required,
        suggested_plan=suggested_plan,
    )


def needs_image_for_intent(intent: TaskIntent) -> bool:
    return intent.category in (
        IntentCategory.VISION_ANALYSIS,
        IntentCategory.UI_ANALYSIS,
        IntentCategory.DEBUGGING,
    ) and not any("vision" in c.value for c in intent.required_capabilities)