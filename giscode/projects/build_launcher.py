"""Build launcher: the safe handoff from Project Workspace
APPROVE & START into the existing coding pipeline.

The :class:`BuildLauncher` consumes a :class:`BuildHandoff`
produced by :func:`giscode.projects.approval.approve_and_start`
and starts the *existing*
:class:`giscode.agentic.loop.CodingAgentLoop` with a
:class:`giscode.agentic.types.TaskRequest` constructed from the
handoff's fields. It does **not** replace, rewrite, or duplicate
the coding pipeline.

Design contract

* **No approval.** The launcher only ever runs after
  :func:`approve_and_start` has produced a handoff. It never
  re-approves, never moves lifecycle, never claims
  :class:`ProjectLifecycle` states it did not reach.
* **No second coding engine.** The launcher is given the
  application's existing :class:`CodingAgentLoop`. It calls
  :meth:`CodingAgentLoop.start` with a :class:`TaskRequest`
  built from the handoff.
* **Non-blocking.** :meth:`BuildLauncher.launch` returns
  immediately. The :class:`CodingAgentLoop.start` call runs on
  a daemon thread; completion / failure is reported via the
  supplied ``on_complete`` / ``on_failure`` callbacks (both
  invoked on the background thread — the caller is responsible
  for marshalling to the UI thread if needed).
* **Plan passed through the existing mechanism.** The handoff's
  ``plan`` dict is embedded in
  :attr:`TaskRequest.completion_criteria` under the key
  ``"project_plan"`` (so the existing :class:`TaskManager`
  surfaces it without any schema change), and a short textual
  summary is included in
  :attr:`TaskRequest.description` (so the existing
  :class:`Planner` and downstream stages can read it).
* **Duplicate protection.** A per-process, per-project guard
  refuses a second launch for the same project while the first
  is still running. This is in addition to the existing
  :class:`TaskManager` single-active-task guard.
* **Failure surfacing.** Any exception raised by
  :meth:`CodingAgentLoop.start` (or by handoff validation) is
  reported through ``on_failure`` and logged. The launcher
  does **not** silently report success.

Lifecycle

The launcher is responsible for the
``BUILDING → TESTING → VERIFICATION → COMPLETE`` transitions in
the sense that the existing :class:`CodingAgentLoop` and
:class:`TaskManager` drive them via
:func:`giscode.projects.store.ProjectStore.transition_lifecycle`.
This task does **not** add new lifecycle transitions and does
not mark the project ``COMPLETE`` merely because the loop
started; that remains the responsibility of the existing
``CodingAgentLoop`` / ``BuildTestOrchestrator`` / verifier
subsystems.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.rooting import (
    RootNotAllowedError,
    resolve_project_root,
    start_coding_task,
)
from giscode.agentic.types import TaskRequest, TaskResult
from giscode.projects.approval import BuildHandoff

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------- errors


class BuildLauncherError(Exception):
    """Base error for the build launcher."""


class BuildAlreadyInProgressError(BuildLauncherError):
    """Raised when a launch is requested for a project that
    already has an in-flight build."""


class InvalidHandoffError(BuildLauncherError):
    """Raised when the supplied :class:`BuildHandoff` is missing
    required fields.
    """


class BuildRootNotAllowedError(BuildLauncherError):
    """Raised when the project root cannot be used for this build.

    Raised before anything is launched, so the caller can leave the
    project in PLANNING instead of BUILDING.
    """


# ----------------------------------------------------------------- result


@dataclass(frozen=True)
class BuildLaunch:
    """Identifies a single launched build. Used by callers (and
    tests) to correlate callbacks with a specific project.
    """

    project_id: str
    task_id: str
    started_at: str
    thread_name: str

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "thread_name": self.thread_name,
        }


# Type aliases for the callback signatures. Callbacks are
# invoked on the background thread; callers must marshal to the
# UI thread themselves.
OnComplete = Callable[[BuildLaunch, TaskResult], None]
OnFailure = Callable[[BuildLaunch, BaseException], None]


# Reserved key under which the project plan is placed inside
# ``TaskRequest.completion_criteria``. Using a reserved key
# avoids collisions with anything the existing harness might
# add later; the existing pipeline already accepts an
# arbitrary ``Optional[dict]`` here.
PROJECT_PLAN_KEY = "project_plan"


# ----------------------------------------------------------------- launcher


def _utc_now_iso() -> str:
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{int((time.time() % 1) * 1000):03d}Z"
    )


def _summarize_plan(plan: dict) -> str:
    """Produce a short, human-readable summary of the project
    plan to embed in the :class:`TaskRequest` description. The
    full plan is also available in
    ``completion_criteria[PROJECT_PLAN_KEY]``.
    """
    if not isinstance(plan, dict):
        return ""
    inner = plan.get("plan", plan)
    if not isinstance(inner, dict):
        return ""
    und = inner.get("understanding", {}) or {}
    lines: list[str] = []
    idea = und.get("original_idea")
    if idea:
        lines.append(f"Original idea: {idea}")
    desc = und.get("refined_description")
    if desc:
        lines.append(f"Description: {desc}")
    req = inner.get("requirements", {}) or {}
    feats = req.get("features") or []
    if feats:
        lines.append("Features:")
        for f in feats:
            lines.append(f"  - {f}")
    des = inner.get("design", {}) or {}
    if des.get("application_type"):
        lines.append(f"Application type: {des['application_type']}")
    screens = inner.get("screens") or []
    if screens:
        lines.append(f"Screens: {len(screens)} defined")
    rm = inner.get("roadmap") or []
    if rm:
        lines.append(f"Roadmap: {len(rm)} items")
    return "\n".join(lines)


class BuildLauncher:
    """Launches the existing :class:`CodingAgentLoop` from a
    :class:`BuildHandoff`.

    The launcher is constructed with the application's existing
    :class:`CodingAgentLoop` (and may also be constructed with
    a factory that returns one). It is intentionally a thin
    adapter: no second coding engine, no second task system, no
    second lifecycle.
    """

    def __init__(
        self,
        coding_agent: CodingAgentLoop,
        *,
        allowed_root: Optional["str | Path"] = None,
        allowed_roots: Optional[Iterable["str | Path"]] = None,
        on_complete: Optional[OnComplete] = None,
        on_failure: Optional[OnFailure] = None,
    ) -> None:
        if coding_agent is None:
            raise BuildLauncherError("coding_agent is required")
        self._coding_agent = coding_agent
        # The folder the coding loop is allowed to be re-rooted into: the
        # application's configured projects folder. ``None`` keeps the
        # historical behaviour for callers that manage roots themselves.
        self._allowed_root = str(allowed_root) if allowed_root is not None else None
        # Folders the user explicitly selected outside that folder. Each one is
        # still validated (exists, is a directory, canonicalised) per launch;
        # this list only widens *which* explicit folders are acceptable, never
        # the containment rule inside them.
        self._allowed_roots = tuple(str(root) for root in (allowed_roots or ()))
        self._on_complete = on_complete
        self._on_failure = on_failure
        self._in_flight: dict[str, BuildLaunch] = {}
        self._in_flight_lock = threading.Lock()
        self._results: dict[str, TaskResult] = {}
        self._results_lock = threading.Lock()
        # project_id -> validated root, and project_id -> changed files
        # (``None`` while unknown). Guarded by ``_in_flight_lock``.
        self._roots: dict[str, Path] = {}
        self._changed: dict[str, Optional[list[str]]] = {}

    # ----------------------------------------------------------------- public

    def is_in_flight(self, project_id: str) -> bool:
        with self._in_flight_lock:
            return project_id in self._in_flight

    def get_in_flight(self, project_id: str) -> Optional[BuildLaunch]:
        with self._in_flight_lock:
            return self._in_flight.get(project_id)

    def get_result(self, project_id: str) -> Optional[TaskResult]:
        with self._results_lock:
            return self._results.get(project_id)

    def changed_files(self, project_id: str) -> Optional[list[str]]:
        """Project-relative files the last build wrote, or ``None`` if unknown.

        An empty list is a real result: the build ran and touched nothing, so
        the caller reports "completed with no changes" instead of plain
        completion. ``None`` means the coding agent cannot say.
        """
        with self._in_flight_lock:
            return self._changed.get(project_id)

    def validate_root(self, handoff: BuildHandoff) -> Optional[Path]:
        """Return the root this handoff may run in, or raise.

        Public so a caller can check *before* crossing the approval boundary:
        a refused root must leave the project in PLANNING, not BUILDING.
        """
        if handoff is None or not handoff.root_path:
            raise BuildRootNotAllowedError("handoff.root_path is required")
        if self._allowed_root is None and not self._allowed_roots:
            return None
        try:
            return resolve_project_root(
                handoff.root_path,
                allowed_root=self._allowed_root,
                allowed_roots=self._allowed_roots,
            )
        except RootNotAllowedError as exc:
            raise BuildRootNotAllowedError(str(exc)) from exc

    def cancel(self, project_id: str) -> bool:
        """Ask the in-flight coding loop to stop.

        Returns ``True`` only when a launch for ``project_id`` was in flight
        and the coding loop exposed its own ``cancel`` entry point. The loop
        stops at its next cancellation check and reports ``CANCELLED`` through
        ``on_complete``, so the caller still learns the real outcome instead of
        assuming the stop succeeded.
        """
        with self._in_flight_lock:
            in_flight = project_id in self._in_flight
        if not in_flight:
            return False
        cancel = getattr(self._coding_agent, "cancel", None)
        if not callable(cancel):
            return False
        try:
            cancel()
        except Exception:
            logger.exception(
                "build_launcher cancel failed project_id=%s", project_id
            )
            return False
        return True

    def launch(
        self,
        handoff: BuildHandoff,
        *,
        max_duration: Optional[float] = None,
    ) -> BuildLaunch:
        """Start the existing :class:`CodingAgentLoop` for the
        supplied :class:`BuildHandoff`.

        Returns immediately with a :class:`BuildLaunch` describing
        the background work. The actual
        :meth:`CodingAgentLoop.start` call runs on a daemon
        thread; completion is reported via ``on_complete`` and
        failure via ``on_failure``.

        Raises :class:`BuildAlreadyInProgressError` if a launch
        for the same project is already in flight. Raises
        :class:`InvalidHandoffError` if the handoff is missing
        required fields. Does not raise on coding-loop errors
        — those are reported through ``on_failure``.
        """
        if handoff is None:
            raise InvalidHandoffError("handoff is required")
        if not handoff.project_id:
            raise InvalidHandoffError("handoff.project_id is required")
        if not handoff.root_path:
            raise InvalidHandoffError("handoff.root_path is required")

        root = self.validate_root(handoff)

        with self._in_flight_lock:
            if handoff.project_id in self._in_flight:
                raise BuildAlreadyInProgressError(
                    f"a build is already in flight for project "
                    f"{handoff.project_id!r}"
                )

        request = self._build_task_request(handoff, max_duration=max_duration)
        task_id = request.task_id
        launch = BuildLaunch(
            project_id=handoff.project_id,
            task_id=task_id,
            started_at=_utc_now_iso(),
            thread_name="",  # filled in once the thread starts
        )

        with self._in_flight_lock:
            # Re-check after the lock dance.
            if handoff.project_id in self._in_flight:
                raise BuildAlreadyInProgressError(
                    f"a build is already in flight for project "
                    f"{handoff.project_id!r}"
                )
            self._in_flight[handoff.project_id] = launch
            if root is not None:
                self._roots[handoff.project_id] = root
            self._changed[handoff.project_id] = None

        thread = threading.Thread(
            target=self._run,
            args=(handoff, request, launch),
            name=f"build-launcher-{handoff.project_id[:8]}",
            daemon=True,
        )
        thread.start()
        return BuildLaunch(
            project_id=launch.project_id,
            task_id=launch.task_id,
            started_at=launch.started_at,
            thread_name=thread.name,
        )

    # ----------------------------------------------------------------- internal

    def _build_task_request(
        self,
        handoff: BuildHandoff,
        *,
        max_duration: Optional[float],
    ) -> TaskRequest:
        summary = _summarize_plan(handoff.plan or {})
        description = (
            f"Build project {handoff.project_name!r} "
            f"(id={handoff.project_id}).\n\n"
            f"Approved at {handoff.approved_at}, started at "
            f"{handoff.started_at}.\n\n"
            f"Project plan summary:\n{summary}"
        ).strip()
        completion_criteria: dict[str, Any] = {
            "project_id": handoff.project_id,
            "project_name": handoff.project_name,
            "root_path": handoff.root_path,
            PROJECT_PLAN_KEY: handoff.plan or {},
            "source": "project_workspace.build_launcher",
        }
        return TaskRequest(
            task_id=f"build-{handoff.project_id}",
            description=description,
            max_duration=max_duration,
            completion_criteria=completion_criteria,
        )

    def _record_changes(self, project_id: str, task_id: str) -> None:
        """Ask the coding agent which files this build really wrote.

        Agents that cannot answer leave ``None`` (unknown) rather than a false
        "no changes" claim.
        """
        reader = getattr(self._coding_agent, "changed_files_for", None)
        if not callable(reader):
            return
        try:
            changed = list(reader(task_id) or [])
        except Exception:  # noqa: BLE001 - unknown is better than a guess
            return
        with self._in_flight_lock:
            self._changed[project_id] = changed

    def _run(
        self,
        handoff: BuildHandoff,
        request: TaskRequest,
        launch: BuildLaunch,
    ) -> None:
        try:
            logger.info(
                "build_launcher starting project_id=%s task_id=%s",
                handoff.project_id,
                request.task_id,
            )
            with self._in_flight_lock:
                root = self._roots.get(handoff.project_id)
            result = start_coding_task(self._coding_agent, request, root)
            self._record_changes(handoff.project_id, request.task_id)
        except BaseException as exc:  # noqa: BLE001 — surface everything
            logger.exception(
                "build_launcher failed project_id=%s: %s",
                handoff.project_id,
                exc,
            )
            with self._in_flight_lock:
                self._in_flight.pop(handoff.project_id, None)
                self._roots.pop(handoff.project_id, None)
            if self._on_failure is not None:
                try:
                    self._on_failure(launch, exc)
                except Exception:  # pragma: no cover - defensive
                    logger.warning(
                        "on_failure callback raised: %s", traceback.format_exc()
                    )
            return

        with self._results_lock:
            self._results[handoff.project_id] = result
        with self._in_flight_lock:
            self._in_flight.pop(handoff.project_id, None)
            self._roots.pop(handoff.project_id, None)
        if self._on_complete is not None:
            try:
                self._on_complete(launch, result)
            except Exception:  # pragma: no cover - defensive
                logger.warning(
                    "on_complete callback raised: %s", traceback.format_exc()
                )
