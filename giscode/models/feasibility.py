"""One feasibility answer per model, taken from the existing load planner.

``giscode.inference.resource_budget.plan_model_load`` already decides how a GGUF
can be loaded - every layer on the GPU, experts on the CPU, fewer layers, a
smaller context - and refuses with a plain reason. This module only *exposes*
that decision to selection (routing and startup autoload), so a model is never
offered as usable merely because its file exists.

No second estimator is introduced: every number in a verdict (``n_ctx``,
``gpu_layers``, the reason, the plan itself) is the planner's own output.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

OK = "ok"
MISSING = "missing"
INCOMPATIBLE = "incompatible"
INSUFFICIENT_MEMORY = "insufficient_memory"
RUNTIME_UNAVAILABLE = "runtime_unavailable"
UNVERIFIED = "unverified"

#: Short, user-facing label per category.
LABELS = {
    OK: "fits",
    MISSING: "model file not found",
    INCOMPATIBLE: "not loadable",
    INSUFFICIENT_MEMORY: "insufficient memory",
    RUNTIME_UNAVAILABLE: "no usable runtime",
    UNVERIFIED: "feasibility unverified",
}


@dataclass(frozen=True)
class Feasibility:
    """Whether one model can be loaded now, and why not when it cannot."""

    name: str
    path: str
    feasible: bool
    category: str
    reason: str
    n_ctx: int = 0
    gpu_layers: int = 0
    plan: Optional[dict] = None

    @property
    def label(self) -> str:
        return LABELS.get(self.category, self.category)

    @property
    def plan_reason(self) -> str:
        return str((self.plan or {}).get("reason") or "")

    def describe(self) -> str:
        """One plain sentence for a log line, note or refusal reason."""
        if self.feasible:
            detail = self.plan_reason or "the resource plan fits"
            return f"'{self.name}' can be loaded ({detail})"
        return f"'{self.name}' cannot be loaded: {self.label} - {self.reason}"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "feasible": self.feasible,
            "category": self.category,
            "label": self.label,
            "reason": self.reason,
            "n_ctx": self.n_ctx,
            "gpu_layers": self.gpu_layers,
            "plan_reason": self.plan_reason,
        }


def is_coding_capable(model) -> bool:
    """Coding capability as the project already records it (tags/capabilities).

    Deliberately not a model-name test of its own: discovery and the catalog
    are what attach ``coding``, and a candidate still has to pass feasibility
    before it is offered.
    """
    values = list(getattr(model, "capabilities", []) or []) + list(
        getattr(model, "tags", []) or []
    )
    return any(str(value).strip().lower() == "coding" for value in values)


def evaluate(inference, *, name: str, path: str, n_ctx: Optional[int] = None) -> Feasibility:
    """Ask the load planner whether ``name`` can be loaded from ``path`` now."""
    resolved = str(path or "")
    if not resolved or not Path(resolved).is_file():
        return Feasibility(
            name=name,
            path=resolved,
            feasible=False,
            category=MISSING,
            reason="the model file is not on disk",
        )

    available = True
    try:
        available = bool(inference.available)
    except Exception:
        available = True
    if not available:
        return Feasibility(
            name=name,
            path=resolved,
            feasible=False,
            category=RUNTIME_UNAVAILABLE,
            reason="no llama-server build or in-process runtime is available",
        )

    try:
        plan = inference.plan_load(resolved, n_ctx=n_ctx)
    except Exception as exc:
        # The planner could not read this file (truncated header, exotic
        # architecture). That is *not* a refusal: it proves nothing about
        # memory, and the loader has its own fallback for it. Call it
        # unverified and let the load path give the real answer.
        return Feasibility(
            name=name,
            path=resolved,
            feasible=True,
            category=UNVERIFIED,
            reason=f"the load planner could not evaluate this file ({type(exc).__name__}: {exc})",
        )

    fits = bool(plan.get("fits"))
    return Feasibility(
        name=name,
        path=resolved,
        feasible=fits,
        category=OK if fits else INSUFFICIENT_MEMORY,
        reason=str(
            plan.get("reason") or ("the resource plan fits" if fits else "no load plan fits")
        ),
        n_ctx=int(plan.get("n_ctx") or 0),
        gpu_layers=int(plan.get("gpu_layers") or 0),
        plan=plan,
    )
