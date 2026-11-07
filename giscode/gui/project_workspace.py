"""Project Workspace GUI for GisCode.

This module is the *first* real Project Workspace front-end. It
composes the backend pieces already implemented in Tasks 4-9:

* :class:`giscode.projects.store.ProjectStore`      (Tasks 4)
* :class:`giscode.projects.model.Project`          (Tasks 4)
* :class:`giscode.projects.workspace.ProjectWorkspaceSession` (Task 8)
* :class:`giscode.projects.approval.approve_and_start`        (Task 9)
* :class:`giscode.planer.engine.PlanerEngine`      (Task 5)
* :class:`giscode.planer.ai.PlanerAI`              (Task 6)
* :class:`giscode.planer.chat.PlanerChatPersistence`(Task 7)
* :class:`giscode.storage.chat_store.ChatStore`    (Task 4+)

Architecture
------------

The module is split into a **controller** and a **view**:

* :class:`ProjectWorkspaceController` is non-visual and
  framework-agnostic. It holds the
  :class:`ProjectStore`/``ChatStore`` and the current
  :class:`ProjectWorkspaceSession`, and exposes the operations
  the GUI needs (create project, open project, list projects,
  send planer message, save plan, approve & start, close).
  This is the layer the test suite exercises.

* :class:`ProjectWorkspaceView` is the Tk widget that the
  controller drives. It is created with an existing parent
  frame; it never creates its own root window. It calls the
  controller and renders the result.

The view is *not* a Toplevel; it embeds inside the existing
``MainWindow`` "Projects" tab. Existing GUI is preserved.

Safety
------

The view never calls :meth:`ProjectStore.approve` or
:meth:`ProjectStore.start_building` directly. The only path from
PLANNING / REVIEW to BUILDING is the explicit button, which
goes through :func:`approve_and_start` (Task 9). The controller
exposes :meth:`ProjectWorkspaceController.approve_and_start`
which is the only public method that may cross the approval
boundary.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable, Iterator, List, Optional

from giscode.agentic.rooting import (
    RootNotAllowedError,
    canonical_path,
    external_root_notice,
    is_external_project_root,
    resolve_project_root,
)
from giscode.config.paths import profile_root
from giscode.planer.ai import PlanerAI
from giscode.planer.engine import PlanerEngine
from giscode.planer.chat import PlanerChatPersistence
from giscode.projects import (
    Project,
    ProjectLifecycle,
    ProjectStore,
)
from giscode.projects.lifecycle import RETRYABLE_STATES, can_transition
from giscode.projects.workspace import ProjectStoreUnavailableError, ProjectWorkspaceError
from giscode.projects.workspace import GisAiInferenceBridge
from giscode.projects.workspace import ProjectNotFoundError
from giscode.projects.workspace import PlanerChatUnavailableError, ProjectWorkspaceSession
from giscode.storage import ChatStore

from giscode.gui.text_interaction import (
    add_text_context_menu,
    make_value_copyable,
)


logger = logging.getLogger(__name__)

# Lifecycle states in which a build or its verification is still in progress.
# They are the only states a completion callback may move, and the only ones the
# Projects tab keeps polling for, because the outcome arrives after the worker
# thread has already left the launcher's in-flight table.
_IN_PROGRESS_STATES = frozenset({
    ProjectLifecycle.BUILDING,
    ProjectLifecycle.TESTING,
    ProjectLifecycle.VERIFICATION,
})


# ----------------------------------------------------------------- build handoff


class BuildStatus(str, Enum):
    """What the approve -> launch handoff actually achieved.

    Approval alone changes the project lifecycle but runs no code, so these
    states are kept apart: a launch that never started anything must never be
    reported as a running build.
    """

    RUNNING = "running"
    LAUNCH_FAILED = "launch_failed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class BuildStartResult:
    """Outcome of :meth:`ProjectWorkspaceController.approve_and_start_build`."""

    status: BuildStatus
    message: str
    approved: bool
    launched: bool
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.status == BuildStatus.RUNNING


# ----------------------------------------------------------------- errors


class ExternalProjectPermissionRequired(ProjectWorkspaceError):
    """A project lives outside the configured projects folder.

    External projects are supported, but only after the user explicitly
    selects that exact folder: the window asks with the wording in
    :func:`giscode.agentic.rooting.external_root_notice` (which names the
    directory GisCode will modify) and retries with ``allow_external=True``.
    Carrying the target path and the notice on the exception keeps the text
    the user approves identical to the text the tests pin.
    """

    def __init__(
        self,
        project_id: Optional[str],
        root_path: "str | os.PathLike[str]",
        notice: str,
    ) -> None:
        super().__init__(notice)
        self.project_id = project_id
        self.root_path = str(root_path)
        self.notice = notice


# ----------------------------------------------------------------- helpers


def _default_projects_root() -> str:
    """Return the default projects workspace root, creating it if necessary."""
    root = str(profile_root() / "giscode_projects")
    os.makedirs(root, exist_ok=True)
    return root


# ----------------------------------------------------------------- dialogs


class _StartProjectDialog:
    """Modal dialog for collecting project name (path is auto-generated in workspace)."""

    def __init__(
        self, parent: tk.Misc, existing_project_roots: Optional[list[str]] = None
    ) -> None:
        self._existing_project_roots = list(existing_project_roots or [])
        self._top = tk.Toplevel(parent)
        self._top.title("Start New Project")
        self._top.transient(parent.winfo_toplevel())
        self._top.grab_set()
        self._result: Optional[tuple[str, str, str]] = None

        body = ttk.Frame(self._top, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="Project name:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self._name_var = tk.StringVar()
        name_entry = ttk.Entry(body, textvariable=self._name_var, width=40)
        name_entry.grid(row=0, column=1, sticky=tk.EW, pady=2)
        add_text_context_menu(name_entry, editable=True)

        # Development mode selection
        ttk.Label(body, text="Development mode:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self._mode_var = tk.StringVar(value="traditional")
        mode_frame = ttk.Frame(body)
        mode_frame.grid(row=1, column=1, sticky=tk.W, pady=2)
        ttk.Radiobutton(mode_frame, text="Traditional Coding", variable=self._mode_var,
                        value="traditional").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(mode_frame, text="Visual App Design", variable=self._mode_var,
                        value="visual_app_design").pack(side=tk.LEFT)

        # Auto-generate path in default workspace; user can override if needed
        ttk.Label(body, text="Project path:").grid(row=2, column=0, sticky=tk.W, pady=2)
        path_row = ttk.Frame(body)
        path_row.grid(row=2, column=1, sticky=tk.EW, pady=2)
        self._path_var = tk.StringVar(value=_default_projects_root())
        path_entry = ttk.Entry(path_row, textvariable=self._path_var, width=32)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        add_text_context_menu(path_entry, editable=True)
        ttk.Button(path_row, text="Browse…", command=self._browse).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        body.columnconfigure(1, weight=1)

        btns = ttk.Frame(self._top, padding=8)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Create", command=self._ok).pack(
            side=tk.RIGHT, padx=2
        )
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(
            side=tk.RIGHT, padx=2
        )

        self._top.bind("<Return>", lambda _e: self._ok())
        self._top.bind("<Escape>", lambda _e: self._cancel())
        self._top.protocol("WM_DELETE_WINDOW", self._cancel)

    def _browse(self) -> None:
        directory = filedialog.askdirectory(
            title="Select project folder", parent=self._top
        )
        if directory:
            self._path_var.set(directory)

    def _ok(self) -> None:
        name = self._name_var.get().strip()
        path = self._path_var.get().strip()
        mode = self._mode_var.get()
        if not name:
            # Auto-generate a unique name in the default workspace
            base_name = "Project"
            workspace = _default_projects_root()
            counter = 1
            while True:
                candidate = f"{base_name}_{counter}" if counter > 1 else base_name
                candidate_path = os.path.join(workspace, candidate)
                if not os.path.exists(candidate_path):
                    name = candidate
                    break
                counter += 1
                if counter > 1000:
                    messagebox.showerror("Start New Project", "Too many projects; cannot generate unique name.", parent=self._top)
                    return
            self._name_var.set(name)
            # Re-validate with generated name
            path = self._path_var.get().strip()
            if not path:
                path = workspace
                self._path_var.set(path)
        if not name:
            messagebox.showerror("Start New Project", "Name is required.", parent=self._top)
            return
        if not path:
            messagebox.showerror("Start New Project", "Path is required.", parent=self._top)
            return
        # Validate that the path is not inside an existing project directory
        if self._is_inside_existing_project(path):
            messagebox.showerror(
                "Start New Project",
                "Cannot create a new project inside an existing project directory.\n"
                "Choose a different location or use 'Open Existing Project' to select it without changes.",
                parent=self._top,
            )
            return
        if self._has_project_markers(path):
            messagebox.showerror(
                "Existing Project Folder",
                "This folder contains an existing project marker. Use 'Open Existing Project' "
                "to select and inspect it without creating a new project record.",
                parent=self._top,
            )
            return
        if self._is_ambiguous_nonempty_directory(path):
            proceed = messagebox.askyesno(
                "Existing Folder",
                "This folder already contains files, but GisCode cannot identify it as a "
                "registered project. Create a new project record here anyway? Existing "
                "folder contents will not be changed. Choose No to cancel, then use "
                "'Open Existing…' to select it for inspection.",
                parent=self._top,
            )
            if not proceed:
                return
        self._result = (name, path, mode)
        self._top.destroy()

    def _is_inside_existing_project(self, path: str) -> bool:
        """Check if the given path is inside an existing project directory."""
        path = os.path.normcase(os.path.abspath(path))
        roots = list(self._existing_project_roots)
        try:
            from giscode.projects.store import ProjectStore

            with ProjectStore() as store:
                roots.extend(project.root_path for project in store.list_projects())
        except Exception:
            pass
        for root in roots:
            project_root = os.path.normcase(os.path.abspath(root))
            try:
                if os.path.commonpath((path, project_root)) == project_root:
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _has_project_markers(path: str) -> bool:
        """Use the application's established checkout markers as evidence."""
        root = Path(path)
        return (
            (root / "giscode").is_dir()
            or (root / ".git").exists()
            or (root / "GisCode.spec").is_file()
            or (root / "pyproject.toml").is_file()
        )

    @staticmethod
    def _is_ambiguous_nonempty_directory(path: str) -> bool:
        """Identify uncertain populated folders before New Project proceeds."""
        try:
            if not os.path.isdir(path):
                return False
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
                os.path.abspath(_default_projects_root())
            ):
                return False
            with os.scandir(path) as entries:
                return next(entries, None) is not None
        except OSError:
            return False

    def _cancel(self) -> None:
        self._result = None
        self._top.destroy()

    def show(self) -> Optional[tuple[str, str, str]]:
        try:
            self._top.wait_window()
        except Exception:
            pass
        return self._result


# ----------------------------------------------------------------- dialogs


class _ProjectSettingsDialog:
    """Modal dialog for editing project settings (name, description)."""

    def __init__(
        self,
        parent: tk.Misc,
        project_store: ProjectStore,
        project_id: str,
    ) -> None:
        self._project_store = project_store
        self._project_id = project_id
        self._result: Optional[dict] = None

        self._top = tk.Toplevel(parent)
        self._top.title("Project Settings")
        self._top.transient(parent.winfo_toplevel())
        self._top.grab_set()
        self._top.resizable(False, False)

        project = project_store.get_project(project_id)
        if project is None:
            self._top.destroy()
            return

        self._original_name = project.name
        self._original_description = project.description
        self._original_lifecycle = project.lifecycle.value
        self._original_root_path = project.root_path
        self._original_created_at = project.created_at
        self._original_updated_at = project.updated_at

        body = ttk.Frame(self._top, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        # Project ID (read-only)
        ttk.Label(body, text="Project ID:").grid(row=0, column=0, sticky=tk.W, pady=2)
        ttk.Label(body, text=project.id, foreground="gray").grid(row=0, column=1, sticky=tk.W, pady=2, padx=(8, 0))

        # Project name (editable)
        ttk.Label(body, text="Project name:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self._name_var = tk.StringVar(value=project.name)
        edit_name_entry = ttk.Entry(body, textvariable=self._name_var, width=40)
        edit_name_entry.grid(row=1, column=1, sticky=tk.EW, pady=2, padx=(8, 0))
        add_text_context_menu(edit_name_entry, editable=True)

        # Description (editable)
        ttk.Label(body, text="Description:").grid(row=2, column=0, sticky=tk.NW, pady=2)
        self._description_text = tk.Text(body, width=40, height=4, wrap=tk.WORD)
        self._description_text.grid(row=2, column=1, sticky=tk.EW, pady=2, padx=(8, 0))
        self._description_text.insert("1.0", project.description or "")
        add_text_context_menu(self._description_text, editable=True)

        # Lifecycle (read-only)
        ttk.Label(body, text="Lifecycle:").grid(row=3, column=0, sticky=tk.W, pady=2)
        make_value_copyable(ttk.Label(
            body, text=project.lifecycle.value.capitalize(), foreground="gray"
        )).grid(row=3, column=1, sticky=tk.W, pady=2, padx=(8, 0))

        # Root path (read-only)
        ttk.Label(body, text="Root path:").grid(row=4, column=0, sticky=tk.W, pady=2)
        make_value_copyable(ttk.Label(
            body, text=project.root_path, foreground="gray"
        )).grid(row=4, column=1, sticky=tk.W, pady=2, padx=(8, 0))

        # Created / Updated timestamps (read-only)
        ttk.Label(body, text="Created:").grid(row=5, column=0, sticky=tk.W, pady=2)
        make_value_copyable(ttk.Label(
            body, text=project.created_at, foreground="gray"
        )).grid(row=5, column=1, sticky=tk.W, pady=2, padx=(8, 0))
        ttk.Label(body, text="Updated:").grid(row=6, column=0, sticky=tk.W, pady=2)
        make_value_copyable(ttk.Label(
            body, text=project.updated_at, foreground="gray"
        )).grid(row=6, column=1, sticky=tk.W, pady=2, padx=(8, 0))

        body.columnconfigure(1, weight=1)

        btns = ttk.Frame(self._top, padding=8)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Save", command=self._ok).pack(
            side=tk.RIGHT, padx=2
        )
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(
            side=tk.RIGHT, padx=2
        )

        self._top.bind("<Escape>", lambda _e: self._cancel())
        self._top.protocol("WM_DELETE_WINDOW", self._cancel)

    def _ok(self) -> None:
        name = self._name_var.get().strip()
        description = self._description_text.get("1.0", "end-1c").strip()

        if not name:
            messagebox.showerror("Project Settings", "Name is required.", parent=self._top)
            return

        # Normalize name using existing function
        from giscode.projects.model import normalize_project_name
        name = normalize_project_name(name)

        # Only save if something changed
        if name == self._original_name and description == self._original_description:
            self._result = {"changed": False}
            self._top.destroy()
            return

        updated = self._project_store.update_project(
            self._project_id,
            name=name,
            description=description,
        )
        if updated is None:
            messagebox.showerror("Project Settings", "Project not found.", parent=self._top)
            return

        self._result = {"changed": True, "name": updated.name, "description": updated.description}
        self._top.destroy()

    def _cancel(self) -> None:
        self._result = None
        self._top.destroy()

    def show(self) -> Optional[dict]:
        try:
            self._top.wait_window()
        except Exception:
            pass
        return self._result


# ----------------------------------------------------------------- controller


class ProjectWorkspaceController:
    """Non-visual controller for the Project Workspace."""

    def __init__(
        self,
        project_store: ProjectStore,
        chat_store: ChatStore,
        vision_service: Optional[object] = None,
        gisai_engine: Optional[object] = None,
        coding_agent: Optional[object] = None,
    ) -> None:
        self._project_store = project_store
        self._chat_store = chat_store
        # Optional: the shared on-demand vision capability. When present
        # it really reads attached images with a local vision model.
        self._vision_service = vision_service
        self._gisai_engine = gisai_engine
        # Optional: the application's existing CodingAgentLoop, consumed by the
        # existing BuildLauncher when an approved plan is started. Without it the
        # approval handoff has nothing to run, and the controller says so instead
        # of leaving the project in BUILDING.
        self._coding_agent = coding_agent
        self._build_launcher = None
        self._build_states: dict[str, BuildStartResult] = {}
        self._build_lock = threading.Lock()
        # Explicit retries in progress (double-click guard) and the stop flag
        # each launched build owns, so verification can honour Stop Build.
        self._retrying: set[str] = set()
        self._build_cancel_events: dict[str, threading.Event] = {}
        # Projects whose completion callback is still running (verification).
        # The launcher removes a project from its in-flight table before that
        # callback runs, so this is what keeps "still working" true.
        self._finishing: set[str] = set()
        # The verification report behind each finished build's message.
        self._verification_reports: dict[str, object] = {}
        self._session: Optional[ProjectWorkspaceSession] = None
        self._existing_project_root: Optional[str] = None
        self._lock = threading.Lock()
        # Canonical roots outside the projects folder that the user selected in
        # this session. A build may only be re-rooted into one of these or into
        # the projects folder itself; the list is never inferred from a record.
        self._confirmed_external_roots: set[str] = set()


    # ----------------------------------------------------------------- properties

    @property
    def current_project_id(self) -> Optional[str]:
        if self._session is None:
            return None
        return self._session.project_id

    @property
    def vision_service(self) -> Optional[object]:
        """The shared vision capability, or ``None`` when unavailable."""
        return self._vision_service

    @property
    def has_active_session(self) -> bool:
        """Whether a project session is open.

        Project-bound operations require this. The standalone image path
        deliberately does not, so the view uses this to decide whether a
        response has persisted chat history to be read back from.
        """
        return self._session is not None


    @property
    def current_project_root(self) -> Optional[str]:
        if self._session is not None:
            return self._session.project.root_path
        return self._existing_project_root

    @property
    def lifecycle(self) -> Optional[ProjectLifecycle]:
        if self._session is None:
            return None
        # Approval and build outcomes are written to the store, not to the
        # session's cached Project. Read the stored lifecycle so the State label
        # reflects what actually happened, falling back to the session value when
        # the store cannot answer.
        try:
            project = self._project_store.get_project(self._session.project_id)
        except Exception:
            project = None
        if project is not None:
            return project.lifecycle
        return self._session.lifecycle

    @property
    def engine(self) -> Optional[PlanerEngine]:
        if self._session is None:
            return None
        return self._session.engine

    @property
    def ai(self) -> Optional[PlanerAI]:
        if self._session is None:
            return None
        return self._session.ai

    @property
    def planer_chat(self) -> Optional[PlanerChatPersistence]:
        if self._session is None:
            return None
        return self._session.planer_chat

    def _planer_inference(self) -> Optional[object]:
        """The backend the Planer should call: Chat's own active model.

        The GisAi engine owns the single loaded model (and the optional
        hosted writer surface). A live bridge is handed to the Planer so a
        plan request uses that model without loading a second one, and so a
        later backend swap is honoured. ``None`` (no engine wired) keeps the
        Planer's honest "no inference backend configured" answer.
        """
        engine = self._gisai_engine
        if engine is None:
            return None
        return GisAiInferenceBridge(engine)

    # ----------------------------------------------------------------- create / open / close

    def create_project(
        self,
        name: str,
        root_path: str,
        description: str = "",
        plan_data: Optional[dict] = None,
        mode: str = "traditional",
        *,
        allow_external: bool = False,
    ) -> ProjectWorkspaceSession:
        """Create a new project and open a workspace for it.

        A folder outside the configured projects folder is an *external*
        project. It is supported, but only after the user selected that exact
        folder: the caller passes ``allow_external=True`` once the notice from
        :meth:`external_project_notice` has been shown and accepted. An
        external folder must already exist and be a directory; refusing that
        here means nothing is recorded and nothing is touched.
        """
        name = (name or "").strip()
        root_path = (root_path or "").strip()
        if not name:
            raise ProjectWorkspaceError("project name must be a non-empty string")
        if not root_path:
            raise ProjectWorkspaceError("project root_path must be a non-empty string")
        if self._needs_external_confirmation(root_path):
            try:
                usable = resolve_project_root(root_path)
            except RootNotAllowedError as exc:
                raise ProjectWorkspaceError(
                    f"the external project folder cannot be used: {exc}"
                ) from exc
            if not allow_external:
                raise ExternalProjectPermissionRequired(
                    None, usable, self.external_project_notice(usable)
                )
            self._confirm_external_root(usable)

        with self._lock:
            self._close_current_locked()
            self._existing_project_root = None
            try:
                session = ProjectWorkspaceSession.create(
                    project_store=self._project_store,
                    chat_store=self._chat_store,
                    name=name,
                    root_path=root_path,
                    description=description,
                    plan_data=plan_data,
                    mode=mode,
                    inference=self._planer_inference(),
                )
            except ProjectStoreUnavailableError as exc:
                raise ProjectWorkspaceError(
                    f"Failed to create project: {exc}"
                ) from exc
            self._session = session
            return session

    def open_project(
        self,
        project_id: str,
        *,
        allow_external: bool = False,
    ) -> ProjectWorkspaceSession:
        """Open an existing project as a workspace.

        A stored record whose root is outside the configured projects folder
        is opened only when the user explicitly selected it
        (``allow_external=True``). Otherwise
        :class:`ExternalProjectPermissionRequired` carries the exact notice
        for the window to show, and nothing is opened or modified.
        """
        project_id = (project_id or "").strip()
        if not project_id:
            raise ProjectWorkspaceError("Project id is required")
        self._authorise_stored_root(project_id, allow_external=allow_external)
        with self._lock:
            self._close_current_locked()
            self._existing_project_root = None
            try:
                session = ProjectWorkspaceSession.open(
                    project_store=self._project_store,
                    chat_store=self._chat_store,
                    project_id=project_id,
                    inference=self._planer_inference(),
                )
            except ProjectNotFoundError as exc:
                raise ProjectWorkspaceError(
                    f"Project not found: {project_id}"
                ) from exc
            except ProjectStoreUnavailableError as exc:
                raise ProjectWorkspaceError(
                    f"Failed to open project: {exc}"
                ) from exc
            self._session = session
            return session

    def select_existing_project_root(self, root_path: Optional[str]) -> None:
        """Select a transferred/legacy project root without creating a record.

        This deliberately does not inspect, initialize, or write to the
        selected directory. Stored Project records continue to use
        :meth:`open_project`; this path-only state exists for folders that
        have not been registered on this machine.
        """
        normalized = os.path.abspath(root_path) if root_path else None
        with self._lock:
            if (
                self._session is not None
                and normalized is not None
                and os.path.normcase(self._session.project.root_path)
                == os.path.normcase(normalized)
            ):
                self._existing_project_root = None
                return
            self._close_current_locked()
            self._existing_project_root = normalized

    def close(self) -> None:
        """Close the current workspace session."""
        with self._lock:
            self._close_current_locked()
            self._existing_project_root = None

    def _close_current_locked(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                logger.debug("Error closing session", exc_info=True)
            self._session = None

    # ----------------------------------------------------------------- list / info

    def list_projects(self) -> List[Project]:
        """List projects using the store's existing domain objects."""
        return list(self._project_store.list_projects())

    # ----------------------------------------------------------------- planer chat / messages

    def send_planer_message(self, message: str):
        """Send a text message to the Planer AI and return the response.

        This is the original text-only path and is unchanged: no image
        is involved, so no vision capability is consulted.
        """
        if self._session is None:
            raise ProjectWorkspaceError("No active project session")
        return self._session.chat_with_planer(message)

    def send_planer_message_with_images(
        self,
        message: str,
        attachments: Iterable[object],
    ):
        """Send a message together with real image attachments.

        The images are read and described by the vision pipeline before
        planning. Requires a vision service: without one the call fails
        honestly rather than answering as if the image had been seen.
        """
        if self._session is None:
            if self._gisai_engine is None:
                raise ProjectWorkspaceError("No active project session")
            return self._send_standalone_image_message(message, attachments)
        return self._session.chat_with_planer(
            message,
            attachments=attachments,
            vision_service=self._vision_service,
        )

    def _send_standalone_image_message(self, message: str, attachments: Iterable[object]):
        """Use the existing GisAI vision path for project-independent images."""
        from giscode.agentic.attachments import Attachment
        from giscode.planer.ai import PlanerAIResult
        from giscode.vision.images import build_attachment_dicts, load_images

        paths = [str(getattr(item, "path", item)) for item in attachments]
        loaded = load_images(paths)
        task = self._gisai_engine.receive(
            message,
            attachments=[
                Attachment(image.path, image.mime_type, image.size_bytes)
                for image in loaded
            ],
            project_context=None,
        )
        state = getattr(getattr(task, "state", None), "value", None)
        if state == "waiting_input":
            return PlanerAIResult(
                success=False,
                parse_error="Waiting for an image attachment before analysis.",
                image_status="failed",
                image_error="Waiting for an image attachment before analysis.",
            )
        result = self._gisai_engine.execute(task)
        if not isinstance(result, dict):
            return PlanerAIResult(
                success=False,
                inference_error="Standalone image analysis returned no structured result.",
            )
        if result.get("error"):
            return PlanerAIResult(
                success=False,
                parse_error=str(result["error"]),
                image_status="failed",
                image_error=str(result["error"]),
                images=build_attachment_dicts(loaded),
            )
        observation = str(result.get("vision_result") or result.get("response") or "")
        return PlanerAIResult(
            success=bool(observation),
            assistant_message=observation,
            image_status="interpreted" if observation else "failed",
            image_report=observation,
            image_model=str(result.get("model_used") or ""),
            images=build_attachment_dicts(loaded),
        )


    def load_chat_history(self) -> List[dict]:
        """Load the chat history for the current project."""
        if self._session is None:
            raise ProjectWorkspaceError("No active project session")
        return [message.to_dict() for message in self._session.load_chat_history()]

    # ----------------------------------------------------------------- plan / readiness

    def get_plan_dict(self) -> Optional[dict]:
        """Get the current plan as a dictionary."""
        if self._session is None:
            return None
        return self._session.project.plan

    def get_readiness(self) -> dict:
        """Get the plan readiness status."""
        if self._session is None:
            return {"plan_readiness": "NO_PLAN", "is_ready_for_review": False}
        return self._session.readiness().to_dict()

    def save_plan(self) -> None:
        """Save the current plan to the project store."""
        if self._session is None:
            raise ProjectWorkspaceError("No active project session")
        self._session.save_plan()

    def get_visual_design(self) -> dict:
        """Return the active project's Visual Design document."""
        if self._session is None:
            raise ProjectWorkspaceError("No active project session")
        return self._session.get_visual_design()

    def save_visual_design(self, visual_design: dict) -> None:
        """Persist Visual Design data through the active project session."""
        if self._session is None:
            raise ProjectWorkspaceError("No active project session")
        self._session.save_visual_design(visual_design)

    def inspect_project_integrity(self):
        """Run explicit read-only integrity checks for the active project."""
        if self._session is not None:
            project_root = self._session.project.root_path
            project_id = self._session.project_id
        elif self._existing_project_root is not None:
            project_root = self._existing_project_root
            project_id = None
        else:
            raise ProjectWorkspaceError("No active project session")
        from giscode.agentic.integrity import ProjectIntegrityInspector

        return ProjectIntegrityInspector(
            project_root,
            project_store=self._project_store if project_id else None,
            project_id=project_id,
        ).inspect()

    # ----------------------------------------------------------------- approve & start

    def approve_and_start(self, notes: Optional[list[str]] = None):
        """Approve the current plan and start the build.

        This is the *only* method that may cross the PLANNING -> BUILDING
        boundary. It delegates to the approval module (Task 9).
        """
        if self._session is None:
            raise ProjectWorkspaceError("No active project session")
        from giscode.projects.approval import approve_and_start
        try:
            return approve_and_start(
                self._project_store,
                self._session.project_id,
                notes=notes,
            )
        except Exception as exc:
            raise ProjectWorkspaceError(str(exc)) from exc

    def approve_and_start_build(self, notes: Optional[list[str]] = None) -> BuildStartResult:
        """Approve the plan and really start the existing coding pipeline.

        :meth:`approve_and_start` only crosses the approval boundary: it returns
        a :class:`BuildHandoff` and runs nothing. This method hands that same
        handoff to the existing
        :class:`giscode.projects.build_launcher.BuildLauncher`, which starts the
        application's existing ``CodingAgentLoop``.

        The launcher is built *before* approval, so a wiring problem leaves the
        project in PLANNING (retryable) rather than in a BUILDING state no work
        backs. A launch that fails after approval moves the project from
        BUILDING to a truthful terminal state and reports the real error.
        """
        if self._session is None:
            raise ProjectWorkspaceError("No active project session")
        project_id = self._session.project_id

        # One launch per project. The store's lifecycle also refuses a second
        # approval, but saying it here keeps the project in BUILDING and starts
        # no second thread.
        if self.build_in_flight(project_id):
            raise ProjectWorkspaceError(
                "a build is already running for this project; wait for it to "
                "stop before starting another"
            )

        # The root is checked before approval: a refused root must leave the
        # project in PLANNING (retryable), never in BUILDING with no work.
        self._validate_build_root(project_id)
        launcher = self._ensure_build_launcher()
        handoff = self.approve_and_start(notes)

        # Record "starting" before the worker exists. A build can finish before
        # ``launch()`` returns, and that real outcome must not be overwritten by
        # the initial state; the identity check below keeps whichever is newer.
        placeholder = BuildStartResult(
            status=BuildStatus.RUNNING,
            message="Approval completed; starting the build...",
            approved=True,
            launched=True,
            project_id=project_id,
        )
        self._set_build_state(project_id, placeholder)

        with self._build_lock:
            self._build_cancel_events[project_id] = threading.Event()
        try:
            launch = launcher.launch(handoff)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Build launch failed for project %s: %s", project_id, error
            )
            recovered = self._recover_build_failure(project_id, cancelled=False)
            message = (
                f"Approval completed, but the build could not be started: {error}."
            )
            if recovered:
                message += (
                    " The project was moved from BUILDING to FAILED; the plan and "
                    "project files were left unchanged."
                )
            else:
                message += (
                    " The project could not be moved out of BUILDING automatically;"
                    " check its state before retrying."
                )
            result = BuildStartResult(
                status=BuildStatus.LAUNCH_FAILED,
                message=message,
                approved=True,
                launched=False,
                project_id=project_id,
                error=error,
            )
            self._set_build_state(project_id, result)
            return result

        result = BuildStartResult(
            status=BuildStatus.RUNNING,
            message=(
                "Approval completed and the build is running "
                f"(task {launch.task_id})."
            ),
            approved=True,
            launched=True,
            project_id=project_id,
            task_id=launch.task_id,
        )
        with self._build_lock:
            if self._build_states.get(project_id) is placeholder:
                self._build_states[project_id] = result
        return result

    # ----------------------------------------------------------------- build state

    def _ensure_build_launcher(self):
        with self._build_lock:
            if self._build_launcher is not None:
                return self._build_launcher
        coding_agent = self._coding_agent
        if coding_agent is None:
            raise ProjectWorkspaceError(
                "No coding agent is available to run the approved plan, so the "
                "project was not approved and nothing was started"
            )
        from giscode.projects.build_launcher import BuildLauncher

        try:
            launcher = BuildLauncher(
                coding_agent,
                allowed_root=self._allowed_build_root(),
                allowed_roots=self._build_allowed_roots(),
                on_complete=self._on_build_complete,
                on_failure=self._on_build_failure,
            )
        except Exception as exc:
            raise ProjectWorkspaceError(
                "Could not create the build launcher: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        with self._build_lock:
            if self._build_launcher is None:
                self._build_launcher = launcher
            return self._build_launcher

    def _allowed_build_root(self) -> Optional[str]:
        """The configured projects folder the coding loop is rooted at.

        Taken from the loop itself, because that is the tree it would edit if
        a run were not re-rooted. An agent that exposes no workspace (a test
        double) keeps the historical permissive behaviour.
        """
        root = getattr(self._coding_agent, "workspace_root", None)
        return str(root) if root else None

    # ------------------------------------------------- external project roots

    def external_project_notice(self, root_path: "str | os.PathLike[str]") -> str:
        """The exact text shown before an external project is opened.

        Names the folder that will be modified and the projects folder that
        will not, so the answer is about a directory rather than a rule.
        """
        return external_root_notice(root_path, self._allowed_build_root())

    def is_external_project(self, root_path: str) -> bool:
        """Whether *root_path* is outside the configured projects folder."""
        return is_external_project_root(root_path, self._allowed_build_root())

    # Kept as the internal spelling used by the gates below; both ask the same
    # single question so a project can never be external for one and not the
    # other.
    _needs_external_confirmation = is_external_project

    def _confirm_external_root(self, root_path: "str | os.PathLike[str]") -> None:
        """Record the user's explicit selection of an external project root."""
        key = str(canonical_path(root_path))
        if key in self._confirmed_external_roots:
            return
        self._confirmed_external_roots.add(key)
        with self._build_lock:
            # The launcher snapshots the approved roots at construction, so a
            # newly approved folder must get a launcher that knows about it.
            self._build_launcher = None

    def _build_allowed_roots(self) -> list[str]:
        """Canonical external roots this session's user explicitly selected."""
        return sorted(self._confirmed_external_roots)

    def _authorise_stored_root(self, project_id: str, *, allow_external: bool) -> None:
        """Gate a stored project record on explicit external selection."""
        try:
            project = self._project_store.get_project(project_id)
        except Exception as exc:  # noqa: BLE001 - report, do not guess
            raise ProjectWorkspaceError(
                f"the project could not be read, so it was not opened: {exc}"
            ) from exc
        if project is None or not self._needs_external_confirmation(project.root_path):
            return
        try:
            usable = resolve_project_root(project.root_path)
        except RootNotAllowedError as exc:
            raise ProjectWorkspaceError(
                f"the project root cannot be used, so it was not opened: {exc}"
            ) from exc
        if not allow_external:
            raise ExternalProjectPermissionRequired(
                project_id, usable, self.external_project_notice(usable)
            )
        self._confirm_external_root(usable)

    def _validate_build_root(self, project_id: str) -> None:
        """Refuse a build whose project root cannot be used, before approval."""
        allowed = self._allowed_build_root()
        if allowed is None:
            return
        try:
            project = self._project_store.get_project(project_id)
        except Exception as exc:  # noqa: BLE001 - report, do not guess
            raise ProjectWorkspaceError(
                f"the project could not be read, so the build was refused: {exc}"
            ) from exc
        if project is None:
            raise ProjectWorkspaceError(
                "the project record is missing, so the build was refused"
            )
        try:
            resolve_project_root(
                project.root_path,
                allowed_root=allowed,
                allowed_roots=self._build_allowed_roots(),
            )
        except RootNotAllowedError as exc:
            raise ProjectWorkspaceError(
                f"the build was refused: {exc}"
            ) from exc

    def _on_build_failure(self, launch, exc) -> None:
        """Launcher callback (background thread): record, then recover."""
        project_id = getattr(launch, "project_id", None)
        error = f"{type(exc).__name__}: {exc}"
        logger.error("Build failed for project %s: %s", project_id, error)
        if not project_id:
            return
        recovered = self._recover_build_failure(project_id, cancelled=False)
        message = f"Build failed: {error}."
        if recovered:
            message += (
                " The project is now FAILED; its plan and files were left unchanged."
            )
        self._set_build_state(
            project_id,
            BuildStartResult(
                status=BuildStatus.FAILED,
                message=message,
                approved=True,
                launched=True,
                project_id=project_id,
                task_id=getattr(launch, "task_id", None),
                error=error,
            ),
        )

    def _on_build_complete(self, launch, result) -> None:
        """Launcher callback (background thread): report the real outcome."""
        project_id = getattr(launch, "project_id", None)
        if not project_id:
            return
        # The launcher has already dropped this project from its in-flight
        # table; verification happens after that. Mark it finishing first so a
        # caller asking "is this build still running?" keeps getting "yes"
        # until the lifecycle has actually moved.
        with self._build_lock:
            self._finishing.add(project_id)
        try:
            self._finish_build(project_id, launch, result)
        finally:
            with self._build_lock:
                self._finishing.discard(project_id)

    def _finish_build(self, project_id: str, launch, result) -> None:
        """Report, verify and settle one finished build."""
        raw_status = getattr(getattr(result, "status", None), "value", None)
        outcome = str(raw_status or getattr(result, "status", "") or "unknown")
        task_id = getattr(launch, "task_id", None)
        error = getattr(result, "error", None)

        if outcome == "cancelled":
            recovered = self._recover_build_failure(project_id, cancelled=True)
            status = BuildStatus.CANCELLED
            message = "Build cancelled."
            if recovered:
                message += (
                    " The project is now CANCELLED; its plan and files were left "
                    "unchanged."
                )
        elif outcome in {"failed", "timed_out"}:
            recovered = self._recover_build_failure(project_id, cancelled=False)
            status = BuildStatus.FAILED
            message = (
                f"Build failed ({outcome}): "
                f"{error or 'the coding loop reported no detail'}."
            )
            if recovered:
                message += (
                    " The project is now FAILED; its plan and files were left "
                    "unchanged."
                )
        else:
            changed = None
            getter = getattr(self._build_launcher, "changed_files", None)
            if callable(getter):
                try:
                    changed = getter(project_id)
                except Exception:  # noqa: BLE001 - unknown stays unknown
                    changed = None
            # A coding-loop handoff is not proof that the project builds. Run the
            # project's own checks on this worker thread and let their real
            # outcome decide the lifecycle.
            self._verify_completed_build(project_id, changed, task_id, outcome)
            return

        self._set_build_state(
            project_id,
            BuildStartResult(
                status=status,
                message=message,
                approved=True,
                launched=True,
                project_id=project_id,
                task_id=task_id,
                error=error,
            ),
        )

    def _recover_build_failure(self, project_id: str, *, cancelled: bool) -> bool:
        """Move BUILDING to a truthful terminal state after a failed build.

        Only ``BUILDING`` is touched, so a late callback can never rewrite a
        lifecycle that was already moved. The plan, the project record's other
        fields, and the project files are never modified here.
        """
        target = (
            ProjectLifecycle.CANCELLED
            if cancelled
            else ProjectLifecycle.FAILED
        )
        try:
            project = self._project_store.get_project(project_id)
            if project is None:
                return False
            if project.lifecycle == target:
                return True
            if project.lifecycle not in _IN_PROGRESS_STATES:
                return False
            if not can_transition(project.lifecycle, target):
                return False
            updated = self._project_store.transition_lifecycle(project_id, target)
            return updated is not None and updated.lifecycle == target
        except Exception:
            logger.exception(
                "Could not move project %s from BUILDING to %s after a build "
                "failure",
                project_id,
                target.name,
            )
            return False

    def _advance_build_stage(
        self, project_id: str, target: ProjectLifecycle
    ) -> bool:
        """Move the project one stage forward, or report why nothing moved."""
        try:
            project = self._project_store.get_project(project_id)
            if project is None:
                return False
            if project.lifecycle == target:
                return True
            if not can_transition(project.lifecycle, target):
                logger.warning(
                    "Cannot move project %s from %s to %s",
                    project_id,
                    project.lifecycle.name,
                    target.name,
                )
                return False
            updated = self._project_store.transition_lifecycle(project_id, target)
            return updated is not None and updated.lifecycle == target
        except Exception:
            logger.exception(
                "Could not move project %s to %s", project_id, target.name
            )
            return False

    def _verify_completed_build(
        self, project_id: str, changed, task_id, outcome: str
    ) -> None:
        """Verify a finished build and record what it actually proved.

        Runs on the launcher's worker thread. Only a passing project suite
        reaches COMPLETE; every other outcome names itself and ends FAILED
        (retryable) instead of leaving the project in BUILDING.
        """
        from giscode.agentic.verification_budget import VerificationBudget
        from giscode.projects.build_verification import (
            BuildVerificationOutcome,
            verify_completed_build,
        )

        error = ""
        try:
            project = self._project_store.get_project(project_id)
        except Exception as exc:  # noqa: BLE001 - reported, not guessed
            project = None
            error = f"{type(exc).__name__}: {exc}"

        if project is None:
            self._set_build_state(
                project_id,
                BuildStartResult(
                    status=BuildStatus.FAILED,
                    message=(
                        "Build finished, but the project record could not be read, "
                        "so verification did not run: "
                        + (error or "project not found")
                    ),
                    approved=True,
                    launched=True,
                    project_id=project_id,
                    task_id=task_id,
                    error=error or "project not found",
                ),
            )
            return

        if project.lifecycle not in _IN_PROGRESS_STATES:
            # A late callback must never rewrite a lifecycle that already moved.
            self._set_build_state(
                project_id,
                BuildStartResult(
                    status=BuildStatus.COMPLETED,
                    message=(
                        f"Build finished ({outcome}), but the project is already "
                        f"{project.lifecycle.name}, so verification was not started."
                    ),
                    approved=True,
                    launched=True,
                    project_id=project_id,
                    task_id=task_id,
                ),
            )
            return

        # The project really is being tested now; say so in the lifecycle before
        # the run starts, so the UI is never showing BUILDING during verification.
        self._advance_build_stage(project_id, ProjectLifecycle.TESTING)
        self._advance_build_stage(project_id, ProjectLifecycle.VERIFICATION)

        with self._build_lock:
            cancel_event = self._build_cancel_events.get(project_id)
        report = verify_completed_build(
            project.root_path,
            changed_files=changed,
            budget=VerificationBudget(max_repair_attempts=0),
            should_cancel=(cancel_event.is_set if cancel_event is not None else None),
        )
        with self._build_lock:
            self._verification_reports[project_id] = report
            self._build_cancel_events.pop(project_id, None)

        if report.outcome is BuildVerificationOutcome.VERIFIED:
            self._advance_build_stage(project_id, ProjectLifecycle.COMPLETE)
            stored = self._project_store.get_project(project_id)
            if stored is not None and stored.lifecycle == ProjectLifecycle.COMPLETE:
                status = BuildStatus.COMPLETED
                message = report.message
            else:
                status = BuildStatus.FAILED
                message = (
                    report.message
                    + " The project could not be moved to COMPLETE; check its state."
                )
        elif report.outcome is BuildVerificationOutcome.CANCELLED:
            self._recover_build_failure(project_id, cancelled=True)
            status = BuildStatus.CANCELLED
            message = report.message
        else:
            self._recover_build_failure(project_id, cancelled=False)
            status = BuildStatus.FAILED
            message = (
                report.message
                + " The project is FAILED; Retry Build reopens it for approval."
            )

        self._set_build_state(
            project_id,
            BuildStartResult(
                status=status,
                message=message,
                approved=True,
                launched=True,
                project_id=project_id,
                task_id=task_id,
                error=(report.message if status is BuildStatus.FAILED else None),
            ),
        )

    def build_verification(self, project_id: Optional[str] = None):
        """The verification report of a project's last finished build, if any."""
        pid = project_id or self.current_project_id
        if pid is None:
            return None
        with self._build_lock:
            return self._verification_reports.get(pid)

    def retry_build(self, project_id: Optional[str] = None) -> BuildStartResult:
        """Explicitly retry a failed build, through the store's own transition.

        Only ``FAILED`` and ``APPROVED`` are eligible, the store moves the
        project back to ``REVIEW``, and the normal approval path then crosses
        the boundary again. Nothing here mutates the lifecycle directly, and a
        running build (or a second concurrent retry) is refused.
        """
        if self._session is None:
            raise ProjectWorkspaceError("No active project session")
        pid = project_id or self._session.project_id
        if pid != self._session.project_id:
            raise ProjectWorkspaceError(
                "only the open project can be retried; open it first"
            )
        if self.build_in_flight(pid):
            raise ProjectWorkspaceError(
                "a build is already running for this project; wait for it to "
                "stop before retrying"
            )
        with self._build_lock:
            if pid in self._retrying:
                raise ProjectWorkspaceError(
                    "a retry is already in progress for this project"
                )
            self._retrying.add(pid)
        try:
            project = self._project_store.get_project(pid)
            if project is None:
                raise ProjectWorkspaceError(
                    "the project record is missing, so nothing was retried"
                )
            if project.lifecycle not in RETRYABLE_STATES:
                raise ProjectWorkspaceError(
                    f"the project is {project.lifecycle.name}; only a FAILED "
                    "project, or an APPROVED build that never started, can be "
                    "retried"
                )
            # The controlled operation: FAILED/APPROVED -> REVIEW.
            self._project_store.retry(pid)
            with self._build_lock:
                self._build_cancel_events.pop(pid, None)
            return self.approve_and_start_build()
        finally:
            with self._build_lock:
                self._retrying.discard(pid)

    def _set_build_state(self, project_id: str, result: BuildStartResult) -> None:
        with self._build_lock:
            self._build_states[project_id] = result

    def build_state(self, project_id: Optional[str] = None) -> Optional[BuildStartResult]:
        """The last known build outcome for a project, if any."""
        pid = project_id or self.current_project_id
        if pid is None:
            return None
        with self._build_lock:
            return self._build_states.get(pid)

    def build_in_flight(self, project_id: Optional[str] = None) -> bool:
        """Whether work for ``project_id`` is still running.

        True while the coding loop runs *and* while its completion callback is
        verifying the project, which happens after the launcher has removed the
        project from its own in-flight table.
        """
        pid = project_id or self.current_project_id
        if pid is None:
            return False
        with self._build_lock:
            if pid in self._finishing:
                return True
        launcher = self._build_launcher
        if launcher is None:
            return False
        try:
            return bool(launcher.is_in_flight(pid))
        except Exception:
            return False

    def cancel_build(self) -> bool:
        """Ask the running coding loop to stop.

        Returns whether a stop was requested. The real outcome arrives later
        through the launcher's completion callback, so the caller must not treat
        ``True`` as "the build stopped".
        """
        launcher = self._build_launcher
        pid = self.current_project_id
        if pid is None:
            return False
        # A completion callback that is already verifying this project is the
        # only owner of the work now; the loop itself has stopped. Ask it to
        # abandon the run instead of reporting "nothing to stop".
        with self._build_lock:
            finishing = pid in self._finishing
            finishing_event = self._build_cancel_events.get(pid)
        if finishing:
            if finishing_event is None:
                return False
            finishing_event.set()
            self._set_build_state(
                pid,
                BuildStartResult(
                    status=BuildStatus.RUNNING,
                    message=(
                        "Stop requested; project verification is being abandoned "
                        "and the build will be reported as cancelled."
                    ),
                    approved=True,
                    launched=True,
                    project_id=pid,
                    task_id=getattr(self.build_state(pid), "task_id", None),
                ),
            )
            return True
        if launcher is None:
            return False
        try:
            requested = bool(launcher.cancel(pid))
        except Exception:
            logger.exception("Could not cancel the build for project %s", pid)
            return False
        if requested:
            with self._build_lock:
                cancel_event = self._build_cancel_events.get(pid)
            if cancel_event is not None:
                # Verification runs on the launcher thread after the loop stops;
                # it reads this flag so a Stop Build also stops that work.
                cancel_event.set()
            previous = self.build_state(pid)
            self._set_build_state(
                pid,
                BuildStartResult(
                    status=BuildStatus.RUNNING,
                    message=(
                        "Stop requested; waiting for the coding loop to stop. No "
                        "file changes were made by this request."
                    ),
                    approved=True,
                    launched=True,
                    project_id=pid,
                    task_id=getattr(previous, "task_id", None),
                ),
            )
        return requested

    # ----------------------------------------------------------------- internal

    def _refresh_projects_list(self) -> None:
        pass

    def _refresh_all(self) -> None:
        pass


# ----------------------------------------------------------------- view


class ProjectWorkspaceView:
    """Tk widget for the Project Workspace tab.

    The view is *not* a Toplevel; it embeds inside the existing
    ``MainWindow`` "Projects" tab. It calls the controller and
    renders the result.
    """

    def __init__(
        self,
        parent: tk.Misc,
        controller: ProjectWorkspaceController,
    ) -> None:
        self._parent = ttk.Frame(parent)
        self._parent.pack(fill=tk.BOTH, expand=True)
        self._controller = controller

        self._build_ui()
        # A scheduled poll keeps a reference to this view and its Tk variables.
        # If the widget goes away first, cancel it: a callback that outlives the
        # view lets its variables be finalized on a worker thread, and Tk cannot
        # serve a call from there.
        self._parent.bind("<Destroy>", self._on_destroy)
        self._refresh_all()


    def pack(self, *args, **kwargs):
        """Keep the view compatible with callers treating it as a widget."""
        return self._parent.pack(*args, **kwargs)

    def _build_ui(self) -> None:
        # NOTE: this view deliberately does NOT build a project picker.
        #
        # It used to create its own Combobox plus "Start New Project" / "Open" /
        # "Close" / "Settings" buttons, which duplicated the project control
        # area that MainWindow already builds in the window header. The two
        # copies each owned a private StringVar, so a project picked in one was
        # invisible to the other and "Open" reported that nothing was selected.
        # The header is now the single authoritative project control area; this
        # view reflects that state read-only so the tab still shows which
        # project the workspace is bound to.
        top = ttk.Frame(self._parent, padding=8)
        top.pack(fill=tk.X)

        self._project_var = tk.StringVar()
        self._lifecycle_var = tk.StringVar(value="No active project")
        # Hand-off for the send path. The Projects tab has no chat input any
        # more: _on_plan_request copies the plan request line here and the
        # worker thread reads it, exactly as the removed chat prompt did.
        self._input_var = tk.StringVar()
        self._send_thread = None
        self._send_seq = 0
        self._plan_request_pending = False
        ttk.Label(top, text="Active project:").pack(side=tk.LEFT, padx=(0, 5))
        project_label = ttk.Label(top, textvariable=self._project_var)
        project_label.pack(side=tk.LEFT)
        make_value_copyable(project_label, self._project_var.get)
        ttk.Label(top, text="   |   State:").pack(side=tk.LEFT, padx=(10, 5))
        lifecycle_label = ttk.Label(top, textvariable=self._lifecycle_var)
        lifecycle_label.pack(side=tk.LEFT)
        make_value_copyable(lifecycle_label, self._lifecycle_var.get)


        self._workspace_tabs = ttk.Notebook(self._parent)
        self._workspace_tabs.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        planner_tab = ttk.Frame(self._workspace_tabs)
        self._workspace_tabs.add(planner_tab, text="Planer")

        from giscode.gui.visual_design import VisualDesignWorkspace

        self._visual_design_view = VisualDesignWorkspace(
            self._workspace_tabs, self._controller
        )
        self._workspace_tabs.add(self._visual_design_view, text="Visual Design")

        integrity_tab = ttk.Frame(self._workspace_tabs)
        self._workspace_tabs.add(integrity_tab, text="Integrity")
        self._integrity_view = ProjectIntegrityView(
            integrity_tab, self._controller
        )
        self._integrity_view.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            planner_tab,
            text=(
                "Chat runs tasks. This field authors the plan that Save Plan, "
                "Get Readiness and Approve & Start act on."
            ),
            foreground="gray",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))

        # The plan actions below operate on the Planer session's plan, which
        # only a Planer turn can author. The full chat pane (transcript +
        # prompt) is not packed any more, so without this one-line request box
        # Save Plan / Get Readiness / Approve & Start would act on a plan that
        # can never be built. This is a single field plus the Planer's answer,
        # not a second chat pane: it reuses the existing send path.
        plan_row = ttk.Frame(planner_tab)
        plan_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(plan_row, text="Plan request (planning only)").pack(side=tk.LEFT)
        self._plan_entry = ttk.Entry(plan_row)
        self._plan_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        self._plan_entry.bind("<Return>", self._on_plan_request)
        self._plan_button = ttk.Button(
            plan_row, text="Plan", command=self._on_plan_request
        )
        self._plan_button.pack(side=tk.LEFT)

        self._plan_reply_var = tk.StringVar(value="")
        plan_reply = ttk.Label(
            planner_tab,
            textvariable=self._plan_reply_var,
            wraplength=900,
            justify=tk.LEFT,
        )
        plan_reply.pack(anchor=tk.W, pady=(4, 0))
        make_value_copyable(plan_reply, self._plan_reply_var.get)

        # Bottom: actions
        action_frame = ttk.Frame(planner_tab)
        action_frame.pack(fill=tk.X, pady=(8, 0))

        self._save_plan_button = ttk.Button(
            action_frame, text="Save Plan", command=self._on_save_plan
        )
        self._save_plan_button.pack(side=tk.LEFT)
        self._readiness_button = ttk.Button(
            action_frame, text="Get Readiness", command=self._on_get_readiness
        )
        self._readiness_button.pack(side=tk.LEFT, padx=(4, 0))
        self._approve_button = ttk.Button(
            action_frame, text="Approve & Start", command=self._on_approve_and_start
        )
        self._approve_button.pack(side=tk.LEFT, padx=(4, 0))
        # Only meaningful while a launched build is running: it asks the same
        # coding loop the rest of the application uses to stop.
        self._stop_build_button = ttk.Button(
            action_frame, text="Stop Build", command=self._on_stop_build
        )
        self._stop_build_button.pack(side=tk.LEFT, padx=(4, 0))
        # Explicit retry for a failed build, or for an approved build whose
        # launch never started. It calls the controller's retry operation, which
        # goes through the store; it never edits the lifecycle itself.
        self._retry_button = ttk.Button(
            action_frame, text="Retry Build", command=self._on_retry_build
        )
        self._retry_button.pack(side=tk.LEFT, padx=(4, 0))
        self._action_hint_var = tk.StringVar(value="")
        ttk.Label(
            planner_tab,
            textvariable=self._action_hint_var,
            foreground="gray",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        self._build_poll_after = None
        self._last_build_status = None

        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(self._parent, textvariable=self._status_var).pack(anchor=tk.W, pady=(8, 0))
        self._update_plan_action_states()

    def show_active_project(
        self,
        label: str,
        current_id: Optional[str] = None,
        root_path: Optional[str] = None,
    ) -> None:
        """Render the authoritative selection from the main window.

        The main window owns project selection; this view only mirrors it so
        the Projects tab shows the same project the header has selected.
        """
        self._project_var.set(label or "No project selected")
        try:
            if current_id and self._controller.current_project_id != current_id:
                self._controller.open_project(current_id)
            elif not current_id:
                self._controller.select_existing_project_root(root_path)
        except Exception:
            logger.error("Failed to reflect active project", exc_info=True)
        self._visual_design_view.refresh()
        self._integrity_view.refresh()
        self._update_plan_action_states()
        # ``State:`` reads ``_lifecycle_var`` and is only written by
        # ``_refresh_readiness``. Without this the label kept the previous
        # value - typically ``NO_PROJECT`` - while the session was open and the
        # actions below it were live. Readiness is derived from the in-memory
        # plan, so this neither calls a model nor changes plan data.
        self._refresh_readiness()

    def _update_plan_action_states(self) -> None:
        """Enable only the plan actions the current state can actually serve.

        ``Plan``, ``Save Plan`` and ``Approve & Start`` all raise
        ``No active project session`` when no project is open, so leaving them
        clickable offered nothing but an error dialog. ``Get Readiness`` has no
        such prerequisite - it answers ``NO_PLAN`` without a session - so it
        stays enabled as the read-only status query it is.

        ``Approve & Start`` is additionally limited to PLANNING / REVIEW and
        disabled while a launched build is running; ``Stop Build`` is the only
        action offered in that state.
        """
        enabled = self._controller_has_session()
        in_flight = self._build_in_flight()
        self._set_button_enabled("_plan_button", enabled)
        self._set_button_enabled("_save_plan_button", enabled)
        # Approval is only legal from PLANNING / REVIEW, and only once: while a
        # build is in flight the project is BUILDING, so a second approve can
        # only raise. A controller whose lifecycle is unknown (a duck-typed
        # stand-in) keeps the old behaviour instead of being disabled.
        lifecycle = getattr(self._controller, "lifecycle", None)
        approvable = enabled and not in_flight
        if lifecycle is not None:
            approvable = approvable and lifecycle in (
                ProjectLifecycle.PLANNING,
                ProjectLifecycle.REVIEW,
            )
        # Retry is offered only where the store's controlled operation accepts
        # it: FAILED, or APPROVED with no build actually running.
        retryable = enabled and not in_flight
        if lifecycle is not None:
            retryable = retryable and lifecycle in RETRYABLE_STATES
        self._set_button_enabled("_approve_button", approvable)
        self._set_button_enabled("_retry_button", retryable)
        self._set_button_enabled("_stop_build_button", in_flight)
        hint = getattr(self, "_action_hint_var", None)
        if hint is not None:
            hint.set(self._action_hint(enabled, in_flight, lifecycle))
        # Keep polling while work is in progress even after the launcher is no
        # longer in flight: verification runs on the worker thread after that,
        # and its outcome is what changes the lifecycle and these buttons.
        pending = lifecycle is not None and lifecycle in _IN_PROGRESS_STATES
        if in_flight or pending:
            self._start_build_poll()

    def _set_button_enabled(self, attribute: str, enabled: bool) -> None:
        button = getattr(self, attribute, None)
        if button is None:
            return
        try:
            button.state(["!disabled" if enabled else "disabled"])
        except tk.TclError:
            pass

    @staticmethod
    def _action_hint(enabled: bool, in_flight: bool, lifecycle) -> str:
        """Why the plan actions are in the state they are in."""
        if not enabled:
            return "Open a project to use these actions."
        if in_flight:
            return (
                "A build is running. Stop Build is available; approval and retry "
                "wait until it finishes."
            )
        if lifecycle is None:
            return ""
        name = getattr(lifecycle, "name", str(lifecycle))
        if lifecycle in (ProjectLifecycle.PLANNING, ProjectLifecycle.REVIEW):
            return "Approve & Start runs this project's plan with the coding agent."
        if lifecycle == ProjectLifecycle.FAILED:
            return (
                "This build failed or could not be verified. Retry Build reopens "
                "it for approval; its plan and files are kept."
            )
        if lifecycle == ProjectLifecycle.APPROVED:
            return (
                "The build was approved but never started. Retry Build reopens it "
                "for approval."
            )
        if lifecycle == ProjectLifecycle.COMPLETE:
            return "The project is COMPLETE: its own tests passed verification."
        if lifecycle == ProjectLifecycle.CANCELLED:
            return "The project is CANCELLED; it cannot be approved or retried."
        return f"The project is {name}; it must finish before it can be retried."

    def _on_retry_build(self) -> None:
        """Retry through the controller's controlled operation."""
        retry = getattr(self._controller, "retry_build", None)
        if not callable(retry):
            self._status_var.set("This controller cannot retry builds.")
            return
        try:
            result = retry()
        except Exception as e:  # noqa: BLE001 - shown, not swallowed
            reason = str(e) or e.__class__.__name__
            # Readiness is rendered first: it writes the status line too, so
            # setting the refusal before it would immediately overwrite it.
            self._refresh_readiness()
            self._plan_reply_var.set(f"Retry refused: {reason}")
            self._status_var.set("Retry not started")
            self._update_plan_action_states()
            return

        message = getattr(result, "message", None) or "Retry requested."
        self._plan_reply_var.set(message)
        self._refresh_readiness()
        if getattr(result, "launched", False):
            self._status_var.set("Build running")
            self._start_build_poll()
        else:
            self._status_var.set("Retry did not start a build")
        self._update_plan_action_states()

    def _build_in_flight(self) -> bool:
        """Whether the controller reports a running build.

        Duck-typed controllers that predate the build handoff simply report
        ``False``, which keeps their previous behaviour.
        """
        checker = getattr(self._controller, "build_in_flight", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    def _refresh_projects_list(self) -> None:
        try:
            projects = self._controller.list_projects()
            current_id = self._controller.current_project_id
            current = next((p for p in projects if p.id == current_id), None)
            if current is not None:
                mode_label = "Visual" if current.mode == "visual_app_design" else "Traditional"
                self._project_var.set(f"{current.name} [{mode_label}] ({current.root_path})")
            elif not self._project_var.get():
                self._project_var.set("No project selected")
        except Exception as e:
            logger.error("Failed to refresh projects list", exc_info=True)

    def _refresh_all(self) -> None:
        self._refresh_projects_list()
        self._refresh_readiness()
        self._visual_design_view.refresh()
        self._integrity_view.refresh()
        self._update_plan_action_states()

    def _controller_has_session(self) -> bool:
        """Whether the controller reports an open project session.

        A controller that predates ``has_active_session`` (or any duck-typed
        stand-in) reports ``None`` here, and the original behaviour is kept:
        try to load the history and handle the failure.
        """
        reported = getattr(self._controller, "has_active_session", None)
        if reported is None:
            return True
        return bool(reported)

    def _refresh_readiness(self) -> None:
        try:
            readiness = self._controller.get_readiness()
            state = readiness.get("plan_readiness", "UNKNOWN")
            self._lifecycle_var.set(str(self._controller.lifecycle.name if self._controller.lifecycle else "NO_PROJECT"))
            self._status_var.set(f"Readiness: {state}")
        except Exception:
            self._lifecycle_var.set("NO_PROJECT")
            self._status_var.set("Ready")

    # ----------------------------------------------------------------- event handlers

    def _on_start_new(self) -> None:
        active_root = getattr(self._controller, "current_project_root", None)
        roots = [active_root] if active_root else []
        dialog = _StartProjectDialog(self._parent, existing_project_roots=roots)
        result = dialog.show()
        if result:
            name, path, mode = result
            try:
                try:
                    self._controller.create_project(
                        name=name, root_path=path, mode=mode
                    )
                except ExternalProjectPermissionRequired as exc:
                    if not messagebox.askyesno(
                        "External Project", exc.notice, parent=self._parent
                    ):
                        self._status_var.set(
                            "Project not created: the external folder was not "
                            "confirmed."
                        )
                        return
                    self._controller.create_project(
                        name=name,
                        root_path=path,
                        mode=mode,
                        allow_external=True,
                    )
                self._refresh_all()
                if self._controller.is_external_project(path):
                    self._status_var.set(
                        f"Project created: {name} (external folder "
                        f"{path}; only that folder will be modified)"
                    )
                else:
                    self._status_var.set(f"Project created: {name}")
            except Exception as e:
                messagebox.showerror("Start New Project", str(e), parent=self._parent)

    def _on_close(self) -> None:
        self._controller.close()
        self._refresh_all()
        self._status_var.set("Workspace closed.")

    # NOTE: the view used to define its own "_on_open_existing" and
    # "_on_settings" handlers, each with a private copy of the
    # "Select a project from the list first" message. Both were wired to the
    # duplicate picker that has been removed, and both read the *active*
    # project instead of the user's *selection*. Project open/settings now live
    # only in MainWindow, which owns the authoritative selection state and,
    # unlike this view, also republishes the active ProjectContext to the
    # GisAI engine. Only "_on_start_new" and "_on_close" remain here because
    # the header control area delegates to them.


    def _current_input_text(self) -> str:
        """The plan request text to send.

        There is no input widget here any more: the Projects tab authors a plan
        through ``_plan_entry``, which copies its text into ``_input_var`` and
        calls the same send path. ``_input_var`` is therefore the source of
        truth, and callers that drive ``_on_send_message`` directly (tests,
        scripted flows) set it the same way.
        """
        return self._input_var.get()

    def _on_send_message(self) -> None:
        msg = self._current_input_text().strip()
        if not msg:
            self._status_var.set("Nothing to send: the message box is empty.")
            return
        if getattr(self, "_send_thread", None) is not None and self._send_thread.is_alive():
            # Say why nothing happened, and keep the text so it is not lost.
            self._status_var.set("A message is already being sent. Press Stop to cancel it.")
            return
        self._input_var.set("")
        self._send_cancel = threading.Event()
        # Build the outcome queue here, on the Tk thread: the worker only
        # posts to it. _result_queue() creates the queue lazily, and when
        # that first happened on the worker thread the Queue/Condition
        # allocation triggered a garbage collection there, which finalized
        # a tkinter Variable on the worker thread. Variable.__del__ then
        # called Tcl off the main thread and the worker blocked forever, so
        # a computed answer was never delivered.
        self._result_queue()
        self._status_var.set("Planning...")
        # Every send gets its own id. A worker's outcome is only rendered by
        # the send that started it, so an abandoned answer can never answer a
        # later message (main_window keys its results by epoch for the same
        # reason).
        seq = int(getattr(self, "_send_seq", 0) or 0) + 1
        self._send_seq = seq
        self._send_thread = threading.Thread(
            target=self._send_worker, args=(msg, seq), daemon=True
        )
        self._send_thread.start()
        # The result is rendered from this thread; see _poll_send_result.
        self._schedule_send_poll(seq)

    def _result_queue(self) -> "queue.Queue[tuple]":
        """Where the send worker reports its outcome (created on first use).

        Some callers build a view with ``object.__new__`` and only the
        attributes the send path needs, so this cannot assume ``__init__``
        ran.
        """
        results = getattr(self, "_send_results", None)
        if results is None:
            results = queue.Queue()
            self._send_results = results
        return results

    def _schedule_send_poll(self, seq: int, delay_ms: int = 0) -> bool:
        """Ask the Tk thread to look for the queued outcome of send ``seq``.

        Only the main thread calls this, so the worker never touches Tk.
        """
        try:
            self._parent.after(delay_ms, lambda: self._poll_send_result(seq))
        except tk.TclError:
            # The interpreter went away with the window; the worker is a
            # daemon and its outcome is simply never rendered.
            return False
        return True

    def _send_worker(self, msg: str, seq: int = 0) -> None:
        """Run the request off the Tk thread and queue the outcome.

        The worker must not touch Tk. A cross-thread ``after(...)`` is
        marshalled to the main thread and blocks until the event loop
        services it, and it raises ``RuntimeError: main thread is not in
        main loop`` whenever the loop is not running. The old code swallowed
        that error, so a model answer that had already been computed was
        dropped and the pane stayed on "Planning..." forever. Reporting
        through a queue means the worker never depends on the loop, and the
        main thread renders the outcome whenever it next gets a turn.
        """
        try:
            response = self._controller.send_planer_message(msg)
            outcome = ("done", response, msg, seq)
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            outcome = ("error", error, msg, seq)
        self._result_queue().put(outcome)

    def _take_send_outcome(self):
        """Pop one queued outcome, or ``None`` when the queue is empty."""
        try:
            return self._result_queue().get_nowait()
        except queue.Empty:
            return None

    def _send_worker_alive(self) -> bool:
        thread = getattr(self, "_send_thread", None)
        return thread is not None and thread.is_alive()

    def _poll_send_result(self, seq: Optional[int] = None) -> None:
        """Render a queued worker outcome on the Tk thread.

        Scheduled by ``_on_send_message`` and rescheduled while the worker is
        still running, so a slow answer is picked up as soon as it arrives and
        a finished one is never left unrendered.

        The worker puts its outcome on the queue *before* its thread ends, so
        "the thread is gone" also means "everything it produced is visible".
        Reading the queue and checking liveness once each leaves a window in
        which the worker can land an outcome between the two, which would lose
        the answer; the second read below closes it.
        """
        current = int(getattr(self, "_send_seq", 0) or 0)
        if seq is None:
            seq = current
        if int(seq) != current:
            # A newer send owns the queue; this poller must not consume it.
            return
        while True:
            outcome = self._take_send_outcome()
            if outcome is None:
                if self._send_worker_alive():
                    self._schedule_send_poll(seq, 25)
                    return
                # The worker may have finished after the read above.
                outcome = self._take_send_outcome()
                if outcome is None:
                    if int(getattr(self, "_send_seq", 0) or 0) == int(seq):
                        self._send_thread = None
                    return
            kind, payload, msg, outcome_seq = outcome
            if int(outcome_seq) != int(seq):
                # An abandoned send's answer is dropped, not shown as this one's.
                continue
            if kind == "error":
                self._on_send_error(payload)
            else:
                self._on_send_done(payload, msg)
            return

    def _on_plan_request(self, _event=None):
        """Send this plan request through the reviewed send path.

        The text goes through ``_input_var`` into the same worker thread,
        cancel event and error surface used before; only the answer is shown
        here, in the reply line next to the plan actions.
        """
        text = (self._plan_entry.get() or "").strip()
        if not text:
            self._plan_reply_var.set("Nothing to plan: the plan request box is empty.")
            self._status_var.set("Nothing to plan: the plan request box is empty.")
            return "break"
        if not self._controller_has_session():
            message = "Open a project before planning."
            self._plan_reply_var.set(message)
            self._status_var.set(message)
            return "break"
        if getattr(self, "_send_thread", None) is not None and self._send_thread.is_alive():
            message = "A message is already being sent. Press Stop to cancel it."
            self._plan_reply_var.set(message)
            self._status_var.set(message)
            return "break"
        self._input_var.set(text)
        self._plan_request_pending = True
        self._plan_entry.delete(0, tk.END)
        self._on_send_message()
        return "break"

    def _publish_plan_reply(self, response) -> None:
        """Show the Planer's answer next to the plan actions."""
        self._plan_request_pending = False
        answer = (getattr(response, "assistant_message", "") or "").strip()
        if not answer:
            answer = (
                getattr(response, "image_error", "")
                or getattr(response, "inference_error", "")
                or getattr(response, "parse_error", "")
                or "The Planer returned no response."
            )
        if len(answer) > 600:
            answer = answer[:600].rstrip() + "..."
        self._plan_reply_var.set(answer)
        self._refresh_readiness()

    def _on_send_done(self, response, message: str = "") -> None:
        self._send_thread = None
        # The reply line is this tab's only output surface, so every answer is
        # published there - not just the ones a ``Plan`` press started.
        self._publish_plan_reply(response)
        if getattr(response, "success", False):
            self._status_var.set("Planer responded")
        else:
            # Report the real reason instead of claiming a response.
            reason = (
                getattr(response, "image_error", "")
                or getattr(response, "inference_error", "")
                or getattr(response, "parse_error", "")
                or "the request failed"
            )
            self._status_var.set(f"Planer could not answer: {reason}")

    def _on_send_error(self, error) -> None:
        """Report a failed send in the reply line, then in a dialog.

        The reason is recorded before anything modal, so the failure is still
        readable after the dialog is dismissed. The dialog itself is only
        raised when the window it parents to is on screen: a hidden or
        withdrawn view (as in the GUI tests) has nothing to parent to, and
        blocking there would hang it.
        """
        reason = str(error) or error.__class__.__name__
        if getattr(self, "_plan_request_pending", False):
            self._plan_request_pending = False
        self._plan_reply_var.set(f"Planer error: {reason}")
        self._status_var.set(f"Planer could not answer: {reason}")
        self._send_thread = None
        try:
            viewable = bool(self._parent.winfo_viewable())
        except Exception:
            viewable = False
        if viewable:
            messagebox.showerror("Planer", reason, parent=self._parent)

    def _cancel_send(self) -> None:
        cancel = getattr(self, "_send_cancel", None)
        if cancel is not None:
            cancel.set()
        thread = getattr(self, "_send_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _on_save_plan(self) -> None:
        try:
            self._controller.save_plan()
            self._status_var.set("Plan saved")
            self._plan_reply_var.set("Plan saved.")
        except Exception as e:
            self._plan_reply_var.set(f"Plan not saved: {e}")
            messagebox.showerror("Save Plan", str(e), parent=self._parent)

    def _on_get_readiness(self) -> None:
        try:
            readiness = self._controller.get_readiness()
            state = readiness.get("plan_readiness", "UNKNOWN")
            self._status_var.set(f"Readiness: {state}")
            self._plan_reply_var.set(f"Plan readiness: {state}")
        except Exception as e:
            self._plan_reply_var.set(f"Readiness unavailable: {e}")
            messagebox.showerror("Readiness", str(e), parent=self._parent)

    def _on_approve_and_start(self) -> None:
        start = getattr(self._controller, "approve_and_start_build", None)
        try:
            result = start() if callable(start) else self._controller.approve_and_start()
        except Exception as e:
            self._plan_reply_var.set(f"Could not approve the plan: {e}")
            messagebox.showerror("Approve & Start", str(e), parent=self._parent)
            return

        message = getattr(result, "message", None) or "Plan approved."
        status = getattr(result, "status", None)
        launched = bool(getattr(result, "launched", False))
        self._plan_reply_var.set(message)
        self._refresh_readiness()
        if not isinstance(result, BuildStartResult):
            # Legacy controller (or a duck-typed stand-in): approval returned a
            # handoff (or nothing) without raising, but nothing launched a build.
            self._status_var.set("Plan approved (no build started)")
            self._update_plan_action_states()
            return
        if launched:
            self._status_var.set("Build running")
            self._start_build_poll()
            self._update_plan_action_states()
            return

        # Approval completed but nothing was started. Never leave the old
        # "started" claim on screen.
        self._update_plan_action_states()
        if status == BuildStatus.LAUNCH_FAILED:
            self._status_var.set("Approval completed; build not started")
            try:
                viewable = bool(self._parent.winfo_viewable())
            except Exception:
                viewable = False
            if viewable:
                messagebox.showerror("Approve & Start", message, parent=self._parent)
        elif getattr(result, "approved", False):
            self._status_var.set("Plan approved (no build started)")
        else:
            self._status_var.set("Plan not approved")

    def _on_stop_build(self) -> None:
        cancel = getattr(self._controller, "cancel_build", None)
        if not callable(cancel):
            return
        try:
            requested = bool(cancel())
        except Exception as e:
            self._plan_reply_var.set(f"Could not stop the build: {e}")
            return
        if requested:
            self._plan_reply_var.set(
                "Stop requested; waiting for the coding loop to stop."
            )
            self._start_build_poll()
        else:
            self._plan_reply_var.set("No running build to stop.")
        self._update_plan_action_states()

    def _on_destroy(self, _event=None) -> None:
        """Cancel the pending build poll when this view is destroyed."""
        self._destroyed = True
        after_id = getattr(self, "_build_poll_after", None)
        self._build_poll_after = None
        if after_id is None:
            return
        try:
            self._parent.after_cancel(after_id)
        except Exception:
            pass

    def _start_build_poll(self) -> None:
        """Poll the controller's build state so the tab shows real outcomes.

        Launcher callbacks run on a worker thread, so the controller only stores
        the outcome; the Tk variables are written here, on the UI thread.
        """
        if getattr(self, "_destroyed", False):
            return
        if getattr(self, "_build_poll_after", None) is not None:
            return
        try:
            self._build_poll_after = self._parent.after(250, self._poll_build_state)
        except tk.TclError:
            self._build_poll_after = None

    def _poll_build_state(self) -> None:
        self._build_poll_after = None
        try:
            if not self._parent.winfo_exists():
                return
        except tk.TclError:
            return
        getter = getattr(self._controller, "build_state", None)
        if callable(getter):
            try:
                result = getter()
            except Exception:
                result = None
            if result is not None:
                status = getattr(result, "status", None)
                if status != self._last_build_status:
                    self._last_build_status = status
                    self._plan_reply_var.set(str(getattr(result, "message", "") or ""))
                    self._refresh_readiness()
                    label = getattr(status, "value", status)
                    self._status_var.set(f"Build: {label}")
        self._update_plan_action_states()


class ProjectIntegrityView(ttk.Frame):
    """Explicit read-only integrity inspection and report surface."""

    def __init__(self, parent: tk.Misc, controller: ProjectWorkspaceController) -> None:
        super().__init__(parent, padding=8)
        self._controller = controller
        self._active_project_root: Optional[str] = None
        # Structured result for the most recent inspection. Copy/Save render
        # from this model, never from the Text widget, so the exported text is
        # the complete report regardless of the visible viewport.
        self._current_report = None

        controls = ttk.Frame(self)
        controls.pack(fill=tk.X)
        self._inspect_button = ttk.Button(
            controls, text="Inspect project integrity", command=self._inspect
        )
        self._inspect_button.pack(side=tk.LEFT)
        self._copy_button = ttk.Button(
            controls, text="Copy Report", command=self._copy_report, state="disabled"
        )
        self._copy_button.pack(side=tk.LEFT, padx=(8, 0))
        self._save_button = ttk.Button(
            controls, text="Save Report…", command=self._save_report, state="disabled"
        )
        self._save_button.pack(side=tk.LEFT, padx=(8, 0))
        self._status_var = tk.StringVar(value="No active project")
        status_label = ttk.Label(controls, textvariable=self._status_var)
        status_label.pack(side=tk.LEFT, padx=(10, 0))
        make_value_copyable(status_label, self._status_var.get)

        report_frame = ttk.Frame(self)
        report_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._report_text = tk.Text(report_frame, wrap=tk.WORD, state=tk.DISABLED)
        scrollbar = ttk.Scrollbar(
            report_frame, orient=tk.VERTICAL, command=self._report_text.yview
        )
        self._report_text.configure(yscrollcommand=scrollbar.set)
        self._report_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        add_text_context_menu(self._report_text, editable=False)
        self.refresh()


    def refresh(self) -> None:
        """Clear stale report content when the active project changes."""
        project_root = getattr(self._controller, "current_project_root", None)
        if project_root != self._active_project_root:
            self._active_project_root = project_root
            self._current_report = None
            self._set_report("")
            self._status_var.set(
                "Ready to inspect active project" if project_root else "No active project"
            )
        self._inspect_button.configure(
            state="normal" if project_root else "disabled"
        )
        self._sync_report_buttons()

    def _sync_report_buttons(self) -> None:
        """Enable Copy/Save only when a completed report is available."""
        state = "normal" if self._current_report is not None else "disabled"
        self._copy_button.configure(state=state)
        self._save_button.configure(state=state)

    def _set_report(self, text: str) -> None:
        self._report_text.configure(state=tk.NORMAL)
        self._report_text.delete("1.0", tk.END)
        if text:
            self._report_text.insert("1.0", text)
        self._report_text.configure(state=tk.DISABLED)

    def _current_report_text(self) -> Optional[str]:
        """Render the complete report for the current structured result.

        The Text widget only shows a bounded slice of a large report, so Copy
        and Save deliberately rebuild the text from the stored
        ``ProjectIntegrityReport`` instead of reading the widget.
        """
        if self._current_report is None:
            return None
        from giscode.agentic.integrity import format_full_integrity_report

        return format_full_integrity_report(self._current_report)

    def _copy_report(self) -> None:
        """Copy the complete Integrity report to the Windows clipboard."""
        try:
            text = self._current_report_text()
        except Exception as exc:
            logger.exception("Could not build the Integrity report text to copy")
            self._status_var.set(f"Report copy failed: {type(exc).__name__}: {exc}")
            return
        if not text:
            self._status_var.set("No Integrity report to copy")
            return
        try:
            self._report_text.clipboard_clear()
            self._report_text.clipboard_append(text)
        except Exception as exc:
            logger.exception("Copying the Integrity report to the clipboard failed")
            self._status_var.set(f"Report copy failed: {type(exc).__name__}: {exc}")
            return
        self._status_var.set("Report copied")

    def _save_report(self) -> None:
        """Save the complete Integrity report as a user-chosen UTF-8 text file."""
        try:
            text = self._current_report_text()
        except Exception as exc:
            logger.exception("Could not build the Integrity report text to save")
            self._status_var.set(f"Report save failed: {type(exc).__name__}: {exc}")
            return
        if not text:
            self._status_var.set("No Integrity report to save")
            return
        # The native dialog owns the destination and its overwrite prompt; the
        # report is never written into the inspected project automatically.
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save Integrity Report",
            initialfile="GisCode_Integrity_Report.txt",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except Exception as exc:
            logger.exception("Saving the Integrity report failed")
            self._status_var.set(f"Report save failed: {type(exc).__name__}: {exc}")
            return
        self._status_var.set(f"Report saved: {path}")


    def _inspect(self) -> None:
        if not self._active_project_root:
            return
        self._status_var.set("Inspecting selected project…")
        try:
            self._set_report("Integrity inspection is in progress…")
            report = self._controller.inspect_project_integrity()
            from giscode.agentic.integrity import format_integrity_report

            rendered_report = format_integrity_report(report)
            self._set_report(rendered_report)
            self._current_report = report
            self._sync_report_buttons()
            self._status_var.set(f"{report.status} — {report.inspected_at}")
        except Exception as exc:
            logger.exception("Project integrity inspection or rendering failed")
            message = f"Inspection could not be completed: {type(exc).__name__}: {exc}"
            self._current_report = None
            self._sync_report_buttons()
            try:

                self._status_var.set(f"Inspection failed — {type(exc).__name__}: {exc}")
            except Exception:
                logger.exception("Could not display Project Integrity error status")
            try:
                self._set_report(message)
            except Exception:
                logger.exception("Could not render Project Integrity error details")


# ----------------------------------------------------------------- exports


__all__ = [
    "ProjectWorkspaceController",
    "ProjectWorkspaceView",
    "ProjectIntegrityView",
    "ProjectWorkspaceError",
    "PlanerChatUnavailableError",
    "ProjectStoreUnavailableError",
    "ProjectNotFoundError",
    "_StartProjectDialog",
    "_ProjectSettingsDialog",
    "_default_projects_root",
]
