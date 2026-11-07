"""Project Workspace session.

This module provides :class:`ProjectWorkspaceSession`, a small
orchestration layer that composes the existing persistence and
planning pieces for the *future* Project Workspace GUI. It is
deliberately *not* a GUI class and never imports :mod:`tkinter`.

The session is responsible for:

* loading and exposing the opened :class:`Project`,
* restoring its :class:`ProjectPlan` into a :class:`PlanerEngine`,
* locating and loading the persisted Planer chat via
  :class:`PlanerChatPersistence`,
* exposing a single :class:`PlanerAI` facade for the GUI,
* explicitly saving the plan back through :class:`ProjectStore`,
* exposing the existing Planer readiness state, **without**
  ever changing the project lifecycle, and
* releasing owned resources on :meth:`close`.

The workspace session is a *boundary*, not a brain. It must not:

* call :meth:`ProjectStore.approve`,
* call :meth:`ProjectStore.start_building`,
* invoke any coding agent, build orchestrator, tester, or
  verifier,
* execute shell commands,
* modify project files.

Those responsibilities belong to the explicit, user-driven
``APPROVE & START`` step, which is a *future* task.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from giscode.inference.runtime import InferenceError
from giscode.planer.ai import PlanerAI, PlanerAIResult
from giscode.planer.engine import PlanerEngine
from giscode.planer.plan import PlanReadiness, plan_to_project_dict
from giscode.planer.chat import PlanerChatPersistence
from giscode.planer.plan import PlanReadiness
from giscode.projects import Project, ProjectStore
from giscode.storage import ChatStore, MessageRecord as ChatMessage

logger = logging.getLogger(__name__)


class GisAiInferenceBridge:
    """Forward ``chat_generate`` to the GisAi engine's *current* backend.

    The workspace session is opened once, while the engine's writer surface
    can be swapped later (:meth:`GisAiEngine.set_writer_provider`). Caching
    the backend would leave the Planer talking to a replaced model, so the
    Planer holds this thin forwarder instead. It loads no model, starts no
    runtime, and calls exactly the method Chat calls.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def chat_generate(self, request: Any) -> Any:
        backend = getattr(self._engine, "inference", None)
        if backend is None:
            # PlanerAI turns this into ``inference_error`` and leaves the
            # plan untouched, so the GUI reports it instead of inventing one.
            raise InferenceError("no inference backend configured")
        return backend.chat_generate(request)


class ProjectWorkspaceError(Exception):
    """Base error for ProjectWorkspaceSession failures."""


class ProjectNotFoundError(ProjectWorkspaceError):
    """Raised when a project ID does not exist in the ProjectStore."""


class ProjectStoreUnavailableError(ProjectWorkspaceError):
    """Raised when the underlying ProjectStore is unreachable."""


class PlanerChatUnavailableError(ProjectWorkspaceError):
    """Raised when the Planer chat cannot be located or loaded."""


class WorkspaceAlreadyClosedError(ProjectWorkspaceError):
    """Raised when an operation is attempted on a closed session."""


@dataclass(frozen=True)
class WorkspaceReadiness:
    """Snapshot of the workspace's planning readiness, suitable for
    the future GUI.

    This is a *read-only* view; the workspace session never modifies
    project lifecycle based on this state.
    """

    plan_readiness: PlanReadiness
    is_ready_for_review: bool
    phase: object

    def to_dict(self) -> dict:
        return {
            "plan_readiness": self.plan_readiness.name
            if hasattr(self.plan_readiness, "name")
            else str(self.plan_readiness),
            "is_ready_for_review": self.is_ready_for_review,
            "phase": self.phase.name if hasattr(self.phase, "name") else str(self.phase),
        }


class ProjectWorkspaceSession:
    """An opened Project Workspace session for one project.

    Instances are created via :meth:`open` (existing project) or
    :meth:`create` (new project). The session owns its
    :class:`PlanerEngine`, :class:`PlanerAI`, and
    :class:`PlanerChatPersistence` for the lifetime of the
    session and releases them in :meth:`close`.

    The session never modifies the project lifecycle. It may *read*
    the lifecycle; it may *save* the plan; it may report readiness.
    Approving, building, testing, and verification are out of
    scope.
    """

    def __init__(
        self,
        project: Project,
        project_store: ProjectStore,
        chat_store: ChatStore,
        engine: PlanerEngine,
        ai: PlanerAI,
        chat: PlanerChatPersistence,
    ) -> None:
        self._project: Project = project
        self._project_store: ProjectStore = project_store
        self._chat_store: ChatStore = chat_store
        self._engine: PlanerEngine = engine
        self._ai: PlanerAI = ai
        self._chat: PlanerChatPersistence = chat
        self._closed: bool = False

    # ------------------------------------------------------------------ construction

    @classmethod
    def create(
        cls,
        project_store: ProjectStore,
        chat_store: ChatStore,
        name: str,
        root_path: str,
        description: str = "",
        plan_data: Optional[dict] = None,
        mode: str = "traditional",
        inference: Optional[Any] = None,
    ) -> "ProjectWorkspaceSession":
        """Create a new project and open a workspace for it.

        ``inference`` is the already-configured chat backend (the app's
        ``InferenceAPI`` or its live bridge); the Planer uses that same
        model and never loads one of its own.

        This delegates project creation to :class:`ProjectStore` (no
        duplicate schema, no separate DB). The new project is
        persisted with the normal initial lifecycle
        (``PLANNING``). No filesystem mutation is performed.
        """
        if project_store is None:
            raise ProjectStoreUnavailableError("project_store is required")
        if chat_store is None:
            raise PlanerChatUnavailableError("chat_store is required")
        if not name or not name.strip():
            raise ProjectWorkspaceError("project name must be a non-empty string")
        if not root_path or not root_path.strip():
            raise ProjectWorkspaceError("project root_path must be a non-empty string")

        project = Project.create(
            name=name.strip(),
            root_path=root_path.strip(),
            description=description,
            plan=plan_data,
            mode=mode,
        )
        try:
            stored = project_store.create_project(project)
        except Exception as exc:  # pragma: no cover - defensive
            raise ProjectStoreUnavailableError(
                f"failed to create project: {type(exc).__name__}: {exc}"
            ) from exc

        return cls._open_from_stored(
            stored, project_store, chat_store, inference=inference
        )

    @classmethod
    def open(
        cls,
        project_store: ProjectStore,
        chat_store: ChatStore,
        project_id: str,
        inference: Optional[Any] = None,
    ) -> "ProjectWorkspaceSession":
        """Open an existing project as a workspace.

        Raises :class:`ProjectNotFoundError` if the project does not
        exist. The project's lifecycle is *not* changed.
        """
        if project_store is None:
            raise ProjectStoreUnavailableError("project_store is required")
        if chat_store is None:
            raise PlanerChatUnavailableError("chat_store is required")
        if not project_id or not project_id.strip():
            raise ProjectWorkspaceError("project_id must be a non-empty string")

        try:
            project = project_store.get_project(project_id)
        except Exception as exc:  # pragma: no cover - defensive
            raise ProjectStoreUnavailableError(
                f"failed to read project {project_id!r}: {type(exc).__name__}: {exc}"
            ) from exc

        if project is None:
            raise ProjectNotFoundError(
                f"project {project_id!r} does not exist"
            )

        return cls._open_from_stored(
            project, project_store, chat_store, inference=inference
        )

    @classmethod
    def _open_from_stored(
        cls,
        project: Project,
        project_store: ProjectStore,
        chat_store: ChatStore,
        inference: Optional[Any] = None,
    ) -> "ProjectWorkspaceSession":
        engine = PlanerEngine(project_id=project.id)
        ai = PlanerAI(engine=engine, inference=inference)
        chat = PlanerChatPersistence(project=project, chat_store=chat_store)

        session = cls(
            project=project,
            project_store=project_store,
            chat_store=chat_store,
            engine=engine,
            ai=ai,
            chat=chat,
        )

        # Restore the persisted ProjectPlan into the engine. A
        # missing/empty plan is acceptable (fresh project); a
        # malformed one is reported.
        if project.plan:
            try:
                session._engine.restore_plan(project.plan)
            except ValueError as exc:
                raise ProjectWorkspaceError(
                    f"invalid persisted plan for project {project.id!r}: {exc}"
                ) from exc

        # Resolve (but do not yet create) the Planer chat. We
        # deliberately avoid calling get_or_create_chat() during
        # open so opening is read-only: if the project has no
        # Planer chat yet, the chat is materialised on the first
        # user message via chat_with_planer().
        existing = chat.find_existing_chat()
        if existing is not None:
            # The persistence layer cached the chat id; nothing
            # else to do here. History is loaded lazily by
            # load_chat_history().
            pass

        return session

    # ------------------------------------------------------------------ accessors

    @property
    def project(self) -> Project:
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")
        return self._project

    @property
    def project_id(self) -> str:
        # Closed sessions still expose the project id (a plain
        # string) so callers can pass it to a new open call
        # without first having to copy the id out.
        if self._closed:
            try:
                return self._project.id  # may also raise; that's OK
            except Exception:
                return ""
        return self._project.id

    @property
    def lifecycle(self) -> object:
        return self.project.lifecycle

    @property
    def engine(self) -> PlanerEngine:
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")
        return self._engine

    @property
    def ai(self) -> PlanerAI:
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")
        return self._ai

    @property
    def planer_chat(self) -> PlanerChatPersistence:
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")
        return self._chat

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ planer chat

    def load_chat_history(self) -> List[ChatMessage]:
        """Return the persisted Planer conversation for this project.

        Returns an empty list if no Planer chat exists yet.
        """
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")
        existing = self._chat.find_existing_chat()
        if existing is None:
            return []
        try:
            return self._chat.load_history()
        except Exception as exc:
            raise PlanerChatUnavailableError(
                f"failed to load planer chat history: {type(exc).__name__}: {exc}"
            ) from exc

    def chat_with_planer(
        self,
        user_message: str,
        attachments: Optional[Iterable[Any]] = None,
        vision_service: Optional[Any] = None,
    ) -> PlanerAIResult:
        """Send ``user_message`` to :class:`PlanerAI` and persist
        the turn via :class:`PlanerChatPersistence`.

        When ``attachments`` are given they are really read and
        described by the vision pipeline before planning, and the
        attachment metadata is persisted alongside the user turn so the
        stored history records which image was discussed.

        This composes the existing components; it does not duplicate
        any planning logic.
        """
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")
        if not user_message or not user_message.strip():
            raise ProjectWorkspaceError("user_message must be a non-empty string")

        result = self._ai.chat(
            user_message,
            attachments=attachments,
            vision_service=vision_service,
        )

        # An image that could not be read must never be stored as an
        # ordinary turn: the history would then show a successful-looking
        # exchange for a picture that was never actually seen. The honest
        # image error is returned to the caller (and surfaced by the GUI)
        # and nothing is written to the chat store.
        if result.image_error:
            return result

        try:
            meta = None
            if result.images:
                meta = {"attachments": list(result.images)}
            self._chat.record_user_turn(user_message, meta=meta)
            self._chat.record_assistant_turn(result.assistant_message)
        except Exception as exc:
            raise PlanerChatUnavailableError(
                f"failed to persist planer turn: {type(exc).__name__}: {exc}"
            ) from exc
        return result

    # ------------------------------------------------------------------ plan

    def save_plan(self) -> Project:
        """Serialize the engine's current :class:`ProjectPlan` and
        persist it through :class:`ProjectStore`.

        The project's lifecycle is *not* changed. Approving the
        project is an explicit, future step.
        """
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")

        plan = self._engine.build_plan()
        plan_data = dict(self._project.plan)
        plan_data.update(plan_to_project_dict(plan))
        try:
            updated = self._project_store.set_plan(self._project.id, plan_data)
        except Exception as exc:
            raise ProjectStoreUnavailableError(
                f"failed to save plan for project {self._project.id!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if updated is None:
            raise ProjectNotFoundError(
                f"project {self._project.id!r} disappeared during save"
            )
        # Refresh the in-memory project so subsequent reads see
        # the persisted plan.
        self._project = updated
        return updated

    def get_visual_design(self) -> dict:
        """Return a detached copy of this project's Visual Design state."""
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")
        value = self._project.plan.get("visual_design", {})
        return deepcopy(value) if isinstance(value, dict) else {}

    def save_visual_design(self, visual_design: dict) -> Project:
        """Persist Visual Design state without replacing other project data."""
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")
        if not isinstance(visual_design, dict):
            raise ProjectWorkspaceError("visual_design must be a dictionary")

        plan_data = dict(self._project.plan)
        plan_data["visual_design"] = deepcopy(visual_design)
        try:
            updated = self._project_store.set_plan(self._project.id, plan_data)
        except Exception as exc:
            raise ProjectStoreUnavailableError(
                f"failed to save visual design for project {self._project.id!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if updated is None:
            raise ProjectNotFoundError(
                f"project {self._project.id!r} disappeared during visual design save"
            )
        self._project = updated
        return updated

    # ------------------------------------------------------------------ readiness

    def readiness(self) -> WorkspaceReadiness:
        """Return the current planning readiness, derived from the
        engine's own validation.

        The session never transitions the project to ``REVIEW``,
        ``APPROVED``, or ``BUILDING`` based on this state.
        """
        if self._closed:
            raise WorkspaceAlreadyClosedError("workspace session is closed")
        return WorkspaceReadiness(
            plan_readiness=self._engine.readiness(),
            is_ready_for_review=self._engine.is_ready_for_review(),
            phase=self._engine.phase,
        )

    def is_ready_for_review(self) -> bool:
        return self.readiness().is_ready_for_review

    # ------------------------------------------------------------------ close

    def close(self) -> None:
        """Release owned resources. The project, its plan, and its
        Planer chat are *not* deleted. The next workspace session
        for the same project can reopen them.
        """
        if self._closed:
            return
        # The engine is in-memory; nothing to release. The
        # ChatStore is shared across sessions and is closed
        # only by its owner (typically the test harness or the
        # future GUI). We just drop our references and mark the
        # session as closed.
        self._closed = True
        self._engine = None  # type: ignore[assignment]
        self._ai = None  # type: ignore[assignment]
        self._chat = None  # type: ignore[assignment]

    # ------------------------------------------------------------------ dunder

    def __enter__(self) -> "ProjectWorkspaceSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        state = "closed" if self._closed else "open"
        try:
            pid = self._project.id if self._project is not None else "<none>"
        except Exception:
            pid = "<unreadable>"
        return f"ProjectWorkspaceSession(project_id={pid!r}, state={state})"
