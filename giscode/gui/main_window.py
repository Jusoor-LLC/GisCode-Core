from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
import contextlib
import mimetypes
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Optional

from giscode import edition
from giscode.logging.logger import get_logger
from giscode.ocr.types import OCRStatus
from giscode.projects import ProjectStore
from giscode.storage import ChatStore
from giscode.config.settings import SettingsManager
from giscode.gui.project_workspace import (
    ExternalProjectPermissionRequired,
    ProjectWorkspaceController,
    ProjectWorkspaceView,
)
from giscode.projects.workspace import ProjectWorkspaceError
from giscode.branding.logo import gis_logo_path, DEFAULT_LOGO_SMALL
from giscode.projects.context import ProjectContext, ProjectContextStore
from giscode.inference.errors import RequestCancelledError
from giscode.gui.task_report import build_change_summary, format_change_summary
from giscode.gui.text_interaction import (
    add_text_context_menu,
    make_value_copyable,
)
# giscode.developer_release and giscode.gui.gislogo_view are Pro-only. They are
# imported inside their builders so a Core launch never imports them.



APP_TITLE = "GisCode"
NAV_TABS = (
    "Chat", "Projects", "Terminal", "Settings", "Model Manager", "History",
    "Status", "iOS / xtool", "GisLogo", "Developer & Release",
    "Device Preview", "Generate Video", "Project Doctor",
    "Code Task", "Analyze", "Agent Registry",
)

ACTIVITY_READY = "Ready"
ACTIVITY_RUNNING_TESTS = "Running tests..."
ACTIVITY_COMPLETED = "Completed"
ACTIVITY_CANCELLED = "Cancelled"
ACTIVITY_FAILED = "Task failed"
ACTIVITY_VISION_UNAVAILABLE = "Vision unavailable"

# Capabilities that act on a project or workspace. A message that needs none
# of these is ordinary conversation and must never trigger the project
# location question.
PROJECT_SCOPED_CAPABILITIES = frozenset({
    "coding",
    "build",
    "testing",
    "terminal",
    "project_inspection",
    "directory_listing",
    "openhands",
})

# A path named in the task text: a drive-letter path, a UNC path or a rooted
# POSIX path. Used so a task that already says where it works needs no question.
_TASK_PATH_PATTERN = re.compile(
    r"""(?:
        [A-Za-z]:[\\/][^\s"'<>|?*]+
      | \\\\[^\s"'<>|?]+
      | (?<![\w.])/(?:[^\s"'<>|/]+/)*[^\s"'<>|/]+
    )""",
    re.VERBOSE,
)

# A quoted path is unambiguous even when it contains spaces, which the unquoted
# pattern above cannot express. Tried first so a path such as
# "C:\Users\me\Desktop\Sample project\SampleTasks" is never truncated at the
# first space and turned into "C:\Users\me\Desktop\Sample".
_QUOTED_TASK_PATH_PATTERN = re.compile(
    r"""["']([A-Za-z]:[\\/][^"']*|\\\\[^"']*|/[^"']*)["']"""
)

# A match is only a real path when the text does not continue it with another
# path segment ("...\Sample project\SampleTasks" truncated to "...\Sample").
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def _path_continues_after(message: str, end: int) -> bool:
    """True when the next word after a match is another path segment."""
    remainder = message[end:]
    sentence_end = _SENTENCE_END.search(remainder)
    if sentence_end:
        remainder = remainder[: sentence_end.start()]
    first_word = re.match(r"\s+(\S+)", remainder)
    if first_word is None:
        return False
    return bool(re.search(r"[\\/]", first_word.group(1)))


def project_path_in_text(message: str) -> Optional[str]:
    """Return the project path named in ``message``, or ``None``.

    A quoted absolute path is taken exactly. An unquoted match is used only
    when the text does not continue it with another path segment; otherwise the
    path is ambiguous, and the caller must ask instead of guessing (and
    creating) a folder.
    """
    if not message:
        return None
    for raw in _QUOTED_TASK_PATH_PATTERN.findall(message):
        candidate = raw.strip().rstrip(".,;:!?)]}'\"")
        if len(candidate) > 2 or candidate.count("/") > 1 or candidate.count("\\") > 1:
            return candidate
    for match in _TASK_PATH_PATTERN.finditer(message):
        candidate = match.group(0).rstrip(".,;:!?)]}'\"")
        # A bare "/" is a filesystem root, not a project location.
        if not (len(candidate) > 2 or candidate.count("/") > 1 or candidate.count("\\") > 1):
            continue
        if _path_continues_after(message, match.end()):
            return None
        return candidate
    return None


class _MultilineEntry(tk.Text):
    """Text input with Entry-compatible numeric indexes for old callers."""

    def _index(self, value):
        if value == 0:
            return "1.0"
        return value

    def get(self, index1="1.0", index2="end-1c"):
        return super().get(self._index(index1), self._index(index2) if index2 is not None else None)

    def delete(self, index1, index2=None):
        return super().delete(self._index(index1), self._index(index2) if index2 is not None else None)

    def insert(self, index, chars, *args):
        return super().insert(self._index(index), chars, *args)


class _SettingsBridge:
    """Adapts the window's settings helpers to services that want get/set.

    ``set`` raises when the settings source cannot save, so a service never
    reports a preference as stored when it was not.
    """

    def __init__(self, window) -> None:
        self._window = window

    def get(self, key, default=None):
        return self._window._settings_get(key, default)

    def set(self, key, value) -> None:
        if not self._window._settings_set(key, value):
            raise RuntimeError("settings are not writable")


class MainWindow:
    # Task clock budget. The wall-clock task timer used to stop real work at
    # 300s; the limit is now a setting (minutes) with a per-model override and
    # a larger default for big models, and user decision time is excluded.
    TASK_TIME_LIMIT_MINUTES = 15.0
    TASK_TIME_LIMIT_MINUTES_LARGE_MODEL = 30.0
    LARGE_MODEL_SIZE_BYTES = 8 * 1024 ** 3

    def __init__(self, root: tk.Tk, task_manager: Optional[object] = None, model_manager: Optional[object] = None, coding_agent: Optional[object] = None, terminal_executor: Optional[object] = None, sandbox_executor: Optional[object] = None, orchestrator: Optional[object] = None, openhands_adapter: Optional[object] = None, knowledge_service: Optional[object] = None, routing_service: Optional[object] = None, ocr_service: Optional[object] = None, monitoring_service: Optional[object] = None, packaging_builder: Optional[object] = None, signing_service: Optional[object] = None, self_improvement_service: Optional[object] = None, research_service: Optional[object] = None, vision_service: Optional[object] = None, project_store: Optional[object] = None, chat_store: Optional[object] = None, gisai_engine: Optional[object] = None, iosxtool_service: Optional[object] = None, developer_registry: Optional[object] = None, developer_workspace: Optional[object] = None, developer_audit: Optional[object] = None, developer_credential_store: Optional[object] = None, agent_registry_service: Optional[object] = None, agent_runtime_manager: Optional[object] = None):
        self.root = root
        self._update_window_title(self._compose_title(None, APP_TITLE))
        self.root.geometry("1400x900")
        # Below ~1 040 px the header squeezes and then silently drops its
        # controls: project Settings is gone under 960 px and has no other entry
        # point, Close/Open go at 860/780 px, the header Stop at 810 px, and the
        # Chat Send/Stop row collapses under 800 px. The project row needs
        # 1 019 px and the task row 871 px, so this keeps every control usable
        # without changing the layout, and 1 400 px still has room to spare.
        self.root.minsize(1100, 700)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.logger = get_logger("giscode.gui")
        self._last_title_state = (None, None)
        self._last_model_load_error = ""

        self._task_manager = task_manager
        self._model_manager = model_manager
        self._coding_agent = coding_agent
        self._terminal_executor = terminal_executor
        self._sandbox_executor = sandbox_executor
        self._orchestrator = orchestrator
        self._openhands_adapter = openhands_adapter
        self._knowledge_service = knowledge_service
        self._routing_service = routing_service
        self._ocr_service = ocr_service
        self._monitoring_service = monitoring_service
        self._packaging_builder = packaging_builder
        self._signing_service = signing_service
        self._self_improvement_service = self_improvement_service
        self._research_service = research_service
        self._vision_service = vision_service
        self._gisai_engine = gisai_engine
        if self._gisai_engine is not None and hasattr(self._gisai_engine, "set_edit_approval_callback"):
            self._gisai_engine.set_edit_approval_callback(self._approve_edit)
            coding_agent = getattr(self._gisai_engine, "_coding_agent", None)
            if coding_agent is not None and hasattr(coding_agent, "set_edit_approval_callback"):
                coding_agent.set_edit_approval_callback(
                    getattr(self._gisai_engine, "_approval_callback_for_run", self._approve_edit)
                )
        self._iosxtool_service = iosxtool_service
        self._developer_setup_error = ""
        # Pro-only services. Core leaves these as None and never imports the
        # developer_release package, so a Core install cannot reach a developer
        # surface by importing the window.
        self._developer_audit = None
        self._developer_credential_store = None
        self._developer_registry = None
        self._developer_workspace = None
        self._developer_controller = None
        if edition.feature_enabled("developer_release") and edition.implementation_available(
            "developer_release"
        ):
            self._init_developer_services(
                developer_registry,
                developer_workspace,
                developer_audit,
                developer_credential_store,
            )
        elif edition.feature_enabled("developer_release"):
            # Pro was asked for, but this tree has no Pro code: the tab explains
            # that instead of the window failing to start.
            self._developer_setup_error = (
                "GisCode Pro tools are not part of this build."
            )
        self._ios_xtool_view = None
        # Pro-only: the Agent Registry pane and the service behind it. Core
        # leaves both as None and never imports giscode.agent_registry, so a
        # Core install cannot reach the registry through the window.
        self._agent_registry_service = agent_registry_service
        # Pro-only: the managed runtime manager. Core leaves this None and the
        # pane has no start/stop controls to reach.
        self._agent_runtime_manager = agent_runtime_manager
        self._agent_registry_view = None
        self._ctx_store = ProjectContextStore()
        self._active_project_context = None
        self._external_project_root: Optional[str] = None
        self._task_controller = None
        self._active_task = False
        self._current_task_id = None
        self._current_task = None
        self._active_gisai_task = None
        self._current_task_epoch = 0
        self._task_epoch = 0
        self._task_start_time = 0.0
        # Declared before the first resolve: the limit resolver reads settings.
        self._settings_manager = None
        self._task_timeout_s = self._resolve_task_time_limit_s()
        self._task_deadline: Optional[ExecutionDeadline] = None
        self._approval_wait_depth = 0
        self._ui_state = "IDLE"
        self._send_thread = None
        self._worker_results = {}
        self._pending_statuses = {}
        self._model_switch_thread = None
        self._model_switch_epoch = 0
        self._model_switch_active = False
        self._model_switch_previous = "Auto"
        self._model_switch_results = queue.Queue()
        self._terminal_state_until = 0.0
        self._send_cancel = threading.Event()
        self._pending_approvals = []
        self._closing = False
        self.root.after(100, self._approval_poll_loop)
        self._last_report = ""
        self._last_work_result = None
        self._last_change_summary = None
        self._diff_viewer = None
        self._history_records = []
        self._history_detail = None
        self._history_diff_btn = None
        self._conversation_records = []
        self._conversation_history_listbox = None
        self._conversation_detail = None
        self._chat_sessions = []
        self._current_chat_index = 0

        # Create or use provided project/chat stores
        if project_store is not None:
            self._project_store = project_store
        elif model_manager is not None:
            self._project_store = ProjectStore()
        else:
            self._project_store = ProjectStore()

        if chat_store is not None:
            self._chat_store = chat_store
        else:
            self._chat_store = ChatStore()

        # Load existing daily chats from ChatStore
        self._load_daily_chats()

        # Backward compatibility attributes for existing tests
        self._project_workspace_controller = None
        # Authoritative project *selection* state. There is exactly one of
        # these in the app: the header project combobox writes it on
        # <<ComboboxSelected>> and every project action (Open / Switch /
        # Settings / Close) reads it. It is deliberately distinct from
        # ``_project_workspace_controller.current_project_id``, which is the
        # *active* project, not the user's current selection. Conflating the
        # two is what made "Open" claim nothing was selected after the user
        # had picked a project.
        self._selected_project_id: Optional[str] = None
        self.phase_var = tk.StringVar(value="Idle")
        self.status_var = tk.StringVar(value="No active task")
        self.elapsed_var = tk.StringVar(value="0.0s")
        self.model_name_var = tk.StringVar(value="None")
        self.model_state_var = tk.StringVar(value="Unloaded")
        self.agent_phase_var = tk.StringVar(value="Idle")
        self.agent_status_var = tk.StringVar(value="No active task")
        # GisAI lead status
        self.gisai_phase_var = tk.StringVar(value="Ready")
        self.gisai_status_var = tk.StringVar(value="Ready")
        self.terminal_status_var = tk.StringVar(value="Idle")
        self.terminal_elapsed_var = tk.StringVar(value="0.0s")
        self.sandbox_status_var = tk.StringVar(value="Unavailable")
        self.sandbox_backend_var = tk.StringVar(value="-")
        self.orchestration_phase_var = tk.StringVar(value="Idle")
        self.orchestration_attempt_var = tk.StringVar(value="0 / 0")
        self.openhands_status_var = tk.StringVar(value="Unavailable")
        self.openhands_runtime_var = tk.StringVar(value="-")
        self.knowledge_count_var = tk.StringVar(value="0 records")
        self.routing_role_var = tk.StringVar(value="-")
        self.routing_model_var = tk.StringVar(value="-")
        self.routing_status_var = tk.StringVar(value="-")
        self.ocr_status_var = tk.StringVar(value="Unavailable")
        self.monitoring_status_var = tk.StringVar(value="Disabled")
        self.packaging_status_var = tk.StringVar(value="Unconfigured")
        self.packaging_type_var = tk.StringVar(value="-")
        self.signing_status_var = tk.StringVar(value="Unsigned")
        self.self_improvement_status_var = tk.StringVar(value="Disabled")
        self.research_status_var = tk.StringVar(value="Disabled")
        self.chat_model_combo = None  # Will be set to _model_combo for compatibility
        self._status_var = tk.StringVar(value="Ready")
        if edition.is_pro() and edition.unavailable_pro_features():
            # Running Pro in a Core-only tree: say so instead of showing an
            # empty window with no explanation for the missing tools.
            self._status_var.set(edition.describe())
        self._model_status_var = tk.StringVar(value="No model")
        self._model_error_var = tk.StringVar(value="")
        self._model_note_var = tk.StringVar(value="")
        self._backend_note_var = tk.StringVar(value="")
        self.current_path_var = tk.StringVar(value="No project selected")
        self.current_name_var = tk.StringVar(value="No project")
        self.project_path_var = tk.StringVar(value="No active project")
        self.branding_var = tk.StringVar(value="Gis branding (default)")
        self.attach_info_var = tk.StringVar(value="")
        self.vision_status_var = tk.StringVar(value=ACTIVITY_READY)
        self._attach_strip_visible = False

        self._build_ui()
        if self._task_manager is not None:
            self._poll_task_state()
        if self._model_manager is not None:
            self._poll_model_state()
        if self._coding_agent is not None:
            self._poll_coding_agent_state()
        if self._gisai_engine is not None:
            self._poll_gisai_state()
        if self._terminal_executor is not None:
            self._poll_terminal_state()
        if self._sandbox_executor is not None:
            self._poll_sandbox_state()
        if self._orchestrator is not None:
            self._poll_orchestrator_state()
        if self._openhands_adapter is not None:
            self._poll_openhands_state()
        if self._knowledge_service is not None:
            self._poll_knowledge_state()
        if self._routing_service is not None:
            self._poll_routing_state()
        if self._ocr_service is not None:
            self._poll_ocr_state()
        if self._monitoring_service is not None:
            self._poll_monitoring_state()
        if self._packaging_builder is not None:
            self._poll_packaging_state()
        if self._signing_service is not None:
            self._poll_signing_state()
        if self._self_improvement_service is not None:
            self._poll_self_improvement_state()
        if self._research_service is not None:
            self._poll_research_state()

    def _load_daily_chats(self) -> None:
        """Load existing daily chats from ChatStore into memory."""
        try:
            daily_chats = self._chat_store.list_chats("daily", "")
            if daily_chats:
                self._chat_sessions = []
                for chat in daily_chats:
                    # Load messages for each chat
                    messages = self._chat_store.load_history(chat.id)
                    history = []
                    for msg in messages:
                        # Convert MessageRecord to history format
                        history.append({"role": msg.role, "content": msg.content})
                    self._chat_sessions.append({
                        "title": chat.title,
                        "chat_id": chat.id,
                        "text": "",  # Not used in new architecture
                        "history": history,
                        "draft": "",
                        "images": [],
                    })
                self._current_chat_index = 0
            else:
                # Keep the established Chat tab behavior: a first regular
                # conversation exists immediately, even before first send.
                self._chat_sessions = []
                self._current_chat_index = 0
                self._ensure_chat_session()
        except Exception as e:
            self.logger.error("Failed to load daily chats from ChatStore", exc_info=True)
            # Fallback to empty chat sessions
            self._chat_sessions = []
            self._current_chat_index = 0

    def _build_ui(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Exit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=file_menu)
        # Help is the discoverable entry point for the two things a user looks
        # for outside their project: how to support the work, and what this
        # build is. The About dialog repeats the support entry, so neither path
        # depends on knowing the other exists.
        self._help_menu = tk.Menu(menubar, tearoff=0)
        self._help_menu.add_command(
            label="Support the Developer", command=self._on_support_developer
        )
        self._help_menu.add_command(label="About GisCode", command=self._show_about)
        menubar.add_cascade(label="Help", menu=self._help_menu)
        self.root.config(menu=menubar)

        # Main container
        outer = ttk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True)

        # ===== HEADER BAR =====
        # Two compact rows instead of one overloaded line: the project row holds
        # everything about *which* project is open, the task row holds how the
        # assistant runs. Same widgets, same handlers, just readable.
        header = ttk.Frame(outer, padding=(10, 8))
        header.pack(fill=tk.X, side=tk.TOP)
        brand_row = ttk.Frame(header)
        brand_row.pack(fill=tk.X)
        task_row = ttk.Frame(header)
        task_row.pack(fill=tk.X, pady=(6, 0))

        # GisCode logo + branding
        try:
            logo_path = gis_logo_path(DEFAULT_LOGO_SMALL)
            if logo_path.is_file():
                self._logo_photo = tk.PhotoImage(file=str(logo_path))
                self._gis_logo_photo = self._logo_photo
                logo_label = ttk.Label(brand_row, image=self._logo_photo)
                logo_label.pack(side=tk.LEFT, padx=(0, 10))
        except Exception:
            self._gis_logo_photo = None

        ttk.Label(brand_row, text="GisCode", font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT, padx=(0, 20))

        # GisAI lead indicator
        self._gisai_header_label = ttk.Label(brand_row, text="GisAI — Ready", font=("Segoe UI", 10, "bold"), foreground="#0066CC")
        self._gisai_header_label.pack(side=tk.LEFT, padx=(0, 20))
        make_value_copyable(self._gisai_header_label)

        # ===== AUTHORITATIVE PROJECT CONTROL AREA =====
        # This header group is the single place project selection and the
        # project lifecycle actions live. It used to be duplicated by a second
        # picker inside the Projects tab (ProjectWorkspaceView._build_ui), which
        # owned a private StringVar and its own Open/Settings handlers. Two
        # independent selection states meant a project chosen in one control
        # was invisible to the other, so "Open" reported "Select a project from
        # the list first". The Projects tab now shows a read-only reflection of
        # this state instead of a second picker.
        ttk.Label(brand_row, text="Project:").pack(side=tk.LEFT, padx=(10, 5))
        self._project_var = tk.StringVar()
        self._project_combo = ttk.Combobox(brand_row, textvariable=self._project_var, state="readonly", width=35)
        self._project_combo.pack(side=tk.LEFT, padx=(0, 10))
        self._project_combo.bind("<<ComboboxSelected>>", self._on_project_selection)
        self._refresh_project_list()

        ttk.Button(brand_row, text="+ New Project", command=self._on_new_project).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(
            brand_row,
            text="Open Existing…",
            command=self._on_open_existing_project,
        ).pack(side=tk.LEFT, padx=(0, 5))
        # "Open", "Switch" and "Close" are no longer header controls. Activating
        # and closing a project are project actions, so they live with the
        # project workspace in the Projects tab; the header keeps selection and
        # creation. Both actions are still one click away and unchanged.
        ttk.Button(brand_row, text="Settings", command=self._on_project_settings).pack(side=tk.LEFT, padx=(0, 20))

        # Assistant mode selector
        ttk.Label(task_row, text="Mode:").pack(side=tk.LEFT, padx=(0, 5))
        self._assistant_mode_var = tk.StringVar(value="Assistant")
        assistant_combo = ttk.Combobox(task_row, textvariable=self._assistant_mode_var, state="readonly", width=18,
                                       values=["Assistant", "Planner", "Coder", "Reviewer"])
        assistant_combo.pack(side=tk.LEFT, padx=(0, 12))

        # Model selector
        ttk.Label(task_row, text="Model:").pack(side=tk.LEFT, padx=(0, 5))
        self._model_var = tk.StringVar(value="Auto" if self._model_manager is not None else "None")
        self._model_combo = ttk.Combobox(task_row, textvariable=self._model_var, state="readonly", width=22)
        self._model_combo.pack(side=tk.LEFT, padx=(0, 12))
        self._model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        self.chat_model_combo = self._model_combo  # For test compatibility
        self._refresh_model_list()

        # Action buttons. Model Manager stays in Core: choosing and switching
        # models is a Core feature. The three advanced actions are Pro-only.
        ttk.Button(task_row, text="Model Manager", command=self._on_model_manager).pack(side=tk.LEFT, padx=(0, 5))
        self.custom_logo_btn = None
        self.self_improvement_btn = None
        self.project_doctor_btn = None
        if edition.feature_enabled("self_improvement"):
            self.self_improvement_btn = ttk.Button(task_row, text="Self-Improvement", command=self._on_self_improvement)
            self.self_improvement_btn.pack(side=tk.LEFT, padx=(0, 5))
        if edition.feature_enabled("project_doctor"):
            self.project_doctor_btn = ttk.Button(task_row, text="Project Doctor", command=self._on_project_doctor)
            self.project_doctor_btn.pack(side=tk.LEFT, padx=(0, 5))
        if edition.feature_enabled("custom_logo"):
            self.custom_logo_btn = ttk.Button(task_row, text="Custom Logo", command=self._choose_custom_logo)
            self.custom_logo_btn.pack(side=tk.LEFT, padx=(0, 5))
        # Stop belongs to the running task, not to the project, so it sits at the
        # end of the task row where it cannot be mistaken for a project action.
        self.header_stop_btn = ttk.Button(task_row, text="Stop", command=self._revoke_task, state=tk.DISABLED)
        self.header_stop_btn.pack(side=tk.RIGHT, padx=(8, 0))

        # ===== MAIN CONTENT: NOTEBOOK =====
        self._notebook = ttk.Notebook(outer)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))

        # Tab frames. Order is the visible navigation order: the workspace the
        # user actually works in comes first, and the panes that only point at
        # another tab or have no backend are last instead of in the middle.
        self._tab_frames = {}
        # One registry decides what this edition contains (see giscode.edition),
        # so a surface cannot appear in Core because a builder was called
        # directly. Pro keeps every builder; Core simply never calls the rest.
        builders = {
            "Chat": self._create_chat_tab,
            "Projects": self._create_projects_tab,
            "Terminal": self._create_terminal_tab,
            "Settings": self._create_settings_tab,
            "Model Manager": self._create_model_manager_tab,
            "History": self._create_history_tab,
            "Status": self._create_status_tab,
            "iOS / xtool": self._create_ios_xtool_tab,
            "GisLogo": self._create_gislogo_tab,
            "Developer & Release": self._create_developer_release_tab,
            "Device Preview": self._create_device_preview_tab,
            "Generate Video": self._create_generate_video_tab,
            "Project Doctor": self._create_project_doctor_tab,
            "Code Task": self._create_code_task_tab,
            "Analyze": self._create_analyze_tab,
            "Agent Registry": self._create_agent_registry_tab,
            "AdSpace": self._create_adspace_tab,
        }
        for tab_name in edition.tabs():
            builder = builders.get(tab_name)
            if builder is None:
                continue
            try:
                builder()
            except ModuleNotFoundError as exc:
                # A public Core tree has no Pro modules. The edition setting is
                # a preference, not a licence, so an unavailable surface shows
                # an explanation instead of taking the whole window down.
                self.logger.warning("Tab %s is unavailable: %s", tab_name, exc)
                self._add_unavailable_tab(tab_name, exc)

        # Status bar at bottom
        status_bar = ttk.Frame(outer, relief=tk.SUNKEN, padding=(5, 2))
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        status_label = ttk.Label(status_bar, textvariable=self._status_var)
        status_label.pack(side=tk.LEFT)
        make_value_copyable(status_label, self._status_var.get)
        model_status_label = ttk.Label(status_bar, textvariable=self._model_status_var)
        model_status_label.pack(side=tk.RIGHT)
        make_value_copyable(model_status_label, self._model_status_var.get)

        # Land on the task workspace rather than whichever tab happened to be
        # built first. Everything else stays one click away in the strip.
        self._select_tab("Chat")

        self.logger.info("MainWindow initialized")
        self._bind_mousewheel()

    def _add_unavailable_tab(self, tab_name: str, error: Exception) -> None:
        """A tab whose implementation is not part of this build.

        Used only when a builder raises ``ModuleNotFoundError``; the tab still
        appears so a Pro user is told why it is empty rather than watching the
        application fail to start.
        """
        frame = ttk.Frame(self._notebook, padding=16)
        self._notebook.add(frame, text=tab_name)
        self._tab_frames[tab_name] = frame
        message = (
            f"{tab_name} is part of GisCode Pro and is not included in this "
            "build. Everything in GisCode Core keeps working; the Pro tools "
            "ship in the full edition."
        )
        ttk.Label(frame, text=message, wraplength=560, justify=tk.LEFT).pack(anchor=tk.W)
        detail = str(error).strip()
        if detail:
            ttk.Label(frame, text=detail, wraplength=560, justify=tk.LEFT).pack(
                anchor=tk.W, pady=(8, 0)
            )

    def _init_developer_services(
        self,
        developer_registry,
        developer_workspace,
        developer_audit,
        developer_credential_store,
    ) -> None:
        """Build the Pro-only developer/release services.

        Called only when the edition has ``developer_release``. The imports are
        local on purpose: Core must launch without loading this package.
        """
        from giscode.developer_release import (
            ApprovalManager,
            DeveloperReleaseController,
            DeveloperReleaseWorkspace,
            GooglePlayConsoleProvider,
            InMemoryAuditRecorder,
            ProviderRegistry,
            UnavailableCredentialStore,
            WindowsCredentialStore,
        )

        self._developer_audit = developer_audit or InMemoryAuditRecorder()
        if developer_credential_store is not None:
            self._developer_credential_store = developer_credential_store
        else:
            try:
                self._developer_credential_store = WindowsCredentialStore()
            except Exception as exc:
                self._developer_setup_error = str(exc)
                self._developer_credential_store = UnavailableCredentialStore()
        self._developer_registry = developer_registry or ProviderRegistry()
        if self._developer_registry.get("google_play_console") is None:
            self._developer_registry.register(
                GooglePlayConsoleProvider(self._developer_credential_store)
            )
        self._developer_workspace = developer_workspace or DeveloperReleaseWorkspace(
            "developer-workspace",
            self._developer_credential_store,
            ApprovalManager(),
            self._developer_audit,
        )
        self._developer_controller = DeveloperReleaseController(
            self._developer_registry,
            self._developer_workspace.approval_manager,
            self._developer_audit,
            permission_manager=self._developer_workspace.permissions,
        )

    def _create_developer_release_tab(self) -> None:
        from giscode.gui.developer_release_workspace import DeveloperReleaseWorkspaceView

        frame = ttk.Frame(self._notebook)
        self._notebook.add(frame, text="Developer & Release")
        self._tab_frames["Developer & Release"] = frame
        self._developer_release_view = DeveloperReleaseWorkspaceView(
            frame,
            registry=self._developer_registry,
            workspace=self._developer_workspace,
            controller=self._developer_controller,
            audit=self._developer_audit,
            credential_store=self._developer_credential_store,
        )

    def _canvas_bg(self) -> str:
        try:
            style = ttk.Style(self.root)
            return style.lookup("TFrame", "background")
        except Exception:
            return "SystemButtonFace"

    def _on_main_frame_configure(self, event=None) -> None:
        self._scroll_canvas.configure(scrollregion=self._scroll_canvas.bbox("all"))

    def _on_canvas_configure(self, event=None) -> None:
        if event is None or self._main_frame_window is None:
            return
        self._scroll_canvas.itemconfigure(self._main_frame_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        if self._scroll_canvas is not None:
            try:
                if getattr(event, "num", None) == 4:
                    self._scroll_canvas.yview_scroll(-1, "units")
                elif getattr(event, "num", None) == 5:
                    self._scroll_canvas.yview_scroll(1, "units")
                else:
                    self._scroll_canvas.yview_scroll(
                        -1 * (event.delta // 120), "units"
                    )
            except tk.TclError:
                pass

    def _bind_mousewheel(self) -> None:
        # Bind to notebook and its children
        if hasattr(self, '_notebook'):
            self._notebook.bind("<MouseWheel>", self._on_mousewheel)
            self._notebook.bind("<Button-4>", self._on_mousewheel)
            self._notebook.bind("<Button-5>", self._on_mousewheel)
        # Also bind to root for global scroll
        self.root.bind("<MouseWheel>", self._on_mousewheel)
        self.root.bind("<Button-4>", self._on_mousewheel)
        self.root.bind("<Button-5>", self._on_mousewheel)

    def _gather_scroll_widgets(self) -> list:
        widgets = []
        if hasattr(self, '_notebook'):
            stack = [self._notebook]
            while stack:
                widget = stack.pop()
                widgets.append(widget)
                stack.extend(widget.winfo_children())
        return widgets

    # ===== HELP / SUPPORT =====

    def help_menu_labels(self) -> list[str]:
        """Command labels of the Help menu, separators skipped."""
        last = self._help_menu.index("end")
        if last is None:
            return []
        return [
            self._help_menu.entrycget(index, "label")
            for index in range(last + 1)
            if self._help_menu.type(index) == "command"
        ]

    def _on_support_developer(self) -> None:
        """Open the owner's sponsorship page in the system browser.

        The page is an owner setting (``support.sponsors_url``) or the
        ``GISCODE_SPONSORS_URL`` environment variable. With none configured
        there is nothing to open, and inventing an account would send users to
        an address the owner never chose. Nothing is collected or charged here:
        the only action is handing a URL to the operating system's browser.
        """
        from giscode import support

        url = support.sponsors_url(
            lambda key, default=None: self._settings_get(key, default)
        )
        if not url:
            messagebox.showinfo(
                "Support the Developer", support.UNCONFIGURED_MESSAGE, parent=self.root
            )
            return
        if not support.open_sponsors(url):
            messagebox.showwarning(
                "Support the Developer",
                f"The browser could not be opened. The page is:\n{url}",
                parent=self.root,
            )

    def _show_about(self) -> tk.Toplevel:
        """The About dialog, whose support button is the second entry point."""
        from giscode import edition

        window = tk.Toplevel(self.root)
        window.title("About GisCode")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=APP_TITLE, font=("Segoe UI", 14, "bold")).pack(
            anchor=tk.W
        )
        ttk.Label(frame, text=f"Edition: {edition.edition()}").pack(
            anchor=tk.W, pady=(4, 0)
        )
        ttk.Label(
            frame,
            text="Runs on this machine. Your code and prompts stay here.",
        ).pack(anchor=tk.W, pady=(4, 12))
        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        support_button = ttk.Button(
            buttons,
            text="Support the Developer",
            command=self._on_support_developer,
        )
        support_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side=tk.RIGHT)
        self._about_window = window
        self._about_support_button = support_button
        return window

    # ===== HEADER HELPERS =====
    # What the header Project field shows when no project is active. It used
    # to be the "+ New Project…" placeholder, which wrongly implied that a
    # new project was pending while a project was actually active, and
    # misrepresented the real state at startup. The dedicated
    # "+ New Project" button remains the way to create a project.
    NO_PROJECT_LABEL = "None"
    # Kept for a stale/legacy selection: choosing it still starts a new
    # project, so no existing behaviour is lost.
    NEW_PROJECT_LABEL = "➕ New Project…"

    def _active_project_display_text(self) -> str:
        """Header text for the Project field, derived from real state.

        Returns the active project's label, or ``NO_PROJECT_LABEL`` when no
        project is open. It never returns a stale placeholder.
        """
        controller = getattr(self, "_project_workspace_controller", None)
        active_id = controller.current_project_id if controller is not None else None
        project = None
        if active_id and self._project_store is not None:
            try:
                project = self._project_store.get_project(active_id)
            except Exception:
                self.logger.debug("Could not resolve active project", exc_info=True)
        if project is not None:
            return self._project_label(project)
        if getattr(self, "_active_project_context", None) is not None:
            return self._existing_project_label(self._active_project_context.root_path)
        return self.NO_PROJECT_LABEL

    def _refresh_active_project_display(self) -> None:
        """Publish the authoritative active project into the header field."""
        try:
            self._project_var.set(self._active_project_display_text())
        except Exception:
            self.logger.error("Failed to refresh active project display", exc_info=True)


    def _project_label(self, project) -> str:
        mode_label = "Visual" if project.mode == "visual_app_design" else "Traditional"
        return f"{project.name} [{mode_label}] ({project.root_path})"

    @staticmethod
    def _existing_project_label(path: str) -> str:
        name = os.path.basename(os.path.normpath(path)) or path
        return f"{name} [Existing] ({path})"

    def _resolve_selected_project(self):
        """Resolve the authoritative selection to a project record.

        The selection is recorded by ``_on_project_selection`` as soon as the
        user picks from the header combobox, so this returns the project the
        user actually chose. The combobox label is used as a fallback so a
        selection restored programmatically (or a test that sets the combo
        directly) still resolves. Returns ``None`` when the selection is only
        the "New Project" placeholder.
        """
        if self._project_store is None:
            return None
        try:
            projects = self._project_store.list_projects()
        except Exception:
            self.logger.error("Failed to list projects", exc_info=True)
            return None

        for p in projects:
            if p.id == self._selected_project_id:
                return p

        selection = self._project_var.get()
        if selection and selection not in (self.NO_PROJECT_LABEL, self.NEW_PROJECT_LABEL):
            for p in projects:
                if self._project_label(p) == selection:
                    return p
        return None

    def _refresh_project_list(self) -> None:
        try:
            projects = self._project_store.list_projects()
            values = [self.NO_PROJECT_LABEL] + [self._project_label(p) for p in projects]
            external_root = self._external_project_root
            if external_root and not any(
                os.path.normcase(os.path.abspath(p.root_path))
                == os.path.normcase(os.path.abspath(external_root))
                for p in projects
            ):
                values.insert(1, self._existing_project_label(external_root))
            self._project_combo["values"] = values
            # Reflect the authoritative selection. A previous implementation
            # reset the combobox to the placeholder whenever its text was not
            # in the value list, which silently discarded the user's choice.
            selected = next(
                (p for p in projects if p.id == self._selected_project_id), None
            )
            if selected is not None:
                self._project_var.set(self._project_label(selected))
            elif external_root:
                self._project_var.set(self._existing_project_label(external_root))
            elif self._project_var.get() not in values:
                # No active project: show the real state, not a placeholder.
                self._refresh_active_project_display()
        except Exception as e:
            self.logger.error("Failed to refresh projects list", exc_info=True)

    def _sync_project_workspace_display(self) -> None:
        """Push the authoritative selection into the Projects tab display."""
        view = getattr(self, "_project_workspace_view", None)
        if view is None:
            return
        try:
            selected = self._resolve_selected_project()
            if selected is not None:
                view.show_active_project(
                    self._project_label(selected),
                    self._project_workspace_controller.current_project_id
                    if self._project_workspace_controller
                    else None,
                    getattr(self._active_project_context, "root_path", None),
                )
            elif self._active_project_context is not None:
                active_id = (
                    self._project_workspace_controller.current_project_id
                    if self._project_workspace_controller
                    else None
                )
                active_project = (
                    self._project_store.get_project(active_id) if active_id else None
                )
                view.show_active_project(
                    self._project_label(active_project)
                    if active_project is not None
                    else self._existing_project_label(self._active_project_context.root_path),
                    active_id,
                    self._active_project_context.root_path,
                )
            else:
                view.show_active_project(self.NO_PROJECT_LABEL, None, None)
        except Exception:
            self.logger.error("Failed to sync project workspace display", exc_info=True)

    def _on_new_project(self) -> None:
        """Create a project from the single project control area."""
        view = getattr(self, "_project_workspace_view", None)
        if view is not None:
            previous_id = (
                self._project_workspace_controller.current_project_id
                if self._project_workspace_controller
                else None
            )
            view._on_start_new()
            self._activate_created_project(previous_id)
        self._sync_project_workspace_display()

    def _activate_created_project(self, previous_id: Optional[str]) -> bool:
        controller = self._project_workspace_controller
        if controller is None:
            return False
        project_id = controller.current_project_id
        if not project_id or project_id == previous_id:
            return False
        project = self._project_store.get_project(project_id)
        if project is None:
            return False
        self._selected_project_id = project.id
        self._select_project_path(project.root_path, project_kind="new")
        self._refresh_active_project_display()
        self._refresh_project_list()
        self._status_var.set(f"Project created: {project.name}")
        return True

    def _on_open_existing_project(self) -> None:
        """Explicitly select an existing directory without initializing it."""
        path = filedialog.askdirectory(
            title="Open Existing Project", parent=self.root, mustexist=True
        )
        if not path:
            return
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            messagebox.showerror(
                "Open Existing Project",
                "Select an existing project folder.",
                parent=self.root,
            )
            return
        try:
            self._selected_project_id = None
            self._select_project_path(path, project_kind="existing")
            self._refresh_active_project_display()
            self._refresh_project_list()
            self._sync_project_workspace_display()
            self._status_var.set(f"Existing project opened: {path}")
        except Exception as exc:
            messagebox.showerror("Open Existing Project", str(exc), parent=self.root)

    def _on_open_project(self) -> None:
        project = self._resolve_selected_project()
        if project is None:
            from tkinter import messagebox
            messagebox.showinfo(
                "Open Project",
                "Select a project from the list first.",
                parent=self.root,
            )
            return
        try:
            # Record the selection first: the project is now both selected and
            # active, so subsequent actions agree on the target.
            self._selected_project_id = project.id
            if self._project_workspace_controller is not None:
                self._open_workspace(project)
            self._select_project_path(project.root_path)
            self._refresh_active_project_display()
            self._refresh_project_list()
            self._sync_project_workspace_display()
            self._status_var.set(f"Project opened: {project.name}")
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror("Open Project", str(e), parent=self.root)

    def _open_workspace(self, project) -> None:
        """Open *project*'s workspace, confirming an external folder first.

        A project outside the configured projects folder is supported, but the
        user must accept the exact directory GisCode will modify. The notice
        comes from the controller, so the text is the same one the tests pin,
        and a declined prompt opens nothing.
        """
        controller = self._project_workspace_controller
        if controller is None:
            return
        from tkinter import messagebox

        try:
            controller.open_project(project.id)
        except ExternalProjectPermissionRequired as exc:
            if not messagebox.askyesno(
                "External Project", exc.notice, parent=self.root
            ):
                raise ProjectWorkspaceError(
                    "the project was not opened: its external folder was not "
                    "confirmed"
                ) from exc
            controller.open_project(project.id, allow_external=True)

    def _on_switch_project(self) -> None:
        # Switching to another project is the same operation as opening one;
        # both activate the selection and republish the active context.
        self._on_open_project()

    def _on_close_project(self) -> None:
        self._close_project()
        self._selected_project_id = None
        self._refresh_active_project_display()
        self._refresh_project_list()
        self._sync_project_workspace_display()
        self._status_var.set("Project closed.")

    def _on_project_selection(self, _event=None) -> None:
        selection = self._project_var.get()
        if (
            self._external_project_root
            and selection == self._existing_project_label(self._external_project_root)
        ):
            self._selected_project_id = None
            self._sync_project_workspace_display()
            return
        if not selection or selection == self.NO_PROJECT_LABEL:
            # The "None" entry means "no project". Close whatever is open
            # instead of starting a new project: creating one is the job of
            # the dedicated "+ New Project" button.
            self._selected_project_id = None
            self._close_project()
            self._refresh_active_project_display()
            self._refresh_project_list()
            self._sync_project_workspace_display()
            self._status_var.set("No project selected.")
            return
        if selection == self.NEW_PROJECT_LABEL or selection.startswith("➕"):
            self._selected_project_id = None
            self._sync_project_workspace_display()
            view = getattr(self, "_project_workspace_view", None)
            if view is not None:
                previous_id = (
                    self._project_workspace_controller.current_project_id
                    if self._project_workspace_controller
                    else None
                )
                view._on_start_new()
                if not self._activate_created_project(previous_id):
                    if previous_id:
                        previous = self._project_store.get_project(previous_id)
                        if previous is not None:
                            self._selected_project_id = previous.id
                            self._project_var.set(self._project_label(previous))
                    self._refresh_project_list()
                    self._sync_project_workspace_display()
                    return
                self._refresh_project_list()
                project = self._project_store.get_project(
                    self._project_workspace_controller.current_project_id
                )
                if project is not None:
                    self._project_var.set(self._project_label(project))
                    self._sync_project_workspace_display()
            return
        # Record the selection immediately so Open / Switch / Settings act on
        # the project the user actually picked, instead of inferring it later.
        project = self._resolve_selected_project()
        if project is not None:
            self._selected_project_id = project.id
        self._sync_project_workspace_display()

    def _on_project_settings(self) -> None:
        # Get the project id from the authoritative selection, falling back to
        # the active project. Previously this read only
        # ``current_project_id`` and so rejected a valid selection.
        if self._project_workspace_controller is None:
            from tkinter import messagebox
            messagebox.showinfo(
                "Project Settings",
                "No project workspace available.",
                parent=self.root,
            )
            return
        current_id = self._selected_project_id
        if not current_id:
            selected = self._resolve_selected_project()
            current_id = selected.id if selected is not None else None
        if not current_id:
            current_id = self._project_workspace_controller.current_project_id
        if not current_id:
            from tkinter import messagebox
            messagebox.showinfo(
                "Project Settings",
                "Select a project from the list first.",
                parent=self.root,
            )
            return
        from giscode.gui.project_workspace import _ProjectSettingsDialog
        dlg = _ProjectSettingsDialog(self.root, self._project_store, current_id)
        result = dlg.show()
        if result is None:
            return  # Cancelled
        if not result.get("changed"):
            return  # No changes
        # Refresh the project list to show updated name
        self._refresh_project_list()
        self._sync_project_workspace_display()
        self._status_var.set(f"Project settings saved: {result['name']}")

    def _refresh_model_list(self) -> None:
        if self._model_manager is not None:
            try:
                list_registered = getattr(self._model_manager, "list_registered", None)
                models = list_registered() if callable(list_registered) else self._model_manager.get_registry().list_all()
                names = ["Auto"] + [m.name for m in models]
                self._model_combo["values"] = names
                if self._model_var.get() not in names:
                    self._model_var.set("Auto")
            except Exception:
                pass

    def _on_model_manager(self) -> None:
        self._select_tab("Model Manager")

    def _on_self_improvement(self) -> None:
        if not edition.feature_enabled("self_improvement"):
            self._pro_only_notice("Self-Improvement")
            return
        self._select_tab("Settings")
        self._status_var.set("Self-Improvement status is shown in Settings")

    def _on_project_doctor(self) -> None:
        if not edition.feature_enabled("project_doctor"):
            self._pro_only_notice("Project Doctor")
            return
        self._select_tab("Project Doctor")

    def _pro_only_notice(self, surface: str) -> None:
        """Say plainly that a surface is not part of this edition.

        Core never builds the control, so this is the second line of defence for
        a command or a stale callback rather than the usual path.
        """
        self._status_var.set(f"{surface} is part of GisCode Pro.")
        messagebox.showinfo(
            "GisCode Pro",
            f"{surface} is part of GisCode Pro, which keeps GisCode Core simple "
            "and free. It is not available in this edition.",
        )

    def _select_tab(self, name: str) -> None:
        frame = self._tab_frames.get(name)
        if frame is not None:
            self._notebook.select(frame)

    # ===== CONVERSATION HISTORY =====
    def _conversation_label(self, chat) -> str:
        if chat.scope == "daily":
            kind = "Regular"
        elif chat.scope.startswith("project:"):
            project_id = chat.scope.split(":", 1)[1]
            project = self._project_store.get_project(project_id)
            kind = f"Project: {project.name}" if project is not None else f"Project: {project_id}"
        else:
            kind = chat.scope
        return f"{chat.title}  |  {kind}  |  {chat.updated_at}"

    def _refresh_conversation_history(self) -> None:
        if self._conversation_history_listbox is None:
            return
        try:
            self._conversation_records = self._chat_store.list_all_chats()
            self._conversation_history_listbox.delete(0, tk.END)
            for chat in self._conversation_records:
                self._conversation_history_listbox.insert(tk.END, self._conversation_label(chat))
        except Exception:
            self._conversation_records = []

    def _selected_conversation(self):
        if self._conversation_history_listbox is None:
            return None
        selected = self._conversation_history_listbox.curselection()
        if not selected or selected[0] >= len(self._conversation_records):
            return None
        return self._conversation_records[selected[0]]

    def _show_conversation_detail(self, _event=None) -> None:
        chat = self._selected_conversation()
        if chat is None or self._conversation_detail is None:
            return
        history = self._chat_store.load_history(chat.id)
        detail = "\n\n".join(f"{message.role}: {message.content}" for message in history)
        self._conversation_detail.configure(state=tk.NORMAL)
        self._conversation_detail.delete("1.0", tk.END)
        self._conversation_detail.insert("1.0", detail or "No messages saved.")
        self._conversation_detail.configure(state=tk.DISABLED)

    def _open_conversation(self) -> None:
        chat = self._selected_conversation()
        if chat is None:
            return
        if chat.scope == "daily":
            index = next(
                (i for i, session in enumerate(self._chat_sessions) if session.get("chat_id") == chat.id),
                None,
            )
            if index is None:
                history = [
                    {"role": message.role, "content": message.content}
                    for message in self._chat_store.load_history(chat.id)
                ]
                self._chat_sessions.append({
                    "title": chat.title,
                    "chat_id": chat.id,
                    "text": "",
                    "history": history,
                    "draft": "",
                    "images": [],
                })
                index = len(self._chat_sessions) - 1
            self._current_chat_index = index
            self._render_chat_tabs()
            self._render_current_chat()
            self._select_tab("Chat")
            return

        if chat.scope.startswith("project:"):
            project_id = chat.scope.split(":", 1)[1]
            try:
                # The conversation names this project; opening it is the user's
                # explicit selection of that project's folder.
                self._project_workspace_controller.open_project(
                    project_id, allow_external=True
                )
                self._project_workspace_view._refresh_all()
                self._select_tab("Projects")
            except Exception as exc:
                self.logger.warning("Failed to open project conversation: %s", exc)

    def _rename_selected_conversation(self) -> None:
        chat = self._selected_conversation()
        if chat is None:
            return
        new_title = simpledialog.askstring(
            "Rename Conversation",
            "Enter new title:",
            initialvalue=chat.title,
            parent=self.root,
        )
        if not new_title or not new_title.strip():
            return
        updated = self._chat_store.rename_chat(chat.id, new_title.strip())
        if updated is not None:
            for session in self._chat_sessions:
                if session.get("chat_id") == updated.id:
                    session["title"] = updated.title
            self._render_chat_tabs()
            self._refresh_conversation_history()

    def _delete_selected_conversation(self) -> None:
        chat = self._selected_conversation()
        if chat is None:
            return
        if not messagebox.askyesno(
            "Delete Conversation",
            f"Delete '{chat.title}'? This only removes the saved conversation.",
            parent=self.root,
        ):
            return
        if self._chat_store.delete_chat(chat.id):
            self._chat_sessions = [
                session for session in self._chat_sessions if session.get("chat_id") != chat.id
            ]
            if self._chat_sessions:
                self._current_chat_index = min(self._current_chat_index, len(self._chat_sessions) - 1)
            else:
                self._current_chat_index = 0
            self._render_chat_tabs()
            self._render_current_chat()
            self._refresh_conversation_history()

    def _new_history_conversation(self) -> None:
        self._select_tab("Chat")
        self._new_chat()

    # ===== CHAT / TASK HELPERS =====
    def _current_chat_session(self) -> dict:
        # Ensure there's always at least one chat session
        if not self._chat_sessions:
            self._ensure_chat_session()
        if self._current_chat_index >= len(self._chat_sessions):
            self._current_chat_index = max(0, len(self._chat_sessions) - 1)
        return self._chat_sessions[self._current_chat_index]

    def _ensure_chat_session(self) -> None:
        """Ensure there's at least one chat session, creating one if needed."""
        if not self._chat_sessions:
            try:
                chat = self._chat_store.create_chat("daily", "", "Chat 1")
                self._chat_sessions.append({
                    "title": "Chat 1",
                    "chat_id": chat.id,
                    "text": "", "history": [], "draft": "", "images": [],
                })
                self._current_chat_index = 0
            except Exception as e:
                self.logger.warning(f"Failed to create default chat session: {e}")
                # Fallback to in-memory session
                self._chat_sessions.append({
                    "title": "Chat 1",
                    "chat_id": None,
                    "text": "", "history": [], "draft": "", "images": [],
                })
                self._current_chat_index = 0

    def _sync_draft(self, _event=None) -> None:
        self._current_chat_session()["draft"] = self.task_entry.get("1.0", "end-1c")

    def _render_chat_tabs(self) -> None:
        for child in self._chat_tabs_frame.winfo_children():
            child.destroy()
        for index, session in enumerate(self._chat_sessions):
            tab_frame = ttk.Frame(self._chat_tabs_frame)
            tab_frame.pack(side=tk.LEFT, padx=(0, 3))
            ttk.Button(
                tab_frame,
                text=session["title"],
                command=lambda i=index: self._switch_chat(i),
            ).pack(side=tk.LEFT)
            # Rename button
            ttk.Button(
                tab_frame,
                text="✎",
                width=2,
                command=lambda i=index: self._rename_chat(i),
            ).pack(side=tk.LEFT, padx=(2, 0))
            if len(self._chat_sessions) > 1:
                ttk.Button(
                    tab_frame,
                    text="x",
                    width=2,
                    command=lambda i=index: self._close_chat(i),
                ).pack(side=tk.LEFT, padx=(2, 0))

    def _render_current_chat(self) -> None:
        session = self._current_chat_session()
        self.response_text.config(state=tk.NORMAL)
        self.response_text.delete("1.0", tk.END)
        for item in session.get("history", []):
            role = item.get("role", "user")
            content = item.get("content", "")
            self.response_text.insert(tk.END, f"{role}: {content}\n")
            if role == "assistant" and content:
                button = ttk.Button(
                    self.response_text,
                    text="Copy",
                    command=lambda value=content: self._copy_chat_response(value),
                )
                self.response_text.window_create(tk.END, window=button)
                self.response_text.insert(tk.END, "\n")
            self.response_text.insert(tk.END, "\n")
        self.response_text.config(state=tk.NORMAL)
        self.response_text.see(tk.END)
        self.task_entry.delete("1.0", tk.END)
        self.task_entry.insert("1.0", session.get("draft", ""))
        self.attach_info_var.set(self._attachment_label(session.get("images", [])))
        self._attach_strip_visible = bool(session.get("images", []))

    def _new_chat(self) -> None:
        self._save_chat_session()
        # Create new chat in ChatStore with "daily" scope
        chat = self._chat_store.create_chat("daily", "", f"Chat {len(self._chat_sessions) + 1}")
        self._chat_sessions.append({
            "title": chat.title,
            "chat_id": chat.id,
            "text": "", "history": [], "draft": "", "images": [],
        })
        self._current_chat_index = len(self._chat_sessions) - 1
        self._render_chat_tabs()
        self._render_current_chat()

    def _switch_chat(self, index: int) -> None:
        if index < 0 or index >= len(self._chat_sessions):
            return
        if self._active_task:
            self._cancel_before_switch()
        self._save_chat_session()
        self._current_chat_index = index
        self._render_chat_tabs()
        self._render_current_chat()

    def _close_chat(self, index: int) -> None:
        if len(self._chat_sessions) == 1:
            return
        # Optionally delete from ChatStore
        session = self._chat_sessions[index]
        chat_id = session.get("chat_id")
        if chat_id:
            try:
                self._chat_store.delete_chat(chat_id)
            except Exception as e:
                self.logger.warning(f"Failed to delete chat from ChatStore: {e}")
        self._chat_sessions.pop(index)
        self._current_chat_index = min(self._current_chat_index, len(self._chat_sessions) - 1)
        self._render_chat_tabs()
        self._render_current_chat()

    def _rename_chat(self, index: int) -> None:
        """Rename a chat session."""
        if index < 0 or index >= len(self._chat_sessions):
            return
        session = self._chat_sessions[index]
        chat_id = session.get("chat_id")
        if not chat_id:
            return
        
        # Show rename dialog
        new_title = simpledialog.askstring("Rename Chat", "Enter new title:", initialvalue=session["title"])
        if not new_title or not new_title.strip():
            return
        new_title = new_title.strip()
        
        # Update in ChatStore
        try:
            updated_chat = self._chat_store.rename_chat(chat_id, new_title)
            if updated_chat:
                session["title"] = updated_chat.title
                self._render_chat_tabs()
        except Exception as e:
            self.logger.warning(f"Failed to rename chat in ChatStore: {e}")
            from tkinter import messagebox
            messagebox.showerror("Rename Failed", f"Failed to rename chat: {e}")

    def _save_chat_session(self) -> None:
        if hasattr(self, "task_entry"):
            draft = self.task_entry.get("1.0", "end-1c")
            session = self._current_chat_session()
            session["draft"] = draft
            # Persist draft to ChatStore (title is already set)
            chat_id = session.get("chat_id")
            if chat_id:
                try:
                    # We only persist draft locally; title changes would go through rename
                    pass
                except Exception as e:
                    self.logger.warning(f"Failed to save chat session to ChatStore: {e}")

    def _append_history(self, role: str, content: str) -> None:
        session = self._current_chat_session()
        session.setdefault("history", []).append({"role": role, "content": content})
        session["text"] = "\n".join(
            item["content"] for item in session["history"] if item["role"] == "assistant"
        )
        # Persist message to ChatStore
        chat_id = session.get("chat_id")
        if chat_id:
            try:
                self._chat_store.append_message(chat_id, role, content)
            except Exception as e:
                self.logger.warning(f"Failed to append message to ChatStore: {e}")
        self._render_current_chat()

    def _append_response(self, content: str) -> None:
        self._append_history("assistant", content)

    def _copy_chat_response(self, content: str) -> None:
        try:
            self.response_text.clipboard_clear()
            self.response_text.clipboard_append(content)
            self._status_var.set("Copied to clipboard")
        except tk.TclError as exc:
            self._status_var.set(f"Copy failed: {exc}")

    def _add_text_context_menu(self, text_widget) -> None:
        """Add right-click context menu to a text widget with Cut, Copy, Paste, Select All.

        Delegates to the shared :mod:`giscode.gui.text_interaction` helper so
        every GisCode text area behaves the same way.
        """
        add_text_context_menu(text_widget)
        text_widget.bind("<Control-A>", lambda e: text_widget.tag_add(tk.SEL, "1.0", tk.END))

    def _current_model(self):
        """Best-effort handle on the loaded model, or None.

        Prefers the inference API's raw ``_model`` (it still exists while
        ``get_model_status`` reports nothing), then the manager's own view.
        """
        manager = self._model_manager
        if manager is None:
            return None
        inference = getattr(manager, "_inference", None)
        model = getattr(inference, "_model", None) if inference is not None else None
        if model is not None:
            return model
        getter = getattr(manager, "get_current_model", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return None

    # ===== TASK TIME LIMIT =====

    def _settings_source(self):
        """Settings reader for GUI-facing limits, or None when unavailable.

        Production passes the real ``SettingsManager`` into the model manager,
        so that instance is preferred; otherwise one is created lazily against
        the active ``GISCODE_HOME`` profile. Never raises.
        """
        manager = self._model_manager
        settings = getattr(manager, "_settings", None) if manager is not None else None
        if settings is not None and callable(getattr(settings, "get", None)):
            return settings
        if self._settings_manager is None:
            try:
                self._settings_manager = SettingsManager()
            except Exception:
                self._settings_manager = False
        return self._settings_manager or None

    def _settings_get(self, key: str, default):
        settings = self._settings_source()
        if settings is None:
            return default
        try:
            value = settings.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _settings_set(self, key: str, value) -> bool:
        """Persist one settings key. Never raises; False means not saved."""
        settings = self._settings_source()
        setter = getattr(settings, "set", None) if settings is not None else None
        if not callable(setter):
            return False
        try:
            setter(key, value)
            return True
        except Exception:
            return False

    def _model_size_bytes(self) -> int:
        """Real size of the loaded model file, or 0 when unknown.

        Catalog entries ship ``size_bytes=0``, so the on-disk GGUF size is the
        fallback that actually distinguishes a 4B model from a 27B one.
        """
        model = self._current_model()
        if model is None:
            return 0
        try:
            size = int(getattr(model, "size_bytes", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            return size
        path = str(getattr(model, "path", "") or "")
        if not path:
            return 0
        try:
            return int(os.path.getsize(path))
        except OSError:
            return 0

    def _model_override_names(self) -> list:
        """Names a per-model override may be keyed by: runtime name and canonical."""
        model = self._current_model()
        name = str(getattr(model, "name", "") or "") if model is not None else ""
        if not name:
            return []
        names = [name]
        manager = self._model_manager
        registry = None
        if manager is not None:
            getter = getattr(manager, "get_registry", None)
            if callable(getter):
                try:
                    registry = getter()
                except Exception:
                    registry = None
            if registry is None:
                registry = getattr(manager, "_registry", None)
        resolver = getattr(registry, "resolve_canonical_name", None)
        if callable(resolver):
            try:
                canonical = resolver(name)
            except Exception:
                canonical = None
            canonical_name = str(getattr(canonical, "name", "") or "")
            if canonical_name and canonical_name not in names:
                names.append(canonical_name)
        return names

    @staticmethod
    def _coerce_minutes(value) -> Optional[float]:
        try:
            minutes = float(value)
        except (TypeError, ValueError):
            return None
        return minutes if minutes > 0 else None

    def _task_time_limit_minutes(self) -> float:
        """Task clock budget in minutes.

        Resolution order: an explicit per-model-name override, then the large
        model default when the loaded model file exceeds 8 GB, then the plain
        setting, then the built-in 15 minutes.
        """
        overrides = self._settings_get("task.time_limit_overrides", {})
        if isinstance(overrides, dict) and overrides:
            names = [n.lower() for n in self._model_override_names()]
            lowered = {str(k).lower(): v for k, v in overrides.items()}
            # Exact name first, then the registry's own identity rule, so a
            # key written as "Qwen3-VL-4B-Instruct" still matches the runtime
            # identity "Qwen3-VL-4B-Instruct-Q4_K_M".
            for name in names:
                minutes = self._coerce_minutes(lowered.get(name))
                if minutes is not None:
                    return minutes
            for key, value in lowered.items():
                minutes = self._coerce_minutes(value)
                if minutes is None:
                    continue
                for name in names:
                    if key in name or name in key:
                        return minutes
        if self._model_size_bytes() > self.LARGE_MODEL_SIZE_BYTES:
            minutes = self._coerce_minutes(self._settings_get(
                "task.large_model_time_limit_minutes", self.TASK_TIME_LIMIT_MINUTES_LARGE_MODEL,
            ))
            if minutes is not None:
                return minutes
        minutes = self._coerce_minutes(self._settings_get(
            "task.time_limit_minutes", self.TASK_TIME_LIMIT_MINUTES,
        ))
        return minutes if minutes is not None else self.TASK_TIME_LIMIT_MINUTES

    def _resolve_task_time_limit_s(self) -> float:
        return self._task_time_limit_minutes() * 60.0

    def _task_limit_minutes_label(self) -> str:
        minutes = self._task_timeout_s / 60.0
        return str(int(minutes)) if float(minutes).is_integer() else f"{minutes:g}"

    def _task_clock_expired(self) -> bool:
        """True once the task clock has consumed its budget (paused time excluded)."""
        deadline = getattr(self, "_task_deadline", None)
        if deadline is None:
            return False
        try:
            return bool(deadline.expired())
        except Exception:
            return False

    @contextlib.contextmanager
    def _task_clock_paused(self):
        """Exclude time the user spends deciding on an approval from the task clock.

        Mirrors the engine's own approval pause (``engine._run_with_approval_pause``)
        so a 10-minute approval dialog cannot consume the task budget. Nested
        waits resume the clock only when the outermost one finishes.
        """
        deadline = getattr(self, "_task_deadline", None)
        if deadline is None:
            yield
            return
        deadline.pause()
        self._approval_wait_depth = getattr(self, "_approval_wait_depth", 0) + 1
        try:
            yield
        finally:
            self._approval_wait_depth = max(0, getattr(self, "_approval_wait_depth", 1) - 1)
            if self._approval_wait_depth == 0:
                deadline.resume()

    def _get_health(self) -> dict:
        """Snapshot of model health used to decide whether Send may proceed.

        When the GisAi engine runs a hosted writer provider, its health wins:
        no local model is involved and ``model_loaded`` reflects key presence.
        Otherwise prefers the model manager's own ``get_health`` (the
        GUI-facing API), then falls back to the inference API it wraps, then
        to the loaded model itself. Never raises: an unreadable health source
        just means "nothing loaded".
        """
        engine = getattr(self, "_gisai_engine", None)
        engine_inference = getattr(engine, "_inference", None)
        if getattr(engine_inference, "is_writer_provider", False) is True:
            getter = getattr(engine_inference, "get_health", None)
            if callable(getter):
                try:
                    health = getter()
                except Exception:
                    health = None
                if isinstance(health, dict) and "model_loaded" in health:
                    return health
        manager = self._model_manager
        if manager is None:
            return {"model_loaded": False, "model_state": "unloaded"}
        for source in (manager, getattr(manager, "_inference", None)):
            getter = getattr(source, "get_health", None) if source is not None else None
            if callable(getter):
                try:
                    health = getter()
                except Exception:
                    health = None
                if isinstance(health, dict) and "model_loaded" in health:
                    return health
        current = self._current_model()
        if current is None:
            return {"model_loaded": False, "model_state": "unloaded"}
        state = getattr(getattr(current, "state", None), "value", "ready")
        return {"model_loaded": True, "model_state": state}

    def _model_is_loading(self) -> bool:
        """True while a model load is in flight, so Send can wait instead of
        firing a request into a half-initialized engine."""
        if getattr(self, "_model_switch_active", False):
            return True
        model = self._current_model()
        if model is None:
            return False
        return getattr(getattr(model, "state", None), "value", "") == "loading"

    def _send_task(self, _event=None) -> str:
        message = self.task_entry.get("1.0", "end-1c").strip()
        if not message:
            return "break"
        if self._active_task:
            self._append_response("A task is already running. Wait for it to finish or press Stop.")
            return "break"
        # Core is a complete product on its own: with no GisAI engine it still
        # dispatches the deterministic operations it owns, so a missing engine
        # is a mode, not a readiness failure. Only the engine-backed path needs
        # a model manager and a loaded model.
        core_only = self._gisai_engine is None
        if self._model_manager is None and not core_only:
            # Never fail silently: say why Send did nothing.
            self._append_response(
                "Cannot send: GisCode is not ready yet. The AI engine or model "
                "manager is still starting. Wait a moment and press Send again."
            )
            self._set_ui_state("IDLE")
            return "break"
        if not core_only:
            if self._model_is_loading():
                self._append_response(
                    "The model is still loading. Wait for it to finish, then press Send again."
                )
                self._set_ui_state("IDLE")
                return "break"
            health = self._get_health()
            if not health.get("model_loaded"):
                if health.get("hosted"):
                    self._append_response(
                        "[Hosted writer not ready] The hosted writer is enabled but "
                        "its API key is missing. Set the GISCODE_HOSTED_API_KEY "
                        "environment variable and restart GisCode."
                    )
                else:
                    self._append_response(
                        "[No model loaded] Open the Model Manager, load a model, "
                        "then press Send again."
                    )
                self._set_ui_state("IDLE")
                return "break"
        if not self._resolve_task_project(message):
            return "break"

        attachments = self._build_attachments(self._current_chat_session().get("images", []))
        self.task_entry.delete("1.0", tk.END)
        self._current_chat_session()["draft"] = ""
        self._append_history("user", message)
        self._active_task = True
        self._task_epoch += 1
        epoch = self._task_epoch
        self._current_task_epoch = epoch
        self._task_start_time = time.time()
        # Resolve the budget per task: the loaded model (and therefore the
        # applicable limit) can change between two messages.
        self._task_timeout_s = self._resolve_task_time_limit_s()
        from giscode.agentic.execution_deadline import ExecutionDeadline

        self._task_deadline = ExecutionDeadline(self._task_timeout_s)
        self._set_ui_state("WORKING")
        if self._task_manager is not None:
            try:
                self._task_manager.start_task("gisai-task", max_duration=self._task_timeout_s)
            except Exception:
                pass
        self._send_thread = threading.Thread(
            target=self._run_task,
            args=(message, attachments, epoch),
            daemon=True,
        )
        self._send_thread.start()
        self.root.after(25, lambda: self._drain_worker_result(epoch))
        return "break"

    def _task_requires_project(self, message: str) -> bool:
        """True when ``message`` is a coding/project task needing a project.

        Uses the engine's own intent detection, so ordinary conversation
        (which resolves to ``general_reasoning``) is never treated as a
        project task.
        """
        engine = self._gisai_engine
        if engine is None:
            # Core has no intent classifier. A named file, or an explicit
            # create/write request, is enough to know a workspace is needed;
            # anything else is left to the dispatcher's honest explanation.
            from giscode.agentic.core_tasks import request_needs_workspace

            return request_needs_workspace(message)
        detector = getattr(engine, "_detect_intent", None)
        if not callable(detector):
            return False
        try:
            intent = detector(message)
        except Exception:
            # Never let classification failure block the user's message.
            return False
        capabilities = (
            intent.get("required_capabilities")
            or intent.get("capabilities")
            or []
        )
        if capabilities:
            return bool(PROJECT_SCOPED_CAPABILITIES.intersection(capabilities))
        return (intent.get("intent") or intent.get("name")) in PROJECT_SCOPED_CAPABILITIES

    def _resolve_task_project(self, message: str) -> bool:
        """Make sure a project context is set, asking only when it is needed.

        * a project already selected -> use it, ask nothing
        * a path named in the task text -> use it directly, ask nothing
        * ordinary chat, or a task needing no project -> ask nothing
        * a coding/project task with no path -> ask existing-or-new
        """
        if self._active_project_context is not None:
            return True

        named_path = project_path_in_text(message)
        if named_path:
            resolved = os.path.abspath(os.path.expanduser(named_path))
            project_kind = "existing" if os.path.isdir(resolved) else "new"
            try:
                if project_kind == "new":
                    Path(resolved).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._append_response(
                    f"Cannot use the project path in your message ({resolved}): {exc}"
                )
                return False
            self._select_project_path(resolved, project_kind=project_kind)
            return True

        if not self._task_requires_project(message):
            # Plain conversation needs no project, so ask nothing.
            return True

        if not self._request_task_workspace():
            self._append_response(
                "Task cancelled: no project was selected. Your message is still "
                "in the box, so you can send it again once a project is chosen."
            )
            return False
        return True

    def _request_task_workspace(self) -> bool:
        """Ask existing-vs-new first, then collect only the path needed."""
        choice = messagebox.askyesnocancel(
            "Project Type",
            "Is this task for an existing project, or a new project?\n\n"
            "Yes = an existing project (you will be asked for its folder)\n"
            "No = a new project (you will be asked where to create it)\n"
            "Cancel = cancel this task",
            parent=self.root,
        )
        if choice is None:
            return False
        existing = bool(choice)

        if existing:
            prompt = (
                "Enter the path of the existing project folder.\n\n"
                "The folder must already exist."
            )
            title = "Existing Project Path"
        else:
            prompt = (
                "Where should the new project be created?\n\n"
                "The folder is created only after you confirm."
            )
            title = "New Project Location"

        raw_path = simpledialog.askstring(title, prompt, parent=self.root)
        if not raw_path or not raw_path.strip():
            return False

        path = os.path.abspath(os.path.expanduser(raw_path.strip()))

        if existing:
            if not os.path.isdir(path):
                messagebox.showerror(
                    title,
                    "The existing project path must be an existing folder.",
                    parent=self.root,
                )
                return False
            self._select_project_path(path, project_kind="existing")
            return True

        if not messagebox.askyesno(
            title,
            f"Create the new project folder here?\n\n{path}",
            parent=self.root,
        ):
            return False
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                title,
                f"The new project/files destination could not be created: {exc}",
                parent=self.root,
            )
            return False
        self._select_project_path(path, project_kind="new")
        return True

    @staticmethod
    def _waiting_input(task) -> bool:
        """``GisAiPhase.WAITING_INPUT`` by name, so core never imports GisAI."""
        state = getattr(task, "state", None)
        return (
            getattr(state, "name", None) == "WAITING_INPUT"
            or state == "waiting_input"
        )

    def _run_task(self, message, attachments, epoch: int) -> None:
        if self._gisai_engine is None:
            self._run_core_task(message, epoch)
            return
        try:
            task = self._gisai_engine.receive(
                message,
                attachments=attachments,
                project_context=self._active_project_context,
            )
            if self._waiting_input(task):
                self._worker_results[epoch] = (None, None, epoch, task)
                return
            result = self._gisai_engine.execute(task, on_status=lambda message: self._on_task_status(message, epoch))
            self._worker_results[epoch] = (result, None, epoch, task)
        except Exception as exc:
            self._worker_results[epoch] = (None, exc, epoch, None)

    def _run_core_task(self, message, epoch: int) -> None:
        """Execute a supported deterministic task without any GisAI engine.

        The dispatcher owns parsing, execution, independent read-back
        verification and rollback; this method only supplies the workspace,
        the cancellation signal and the task record the finish path expects.
        """
        from giscode.agentic.core_tasks import CoreTask, execute_request

        root_path = str(getattr(self._active_project_context, "root_path", "") or "")
        task = CoreTask(
            task_id=f"core-{time.time_ns()}",
            raw_request=str(message),
            project_path=root_path,
            status="running",
        )
        try:
            result = execute_request(
                message,
                root_path,
                task_id=task.task_id,
                should_cancel=lambda: epoch != self._task_epoch or not self._active_task,
                deadline_s=self._remaining_task_budget(),
                # Core edits ask the same person through the same
                # thread-safe queue the engine-backed path uses.
                approve=self._approve_edit,
            )
        except Exception as exc:  # never let a dispatcher bug look like success
            self._worker_results[epoch] = (None, exc, epoch, task)
            return
        task.status = str(result.get("status") or "failed")
        self._worker_results[epoch] = (result, None, epoch, task)

    def _remaining_task_budget(self) -> Optional[float]:
        """Time left on the task clock, or None when no budget was started."""
        budget = getattr(self, "_task_timeout_s", None)
        if not budget:
            return None
        started = float(getattr(self, "_task_start_time", 0.0) or time.time())
        return max(0.0, float(budget) - (time.time() - started))

    def _accept_received_task(self, task, epoch: int) -> None:
        if epoch != self._task_epoch or not self._active_task:
            return
        self._current_task = task
        self._active_gisai_task = task
        self._current_task_id = getattr(task, "task_id", None)

    def _drain_worker_result(self, epoch: int) -> None:
        status = self._pending_statuses.pop(epoch, None)
        if status is not None and epoch == self._task_epoch:
            self._apply_task_status(status)
        result = self._worker_results.pop(epoch, None)
        if result is not None:
            task_result, error, result_epoch, task = result
            if task is not None and self._active_gisai_task is None:
                self._accept_received_task(task, result_epoch)
            if self._waiting_input(task):
                self._set_ui_state("IDLE")
                # The task is parked, not working: a stale "Running tests..."
                # must not stay on screen while it waits for the user.
                self.vision_status_var.set(ACTIVITY_READY)
                self._append_response(self._waiting_input_message(task))
                self._active_task = False
                return
            self._finish_task(task_result, error, result_epoch)
            return
        if self._active_task and epoch == self._task_epoch:
            self.root.after(25, lambda: self._drain_worker_result(epoch))

    @staticmethod
    def _waiting_input_message(task) -> str:
        """Render the waiting-state message from the engine's typed reason.

        ``WAITING_INPUT`` is shared by the vision gate (an image attachment is
        required) and the project-clarification gate (the engine needs an answer
        before continuing). The engine records which one fired in
        ``task.metadata["waiting_reason"]``; the GUI must not guess from the
        request text. A missing reason keeps the historical image message so
        genuine vision requests and older persisted tasks behave unchanged.
        """
        metadata = getattr(task, "metadata", None)
        reason = ""
        if isinstance(metadata, dict):
            reason = str(metadata.get("waiting_reason") or "")
        if reason == "project_clarification":
            questions = [
                q.get("question")
                for q in (metadata.get("clarification_questions") or [])
                if isinstance(q, dict) and q.get("question")
            ]
            lines = ["Clarification needed before this request can continue:"]
            lines.extend(f"- {q}" for q in questions)
            if not questions:
                lines.append("Please answer the clarification question(s) to continue.")
            return "\n".join(lines)
        return "Waiting for an image attachment before analysis."

    def _on_task_status(self, message: str, epoch: Optional[int] = None) -> None:
        if epoch is None:
            epoch = self._task_epoch
            self._live_activity = str(message)
            self._live_activity_at = time.time()
            self.phase_var.set(str(message))
            self.vision_status_var.set(
                ACTIVITY_RUNNING_TESTS if "test" in str(message).lower() else str(message)
            )
        if epoch != self._task_epoch:
            return
        self._pending_statuses[epoch] = str(message)
        return

    def _apply_task_status(self, message: str) -> None:
        lower_message = str(message).lower()
        if "test" in lower_message:
            self.vision_status_var.set(ACTIVITY_RUNNING_TESTS)
        elif "vision" in lower_message or "image" in lower_message:
            self.vision_status_var.set("Analyzing image...")
        try:
            self._status_var.set(str(message))
        except tk.TclError:
            return

    def _finish_task(self, result, error, epoch: int) -> None:
        if epoch != self._task_epoch:
            return
        active_task = getattr(self, "_active_gisai_task", None)
        task_status = getattr(active_task, "status", None)
        task_status = getattr(task_status, "value", task_status)
        self._active_task = False
        self._active_gisai_task = None
        change_summary = None
        if isinstance(result, dict) and isinstance(result.get("work_result"), dict):
            # Recorded for the acceptance harness and any other audit reader,
            # independent of whether an engine produced the result.
            self._last_work_result = result["work_result"]
        if active_task is not None and isinstance(result, dict) and isinstance(result.get("work_result"), dict):
            change_summary = build_change_summary(
                result["work_result"],
                str(getattr(active_task, "project_path", "") or ""),
            )
        self._last_change_summary = change_summary
        if hasattr(self, "view_diff_btn"):
            self.view_diff_btn.state(
                ["!disabled"] if change_summary and change_summary.get("diff") else ["disabled"]
            )
        self._refresh_history()
        cancelled = (
            isinstance(error, RequestCancelledError)
            or (error is not None and "cancel" in str(error).lower())
            or (isinstance(result, dict) and "cancel" in str(result.get("error", "")).lower())
            or task_status == "cancelled"
        )
        if self._task_manager is not None:
            try:
                if cancelled:
                    self._task_manager.cancel()
                else:
                    self._task_manager.complete(success=error is None and not self._result_error(result), failure_info={"message": str(error or result)})
            except Exception:
                pass
        if cancelled:
            self._set_ui_state("STOPPED")
            self.vision_status_var.set(ACTIVITY_CANCELLED)
            self._show_chat_result(
                None,
                self._with_change_summary("Operation cancelled.", change_summary),
                time.time() - self._task_start_time,
                epoch,
            )
        elif error is not None or self._result_error(result):
            self._set_ui_state("ERROR")
            if self._result_error(result) and "vision" in self._result_error(result).lower():
                self.vision_status_var.set(ACTIVITY_VISION_UNAVAILABLE)
            else:
                # Whatever the last progress message was (often "Running
                # tests..."), it no longer describes the finished task.
                self.vision_status_var.set(ACTIVITY_FAILED)
            self._show_chat_result(
                None,
                self._with_change_summary(
                    self._failure_text(result, error or self._result_error(result)), change_summary
                ),
                time.time() - self._task_start_time,
                epoch,
            )
        else:
            self._set_ui_state("IDLE")
            # Any successful task is finished, including a coding task whose
            # last progress message was "Running tests...".
            self.vision_status_var.set(ACTIVITY_COMPLETED)
            report = self._result_text(result)
            report = self._with_change_summary(report, change_summary)
            self._show_chat_result(report, None, time.time() - self._task_start_time, epoch)

    @staticmethod
    def _failure_text(result, error) -> str:
        """The text a failed or refused task shows the user.

        A Core refusal explains itself in ``response``: what Core can do,
        which status the request got, and why. ``error`` is the short
        machine-facing line. The explanation is the point of the capability
        contract, so it wins whenever the dispatcher supplied one.
        """
        detail = str(error or "")
        if isinstance(result, dict) and result.get("capability") and result.get("response"):
            return str(result["response"])
        return detail

    @staticmethod
    def _with_change_summary(text: str, change_summary: Optional[dict]) -> str:
        """Append the change summary when structured work evidence exists.

        A task that failed or was stopped after real writes still changed the
        workspace, so the summary is shown on those paths too rather than
        being silently dropped.
        """
        if change_summary is None:
            return text
        return f"{text}\n\n{format_change_summary(change_summary)}"


    def _show_diff_viewer(self) -> None:
        summary = self._last_change_summary or {}
        diff = summary.get("diff") or ""
        if not diff:
            return
        if self._diff_viewer is not None and self._diff_viewer.winfo_exists():
            self._diff_viewer.lift()
            return
        viewer = tk.Toplevel(self.root)
        self._diff_viewer = viewer
        viewer.title("View Diff")
        viewer.geometry("900x650")
        viewer.transient(self.root)
        ttk.Label(
            viewer,
            text=(
                f"{summary.get('file_count', 0)} files changed  "
                f"+{summary.get('additions', 0)} / -{summary.get('deletions', 0)} lines"
            ),
        ).pack(anchor=tk.W, padx=10, pady=(10, 4))
        text = tk.Text(viewer, wrap=tk.NONE, state=tk.NORMAL)
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        text.insert("1.0", diff)
        text.configure(state=tk.DISABLED)
        add_text_context_menu(text, editable=False)
        ttk.Button(viewer, text="Close", command=viewer.destroy).pack(anchor=tk.E, padx=10, pady=(0, 10))

    def _refresh_history(self) -> None:
        if not hasattr(self, "history_listbox") or self._gisai_engine is None:
            return
        try:
            project_id = getattr(self._active_project_context, "project_id", "")
            records = self._gisai_engine._history_store.get_for_project(project_id) if project_id else []
            self._history_records = records
            self.history_listbox.delete(0, tk.END)
            for record in records:
                label = f"{record.final_status or ('completed' if record.success else 'failed')} | {record.task_id} | {record.request_summary[:60]}"
                self.history_listbox.insert(tk.END, label)
        except Exception:
            self._history_records = []

    def _show_history_detail(self, _event=None) -> None:
        if not self._history_records or self._history_detail is None:
            return
        selection = self.history_listbox.curselection()
        if not selection:
            return
        record = self._history_records[selection[0]]
        files = ", ".join(record.changed_files) if record.changed_files else "No files changed."
        if record.user_approved:
            approval = "Applied (edits approved)"
        elif record.has_changes:
            approval = "Changes recorded (no approval flag)"
        else:
            approval = "No approved changes applied"
        detail = (
            f"Request\n{record.request_summary or '[legacy/incomplete record]'}\n\n"
            f"Status\n{record.final_status or ('completed' if record.success else 'failed')}\n\n"
            f"Changes\n{files}\n+{record.additions} / -{record.deletions}\n\n"
            f"Approval\n{approval}\n\n"
            f"Validation\n{record.verification_status}\n\n"
            f"Errors\n{record.error_summary or 'None recorded.'}"
        )
        self._history_detail.configure(state=tk.NORMAL)
        self._history_detail.delete("1.0", tk.END)
        self._history_detail.insert("1.0", detail)
        self._history_detail.configure(state=tk.DISABLED)
        self._history_diff_btn.state(["!disabled"] if record.unified_diff else ["disabled"])

    def _show_history_diff(self) -> None:
        selection = self.history_listbox.curselection()
        if not selection or not self._history_records[selection[0]].unified_diff:
            return
        viewer = tk.Toplevel(self.root)
        viewer.title("Historical Diff")
        viewer.geometry("900x650")
        text = tk.Text(viewer, wrap=tk.NONE, state=tk.NORMAL)
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        text.insert("1.0", self._history_records[selection[0]].unified_diff)
        text.configure(state=tk.DISABLED)
        add_text_context_menu(text, editable=False)
        ttk.Button(viewer, text="Close", command=viewer.destroy).pack(anchor=tk.E, padx=10, pady=(0, 10))

    @staticmethod
    def _result_error(result) -> Optional[str]:
        if isinstance(result, dict) and result.get("error"):
            return str(result["error"])
        return None

    @staticmethod
    def _result_text(result) -> str:
        if isinstance(result, dict):
            # ``coding_result`` is the engine's own human-readable summary of a
            # real-work run. It must be preferred over the raw payload: falling
            # through to ``str(result)`` would dump the entire serialized
            # work_result, including every action's full pre-edit file content,
            # into the result area and chat history.
            return str(
                result.get("response")
                or result.get("coding_result")
                or result.get("vision_result")
                or result.get("message")
                or result
            )
        return str(getattr(result, "response", result))

    def _show_chat_result(self, report, error, elapsed: float, epoch=None) -> None:
        if epoch is not None and epoch != self._task_epoch:
            return
        text = report if report is not None else error or "Operation cancelled."
        if str(text).lower() == "operation cancelled by user":
            text = "Operation cancelled."
        self._last_report = str(text)
        self._append_history("assistant", str(text))
        self.copy_report_btn.state(["!disabled"])
        self.phase_var.set(
            "Cancelled" if error is not None and "cancel" in str(error).lower()
            else "Error" if error is not None else "Completed"
        )
        self._terminal_state_until = time.time() + 1.0
        self._status_var.set(f"Completed in {elapsed:.1f}s" if error is None else str(error))

    def _format_gisai_task_report(self, task) -> str:
        return "\n".join([
            f"Task ID: {getattr(task, 'task_id', '')}",
            f"Request: {getattr(task, 'raw_request', '')}",
            f"Status: {getattr(getattr(task, 'status', None), 'value', getattr(task, 'status', ''))}",
            f"Phase: {getattr(getattr(task, 'state', None), 'value', getattr(task, 'state', ''))}",
            f"Result: {getattr(task, 'result', '') or ''}",
            f"Repair attempts: {getattr(task, 'repair_attempts', 0)}",
        ])

    def _copy_report(self) -> None:
        if self._last_report:
            self._copy_chat_response(self._last_report)

    def _update_copy_button(self) -> None:
        if self.response_text.get("1.0", "end-1c").strip():
            self.copy_report_btn.state(["!disabled"])
        else:
            self.copy_report_btn.state(["disabled"])

    def _set_ui_state(self, state: str) -> None:
        self._ui_state = state
        if state in ("WORKING", "STOPPING"):
            self.send_task_btn.state(["disabled"])
            self.header_stop_btn.state(["!disabled"])
        else:
            self.send_task_btn.state(["!disabled"])
            self.header_stop_btn.state(["disabled"])
        if state == "WORKING":
            self.revoke_btn.state(["!disabled"])
            self.phase_var.set("Working")
        elif state == "STOPPING":
            self.revoke_btn.state(["disabled"])
            self.phase_var.set("Stopping")
        elif state == "STOPPED":
            self.revoke_btn.state(["disabled"])
            self.phase_var.set("Cancelled")
        elif state == "ERROR":
            self.revoke_btn.state(["disabled"])
            self.phase_var.set("Error")
        elif state == "IDLE":
            self.revoke_btn.state(["disabled"])
            self.phase_var.set("Idle")

    def _stopped_task_handles(self):
        """The task that is about to stop, plus its project root and snapshot.

        A stop can land while the worker is still holding the task, so the
        engine's active task is used as the fallback; the GUI copy is only
        filled in once the result has been drained.
        """
        task = self._active_gisai_task
        if task is None and self._gisai_engine is not None:
            try:
                task = self._gisai_engine.get_active_task()
            except Exception:
                task = None
        metadata = getattr(task, "metadata", None)
        # The executor publishes the snapshot as a plain task attribute; the
        # metadata copy is kept as a fallback for hosts that only forward it.
        snapshot = getattr(task, "snapshot_manager", None)
        if snapshot is None and isinstance(metadata, dict):
            snapshot = metadata.get("snapshot_manager")
        return task, snapshot

    def _task_stop_report(self, *, kind: str) -> str:
        """Roll back applied-but-unverified edits and build the stop report.

        A stopped task never reaches verification, so every file it wrote is an
        unverified edit and the snapshot taken before the write restores it.
        This is the same rule the executor already applies when verification
        fails without progress. The report always names the files that really
        changed.
        """
        if kind == "timeout":
            headline = (
                "Task stopped: it ran longer than the task time limit "
                f"({self._task_limit_minutes_label()} minutes)"
            )
            opened = "Task stopped: time limit reached"
        else:
            headline = "Stop requested. Operation cancelled."
            opened = "Task stopped: stopped by the user"

        task, snapshot = self._stopped_task_handles()
        root = str(getattr(task, "project_path", "") or "") if task is not None else ""
        files: list = []
        compare_failed = False
        result = None
        failure = ""
        if snapshot is not None and root:
            try:
                files = list(snapshot.applied_edits(Path(root)))
            except Exception:
                compare_failed = True
            if files:
                try:
                    from giscode.agentic.rollback import (
                        REASON_CHANGED_AFTER,
                        describe_conflict,
                        rollback_task_edits,
                    )

                    # Only this task's own output is restored: a file the user
                    # saved after the task wrote it is reported, never replaced.
                    result = rollback_task_edits(snapshot, Path(root))
                except Exception as exc:  # noqa: BLE001 - the report must say it
                    failure = f"{type(exc).__name__}: {exc}"

        restored = list(result.restored) + list(result.removed) if result else []
        conflicts = list(result.conflicts) if result else []

        def _not_undone() -> str:
            """Why each kept file was not undone, one file at a time.

            "They changed after the task wrote them" is only true for one of
            the causes; a missing version record or a failed restore has to be
            named as itself.
            """
            if all(reason == REASON_CHANGED_AFTER for _, reason in conflicts):
                kept = ", ".join(path for path, _ in conflicts)
                return f"They changed after the task wrote them: {kept}."
            sentences = "; ".join(
                describe_conflict(path, reason) for path, reason in conflicts
            )
            return sentences[:1].upper() + sentences[1:] + "."

        if compare_failed:
            outcome = f"{opened}. The working tree could not be compared with the pre-edit snapshot."
        elif not files:
            outcome = f"{opened}, no changes were made."
        elif restored and not conflicts:
            outcome = f"{opened}. Edits were rolled back: {', '.join(restored)}."
        elif restored:
            outcome = (
                f"{opened}. Edits were rolled back: {', '.join(restored)}. "
                + _not_undone()
            )
        elif conflicts:
            kept = ", ".join(path for path, _ in conflicts)
            if all(reason == REASON_CHANGED_AFTER for _, reason in conflicts):
                outcome = (
                    f"{opened}. Files that changed: {', '.join(files)}. "
                    f"None were rolled back: {kept} changed after the task wrote them."
                )
            else:
                outcome = (
                    f"{opened}. Files that changed: {', '.join(files)}. "
                    f"None were rolled back. {_not_undone()}"
                )
        elif result is not None and result.blocked:
            outcome = (
                f"{opened}. Files that changed: {', '.join(files)}. "
                f"No edit could be undone: {result.blocked}."
            )
        elif failure:
            outcome = (
                f"{opened}. Files that changed: {', '.join(files)}. "
                f"The rollback could not be attempted ({failure}); those edits "
                "are still on disk."
            )
        else:
            outcome = f"{opened}. Files that changed: {', '.join(files)}."
        return f"{headline}\n{outcome}"

    def _revoke_task(self) -> None:
        if not self._active_task:
            return
        self._set_ui_state("STOPPING")
        report = self._task_stop_report(kind="stop")
        self._task_epoch += 1
        epoch = self._task_epoch
        try:
            if self._gisai_engine is not None:
                self._gisai_engine.cancel()
        finally:
            if self._task_manager is not None:
                self._task_manager.cancel()
            self._active_task = False
            self._active_gisai_task = None
            self._show_chat_result(report, None, time.time() - self._task_start_time, epoch)
            self._set_ui_state("STOPPED")

    def _cancel_before_switch(self) -> None:
        if self._active_task:
            self._revoke_task()
            self._append_response("Task cancelled before switching project.")
            self.phase_var.set("Switching project")

    def _build_attachments(self, paths) -> list:
        from giscode.agentic.attachments import Attachment

        attachments = []
        for path in paths or []:
            try:
                size_bytes = os.path.getsize(path)
            except OSError:
                # Keep the selected path in the request. Size metadata is
                # descriptive; silently dropping the attachment turns a real
                # file/access problem into a misleading WAITING_INPUT result.
                size_bytes = 0
            attachments.append(
                Attachment(
                    path=str(path),
                    mime_type=mimetypes.guess_type(str(path))[0] or "application/octet-stream",
                    size_bytes=size_bytes,
                )
            )
        return attachments

    def _attachment_label(self, paths) -> str:
        if not paths:
            return ""
        details = []
        for path in paths:
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            kind = mime.rsplit("/", 1)[-1].upper()
            try:
                size = os.path.getsize(path)
                size_text = f", {size / 1024:.1f} KB"
            except OSError:
                size_text = ""
            details.append(f"{os.path.basename(path)} ({kind}{size_text})")
        return "Attached: " + ", ".join(details)

    def _attach_image(self, replace=False) -> None:
        path = filedialog.askopenfilename(
            title="Attach image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All files", "*.*")],
        )
        if not path:
            return
        session = self._current_chat_session()
        draft = self.task_entry.get("1.0", "end-1c")
        if replace:
            session["images"] = []
        session["images"] = [path]
        self.attach_info_var.set(self._attachment_label(session["images"]))
        self.vision_status_var.set(ACTIVITY_READY)
        self._attach_strip_visible = True
        self._append_response(f"[Attached image: {os.path.basename(path)}]")
        self.task_entry.delete("1.0", tk.END)
        self.task_entry.insert("1.0", draft)
        session["draft"] = draft

    def _remove_attached_image(self) -> None:
        self._current_chat_session()["images"] = []
        self.attach_info_var.set("")
        self.vision_status_var.set(ACTIVITY_READY)
        self._attach_strip_visible = False

    def _analyze_attached_image(self) -> None:
        if not self._current_chat_session().get("images"):
            self.vision_status_var.set(ACTIVITY_VISION_UNAVAILABLE)
            self._append_response("Attach an image before selecting Analyze.")
            return
        self.task_entry.delete("1.0", tk.END)
        self.task_entry.insert("1.0", "Analyze the attached image")
        self.vision_status_var.set("Analyzing image...")
        self._send_task()

    def _on_terminal_run(self) -> None:
        cmd = self._terminal_input_var.get().strip()
        if not cmd:
            return
        self._terminal_input_var.set("")
        self._terminal_output.config(state=tk.NORMAL)
        self._terminal_output.insert(tk.END, f"$ {cmd}\n")
        self._terminal_output.config(state=tk.DISABLED)
        self._terminal_output.see(tk.END)

        # Run command in background
        def run_cmd():
            import subprocess
            try:
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=".")
                output = result.stdout
                if result.stderr:
                    output += "\n" + result.stderr
            except subprocess.TimeoutExpired:
                output = "Command timed out"
            except Exception as e:
                output = f"Error: {e}"

            self.root.after(0, lambda: self._append_terminal_output(output))

        import threading
        threading.Thread(target=run_cmd, daemon=True).start()

    def _append_terminal_output(self, output: str) -> None:
        self._terminal_output.config(state=tk.NORMAL)
        self._terminal_output.insert(tk.END, output + "\n")
        self._terminal_output.config(state=tk.DISABLED)
        self._terminal_output.see(tk.END)

    _APPROVAL_TIMEOUT_S = 600

    def _approve_edit(self, path, diff: str) -> bool:
        preview = diff if len(diff) <= 12000 else diff[:12000] + "\n... [diff clipped]"
        msg = f"Proposed edit for {path}:\n\n{preview}\n\nApply this edit?"
        # The task clock is paused for the whole decision, on either path: the
        # user reading a diff is not the agent working, so it must not consume
        # the task time limit.
        # Direct/unit callers on the Tk main thread just ask.
        if threading.current_thread() is threading.main_thread():
            with self._task_clock_paused():
                accepted = bool(messagebox.askyesno("Apply proposed edit", msg, parent=self.root))
            self._note_human_edit_decision(path, shown=True, accepted=accepted)
            return accepted
        # Worker thread: enqueue (thread-safe list append) and wait. A poller
        # running ON the Tk main thread (started once, via root.after) drains
        # the queue and shows the dialog there. No cross-thread Tk calls, so
        # the dialog cannot wedge and the worker cannot block the UI.
        ev = threading.Event()
        item = {"msg": msg, "result": False, "done": ev, "shown": False}
        self._pending_approvals.append(item)
        deadline = time.time() + self._APPROVAL_TIMEOUT_S
        with self._task_clock_paused():
            while not ev.wait(0.05):
                if getattr(self, "_closing", False):
                    break
                if time.time() > deadline:
                    break
        # Timeout / window-close / Stop -> Reject. ``shown`` is set by the
        # drainer only when it really opens the dialog, so a decision that
        # never reached a person is not reported as a human approval.
        shown = bool(item.get("shown"))
        accepted = bool(item["result"]) if ev.is_set() else False
        self._note_human_edit_decision(path, shown=shown, accepted=accepted)
        return accepted

    def _note_human_edit_decision(self, path, *, shown: bool, accepted: bool) -> None:
        """Tell the engine whether a person was actually shown the dialog.

        The engine cannot infer that from its approval callback being called -
        a test host or an auto-approval policy calls it too - so the GUI, which
        is the only place that knows a dialog appeared, reports it here.
        """
        engine = getattr(self, "_gisai_engine", None)
        note = getattr(engine, "note_edit_dialog", None)
        if not callable(note):
            return
        try:
            note(path, shown=shown, accepted=accepted)
        except Exception:
            pass

    def _drain_pending_approvals(self) -> None:
        # Runs on the Tk main thread. Show one dialog per pending request.
        while self._pending_approvals:
            item = self._pending_approvals.pop(0)
            if self._closing:
                item["result"] = False
                item["done"].set()
                continue
            item["shown"] = True
            try:
                answer = bool(messagebox.askyesno(
                    "Apply proposed edit", item["msg"], parent=self.root,
                ))
                # A close while the dialog is open voids the answer: the user
                # dismissed the window, not the edit. Recording the answer
                # unconditionally made the outcome depend on whether the
                # waiting worker's 50 ms poll noticed _closing before this
                # assignment, so a closing window could still approve.
                item["result"] = answer and not self._closing
            finally:
                item["done"].set()

    def _approval_poll_loop(self) -> None:
        # Main-thread poller: drains queued approval dialogs. Reschedules
        # itself so it survives across the app lifetime.
        try:
            self._drain_pending_approvals()
        finally:
            if not self._closing:
                self.root.after(100, self._approval_poll_loop)

    def _on_window_close(self) -> None:
        # Reject any approval the user hasn't answered, then destroy the root.
        # Pending worker threads see _closing and return Reject immediately.
        self._closing = True
        while self._pending_approvals:
            item = self._pending_approvals.pop(0)
            item["result"] = False
            item["done"].set()
        # Stop any llama-server this window's inference owns so it cannot
        # outlive the GUI process.
        try:
            mm = getattr(self, "_model_manager", None)
            inf = getattr(mm, "_inference", None) if mm is not None else None
            if inf is not None and hasattr(inf, "unload_model"):
                inf.unload_model()
        except Exception:
            pass
        self.root.destroy()

    # ===== PROJECT CONTEXT / BRANDING =====
    def _select_project_path(self, path: str, *, project_kind: str = "existing") -> None:
        path = os.path.abspath(os.path.expanduser(path))
        context = ProjectContext.create(path, project_kind=project_kind)
        custom_logo = self._ctx_store.get_custom_logo(context.project_id)
        if not custom_logo:
            for stored in self._ctx_store.load_all().values():
                if (
                    os.path.normcase(os.path.abspath(stored.root_path))
                    == os.path.normcase(path)
                    and stored.custom_logo
                ):
                    custom_logo = stored.custom_logo
                    break
        context.custom_logo = custom_logo or ""
        self._active_project_context = context
        self._ctx_store.save(context)
        self.current_path_var.set(context.root_path)
        self.current_name_var.set(context.name)
        self.project_path_var.set(f"Project opened: {context.name} ({context.root_path})")
        self.branding_var.set("Custom logo" if custom_logo else "Gis branding (default)")
        if self._gisai_engine is not None and hasattr(self._gisai_engine, "set_project_context"):
            self._gisai_engine.set_project_context(context)
        self._report_recovered_rollbacks()
        stored_project = None
        try:
            stored_project = next(
                (
                    project
                    for project in self._project_store.list_projects()
                    if os.path.normcase(os.path.abspath(project.root_path))
                    == os.path.normcase(path)
                ),
                None,
            )
        except Exception:
            self.logger.debug("Could not resolve selected project record", exc_info=True)

        if stored_project is not None:
            self._external_project_root = None
            self._selected_project_id = stored_project.id
            if (
                self._project_workspace_controller is not None
                and self._project_workspace_controller.current_project_id != stored_project.id
            ):
                # The user picked this exact folder in the directory dialog, so
                # that choice is the explicit external-project selection.
                self._project_workspace_controller.open_project(
                    stored_project.id, allow_external=True
                )
                if self._project_workspace_controller.is_external_project(
                    stored_project.root_path
                ):
                    self._append_response(
                        "External project: GisCode will modify files only inside "
                        f"{stored_project.root_path}"
                    )
        else:
            # A transferred/legacy project may have no local ProjectStore row.
            # Keep its selected path active without manufacturing one.
            self._selected_project_id = None
            self._external_project_root = path
            if self._project_workspace_controller is not None:
                self._project_workspace_controller.select_existing_project_root(path)
        self._append_response(f"Project opened: {context.name}")
        # The Projects tab mirrors the authoritative selection through this
        # call. Without it the "Active project" label kept showing
        # "No project selected" after a project was chosen here (from the task
        # dialog or Open), because only the header combo box refreshed it.
        self._sync_project_workspace_display()

    def _report_recovered_rollbacks(self) -> None:
        """Tell the user what an interrupted rollback left, and where it is.

        The engine repairs the project when it is opened; this only reports.
        Recovery copies that could not be restored safely stay on disk, so the
        message names them instead of implying the cleanup succeeded.
        """
        actions = []
        getter = getattr(self._gisai_engine, "last_rollback_recovery", None)
        if callable(getter):
            try:
                actions = getter() or []
            except Exception:
                actions = []
        error = ""
        error_getter = getattr(
            self._gisai_engine, "last_rollback_recovery_error", None
        )
        if callable(error_getter):
            try:
                error = str(error_getter() or "")
            except Exception:
                error = ""
        interesting = [action for action in actions if action.status != "skipped"]
        if not interesting and not error:
            return
        lines = ["Rollback recovery after an interrupted task:"]
        lines.extend(f"  {action.describe()}" for action in interesting)
        if error:
            # A scan that could not finish is not the same as a clean project,
            # so it is named rather than silently counted as "nothing found".
            lines.append(
                "  The recovery scan could not finish "
                f"({error}); any copy it would have repaired is still where it was."
            )
        message = "\n".join(lines)
        try:
            self._append_response(message)
            self._last_report = message
        except Exception:
            pass

    def _open_project(self) -> None:
        path = filedialog.askdirectory(title="Open project", parent=self.root)
        if path:
            self._select_project_path(path)

    def _enter_active_section(self, section: str) -> None:
        if self._active_project_context is None:
            return
        self._active_project_context.enter_section(section)
        self._ctx_store.save(self._active_project_context)
        if self._gisai_engine is not None and hasattr(self._gisai_engine, "set_project_context"):
            self._gisai_engine.set_project_context(self._active_project_context)

    def _back_section(self) -> None:
        if self._active_project_context is None:
            return
        self._active_project_context.back_to_previous_section()
        self._ctx_store.save(self._active_project_context)

    def _close_project(self) -> None:
        """Close the current project workspace and clear the active project context."""
        if self._project_workspace_controller is not None:
            try:
                self._project_workspace_controller.close()
            except Exception:
                pass
        self._active_project_context = None
        self._external_project_root = None
        self.current_path_var.set("")
        self.current_name_var.set("")
        self.project_path_var.set("")
        self.branding_var.set("Gis branding (default)")
        # Notify GisAiEngine that project context is cleared
        if self._gisai_engine is not None and hasattr(self._gisai_engine, "set_project_context"):
            self._gisai_engine.set_project_context(None)

    def _choose_custom_logo(self) -> None:
        if self._active_project_context is None:
            return
        path = filedialog.askopenfilename(
            title="Choose custom logo",
            filetypes=[("Logo markup", "*.svg *.html *.htm"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            content = open(path, "r", encoding="utf-8").read()
        except (OSError, UnicodeError):
            messagebox.showerror("Logo not usable", "The selected logo file could not be read.")
            return
        if "<svg" not in content.lower() and "<html" not in content.lower():
            messagebox.showerror("Logo not usable", "The selected file is not usable as a logo.")
            return
        self._active_project_context.custom_logo = content
        self._ctx_store.save(self._active_project_context)
        self._ctx_store.save_custom_logo(self._active_project_context.project_id, content)
        self.branding_var.set("Custom logo")

    def _set_active_context_from_selection(self) -> None:
        selection = self._project_var.get()
        if not selection or selection.startswith("➕"):
            return
        for project in self._project_store.list_projects():
            if f"{project.name} ({project.root_path})" == selection:
                self._select_project_path(project.root_path)
                return

    # ===== TAB CREATORS =====
    def _create_projects_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=10)
        self._notebook.add(frame, text="Projects")
        self._tab_frames["Projects"] = frame

        # Activating and closing a project are project actions, so they live
        # with the project workspace instead of in the header. The handlers,
        # their guards and their state are the ones the header used.
        self.project_actions_row = ttk.Frame(frame)
        self.project_actions_row.pack(fill=tk.X, pady=(0, 8))
        self.open_project_btn = ttk.Button(
            self.project_actions_row, text="Open project", command=self._on_open_project
        )
        self.open_project_btn.pack(side=tk.LEFT)
        self.close_project_btn = ttk.Button(
            self.project_actions_row, text="Close project", command=self._on_close_project
        )
        self.close_project_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Create controller and view for Projects tab. The shared vision
        # service is passed through so the Planner panel can really read
        # an attached image instead of only displaying it.
        # Approve & Start hands the approved plan to the existing build launcher,
        # which runs the application's existing CodingAgentLoop. The window's
        # coding agent is that loop; when only the GisAI engine is wired, its own
        # coding agent is used so the hand-off still has something to run.
        build_coding_agent = self._coding_agent or getattr(
            self._gisai_engine, "_coding_agent", None
        )
        controller = ProjectWorkspaceController(
            self._project_store,
            self._chat_store,
            vision_service=self._vision_service,
            gisai_engine=self._gisai_engine,
            coding_agent=build_coding_agent,
        )
        self._project_workspace_controller = controller  # For test compatibility
        view_host = ttk.Frame(frame)
        view_host.pack(fill=tk.BOTH, expand=True)
        self._project_workspace_view = ProjectWorkspaceView(view_host, controller)
        # ProjectWorkspaceView builds its UI directly in the parent frame, no pack needed

        # Sync the initial "no project" state (None) to the Projects tab view.
        self._sync_project_workspace_display()

    def _create_chat_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=10)
        self._notebook.add(frame, text="Chat")
        self._tab_frames["Chat"] = frame

        chat_frame = ttk.LabelFrame(frame, text="GisCode Assistant", padding=8)
        chat_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.response_text = tk.Text(chat_frame, height=15, wrap=tk.WORD)
        self.response_text.pack(fill=tk.BOTH, expand=True)
        # Add right-click context menu for response_text
        self._add_text_context_menu(self.response_text)

        input_frame = ttk.Frame(chat_frame)
        input_frame.pack(fill=tk.X, pady=(8, 0))

        self.task_entry = _MultilineEntry(input_frame, height=4, wrap=tk.WORD)
        self.task_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self.task_entry.bind("<Return>", self._send_task)
        self.task_entry.bind("<Control-Return>", self._send_task)
        self.task_entry.bind("<KeyRelease>", self._sync_draft)
        # Add right-click context menu for task_entry
        self._add_text_context_menu(self.task_entry)

        self.send_task_btn = ttk.Button(input_frame, text="Send", command=self._send_task)
        self.send_task_btn.pack(side=tk.RIGHT)
        self.revoke_btn = ttk.Button(input_frame, text="Stop", command=self._revoke_task, state=tk.DISABLED)
        self.revoke_btn.pack(side=tk.RIGHT, padx=(4, 0))

        attach_row = ttk.Frame(frame)
        attach_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(attach_row, text="Attach Image", command=self._attach_image).pack(side=tk.LEFT)
        ttk.Button(attach_row, text="Analyze Attachment", command=self._analyze_attached_image).pack(side=tk.LEFT, padx=4)
        ttk.Label(attach_row, textvariable=self.attach_info_var).pack(side=tk.LEFT, padx=8)
        ttk.Label(attach_row, textvariable=self.vision_status_var).pack(side=tk.RIGHT)

        chat_tabs = ttk.Frame(frame)
        chat_tabs.pack(fill=tk.X, pady=(0, 6))
        self._chat_tabs_frame = chat_tabs
        self._render_chat_tabs()

        # Secondary actions share the attachment row. Two full-width rows for
        # them pushed the conversation around; the buttons, their state and
        # their handlers are unchanged, only their placement is.
        self.copy_report_btn = ttk.Button(attach_row, text="Copy latest response", command=self._copy_report, state=tk.DISABLED)
        self.copy_report_btn.pack(side=tk.RIGHT, padx=(4, 0))
        self.view_diff_btn = ttk.Button(attach_row, text="View Diff", command=self._show_diff_viewer, state=tk.DISABLED)
        self.view_diff_btn.pack(side=tk.RIGHT, padx=(4, 0))

        if edition.is_core():
            # History has no top-level tab in Core: the same panel opens inside
            # Chat, so session recovery and persistence are one click away
            # without adding a navigation destination.
            self._history_toggle_btn = ttk.Button(attach_row, text="History", command=self._toggle_chat_history)
            self._history_toggle_btn.pack(side=tk.LEFT, padx=(10, 0))
            self._chat_history_host = ttk.Frame(frame)
            self._chat_history_visible = False
            self._build_history_panel(self._chat_history_host)
        else:
            self._history_toggle_btn = None
            self._chat_history_host = None
            self._chat_history_visible = False

    def _toggle_chat_history(self) -> None:
        """Show or hide the conversation-history panel inside Chat."""
        host = getattr(self, "_chat_history_host", None)
        if host is None:
            return
        if host.winfo_manager():
            host.pack_forget()
            self._chat_history_visible = False
        else:
            host.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
            self._chat_history_visible = True
            self._refresh_conversation_history()

    def _create_terminal_tab(self) -> None:
        # Pro-only: the builder lives in giscode.gui.pro_tabs, which the
        # Core export does not ship. Importing it here is the module
        # boundary: a build without it raises ModuleNotFoundError and the
        # tab explains itself instead of the window failing to start.
        from giscode.gui.pro_tabs import build_terminal_tab

        build_terminal_tab(self)

    def _create_device_preview_tab(self) -> None:
        # Pro-only: the builder lives in giscode.gui.pro_tabs, which the
        # Core export does not ship. Importing it here is the module
        # boundary: a build without it raises ModuleNotFoundError and the
        # tab explains itself instead of the window failing to start.
        from giscode.gui.pro_tabs import build_device_preview_tab

        build_device_preview_tab(self)

    def _create_generate_video_tab(self) -> None:
        # Pro-only: the builder lives in giscode.gui.pro_tabs, which the
        # Core export does not ship. Importing it here is the module
        # boundary: a build without it raises ModuleNotFoundError and the
        # tab explains itself instead of the window failing to start.
        from giscode.gui.pro_tabs import build_generate_video_tab

        build_generate_video_tab(self)

    def _create_code_task_tab(self) -> None:
        # Pro-only: the builder lives in giscode.gui.pro_tabs, which the
        # Core export does not ship. Importing it here is the module
        # boundary: a build without it raises ModuleNotFoundError and the
        # tab explains itself instead of the window failing to start.
        from giscode.gui.pro_tabs import build_code_task_tab

        build_code_task_tab(self)

    def _create_analyze_tab(self) -> None:
        # Pro-only: the builder lives in giscode.gui.pro_tabs, which the
        # Core export does not ship. Importing it here is the module
        # boundary: a build without it raises ModuleNotFoundError and the
        # tab explains itself instead of the window failing to start.
        from giscode.gui.pro_tabs import build_analyze_tab

        build_analyze_tab(self)

    def _create_agent_registry_tab(self) -> None:
        # Pro-only: the builder lives in giscode.gui.pro_tabs, which the
        # Core export does not ship. Importing it here is the module
        # boundary: a build without it raises ModuleNotFoundError and the
        # tab explains itself instead of the window failing to start.
        from giscode.gui.pro_tabs import build_agent_registry_tab

        build_agent_registry_tab(self)

    def _create_settings_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=10)
        self._notebook.add(frame, text="Settings")
        self._tab_frames["Settings"] = frame

        ttk.Label(frame, text="Settings", font=("Segoe UI", 12, "bold")).pack(pady=10)

        # Model settings: the essentials every Core user needs, pointing at the
        # Model Manager rather than duplicating it.
        model_section = ttk.LabelFrame(frame, text="Model", padding=10)
        model_section.pack(fill=tk.X, pady=10)
        ttk.Label(model_section, textvariable=self._model_status_var).pack(anchor=tk.W)
        ttk.Button(
            model_section, text="Open Model Manager", command=self._on_model_manager
        ).pack(anchor=tk.W, pady=(6, 0))
        ttk.Label(
            model_section,
            text="Browse, choose and switch models in the Model Manager tab.",
            foreground="gray",
        ).pack(anchor=tk.W, pady=(4, 0))

        # Hosted writer and self-improvement settings are advanced (Pro) only.
        # Core says so plainly and stops here instead of exposing controls it
        # cannot support.
        if not edition.is_pro():
            advanced = ttk.LabelFrame(frame, text="Advanced settings", padding=10)
            advanced.pack(fill=tk.X, pady=10)
            ttk.Label(
                advanced,
                text=(
                    "Hosted-writer and self-improvement settings are part of "
                    "GisCode Pro. GisCode Core runs entirely on your own machine "
                    "with the model you choose in Model Manager."
                ),
                foreground="gray",
                wraplength=760,
                justify=tk.LEFT,
            ).pack(anchor=tk.W)
            return

        # Hosted writer settings (key never stored here - env only)
        hosted_section = ttk.LabelFrame(frame, text="Hosted writer", padding=10)
        hosted_section.pack(fill=tk.X, pady=10)

        self._hosted_enabled_var = tk.BooleanVar(
            value=bool(self._settings_get("hosted.enabled", False))
        )
        self._hosted_model_var = tk.StringVar(
            value=str(self._settings_get("hosted.model", "minimax-m3"))
        )
        self._hosted_base_url_var = tk.StringVar(
            value=str(self._settings_get("hosted.base_url", "https://opencode.ai/zen/v1"))
        )
        self._hosted_api_style_var = tk.StringVar(
            value=str(self._settings_get("hosted.api_style", "auto"))
        )

        ttk.Checkbutton(
            hosted_section,
            text="Use hosted writer (replaces the local planner/writer for GisAi tasks)",
            variable=self._hosted_enabled_var,
        ).pack(anchor=tk.W)

        grid = ttk.Frame(hosted_section)
        grid.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(grid, text="Model id:").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        ttk.Entry(grid, textvariable=self._hosted_model_var, width=44).grid(
            row=0, column=1, sticky=tk.W, pady=2
        )
        ttk.Label(grid, text="Base URL:").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        ttk.Entry(grid, textvariable=self._hosted_base_url_var, width=44).grid(
            row=1, column=1, sticky=tk.W, pady=2
        )
        ttk.Label(grid, text="API style:").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        ttk.Combobox(
            grid,
            textvariable=self._hosted_api_style_var,
            values=("auto", "chat", "messages", "responses"),
            width=12,
            state="readonly",
        ).grid(row=2, column=1, sticky=tk.W, pady=2)

        self._hosted_key_status_var = tk.StringVar(value="")
        ttk.Label(
            hosted_section, textvariable=self._hosted_key_status_var, foreground="gray"
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            hosted_section,
            text=(
                "The API key is read only from the GISCODE_HOSTED_API_KEY environment "
                "variable at call time; it is never saved in settings or logs. "
                "Changes take effect after restart."
            ),
            foreground="gray",
            wraplength=700,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 0))
        hosted_buttons = ttk.Frame(hosted_section)
        hosted_buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(
            hosted_buttons, text="Save hosted writer settings", command=self._save_hosted_settings
        ).pack(side=tk.LEFT)
        ttk.Button(
            hosted_buttons, text="Refresh key status", command=self._refresh_hosted_key_status
        ).pack(side=tk.LEFT, padx=(6, 0))
        self._refresh_hosted_key_status()

        # Self-Improvement settings
        si_section = ttk.LabelFrame(frame, text="Self-Improvement", padding=10)
        si_section.pack(fill=tk.X, pady=10)
        ttk.Label(si_section, text="Existing service status is shown when connected; no backend is fabricated.", foreground="gray").pack(pady=10)

    def _refresh_hosted_key_status(self) -> None:
        var = getattr(self, "_hosted_key_status_var", None)
        if var is None:
            return
        try:
            from giscode.inference.hosted import masked_key_proof

            var.set(f"API key status: {masked_key_proof()} (env GISCODE_HOSTED_API_KEY)")
        except Exception:
            var.set("API key status: unavailable")

    def _save_hosted_settings(self) -> None:
        if getattr(self, "_hosted_enabled_var", None) is None:
            # Pro-only controls, absent in Core.
            return
        saved = all(
            (
                self._settings_set("hosted.enabled", bool(self._hosted_enabled_var.get())),
                self._settings_set(
                    "hosted.model",
                    str(self._hosted_model_var.get()).strip() or "minimax-m3",
                ),
                self._settings_set(
                    "hosted.base_url",
                    str(self._hosted_base_url_var.get()).strip()
                    or "https://opencode.ai/zen/v1",
                ),
                self._settings_set(
                    "hosted.api_style",
                    str(self._hosted_api_style_var.get()).strip() or "auto",
                ),
            )
        )
        self._refresh_hosted_key_status()
        if saved:
            self._append_response(
                "[Settings] Hosted writer settings saved. Restart GisCode for "
                "them to take effect."
            )
        else:
            self._append_response(
                "[Settings] Could not save hosted writer settings: settings "
                "source unavailable."
            )

    def _create_model_manager_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=10)
        self._notebook.add(frame, text="Model Manager")
        self._tab_frames["Model Manager"] = frame
        ttk.Label(frame, text="Model Manager", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W, pady=(0, 10))
        ttk.Label(
            frame,
            textvariable=self._backend_note_var,
            foreground="gray",
            wraplength=760,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))
        ttk.Label(frame, textvariable=self._model_status_var).pack(anchor=tk.W)
        self._model_error_label = ttk.Label(
            frame,
            textvariable=self._model_error_var,
            foreground="red",
            wraplength=760,
            justify=tk.LEFT,
        )
        self._model_error_label.pack(anchor=tk.W, pady=(4, 0))
        # Grey, not red: a CPU-only load is a fact about the machine, not a
        # failure. Kept in its own variable so the error line stays truthful.
        self._model_note_label = ttk.Label(
            frame,
            textvariable=self._model_note_var,
            foreground="#555555",
            wraplength=760,
            justify=tk.LEFT,
        )
        self._model_note_label.pack(anchor=tk.W, pady=(4, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(anchor=tk.W, pady=12)
        self.load_model_btn = ttk.Button(buttons, text="Load selected model", command=self._load_selected_model)
        self.load_model_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.unload_model_btn = ttk.Button(buttons, text="Unload current model", command=self._unload_current_model)
        self.unload_model_btn.pack(side=tk.LEFT)
        ttk.Label(frame, text="Model loading remains owned by ModelManager and the existing deferred startup worker.", foreground="gray").pack(anchor=tk.W)
        # Providers, profiles and the two tests are additive: the GGUF path above
        # keeps working exactly as it did, and this section adds other local
        # runtimes beside it. Kept in its own module so this file stays readable.
        from giscode.gui.provider_panel import build_provider_panel

        # A broken provider panel must not take the window down with it: the
        # GGUF path above has to keep working. The failure is shown, not hidden.
        try:
            self._provider_panel = build_provider_panel(frame, self)
        except Exception as _provider_exc:  # pragma: no cover - defensive
            self._provider_panel = None
            ttk.Label(
                frame,
                text=(
                    "Provider panel unavailable: "
                    f"{type(_provider_exc).__name__}: {_provider_exc}"
                ),
                foreground="red",
                wraplength=900,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(8, 0))

    def _create_project_doctor_tab(self) -> None:
        # Pro-only: the builder lives in giscode.gui.pro_tabs, which the
        # Core export does not ship. Importing it here is the module
        # boundary: a build without it raises ModuleNotFoundError and the
        # tab explains itself instead of the window failing to start.
        from giscode.gui.pro_tabs import build_project_doctor_tab

        build_project_doctor_tab(self)

    def _create_history_tab(self) -> None:
        # Pro-only: the builder lives in giscode.gui.pro_tabs, which the
        # Core export does not ship. Importing it here is the module
        # boundary: a build without it raises ModuleNotFoundError and the
        # tab explains itself instead of the window failing to start.
        from giscode.gui.pro_tabs import build_history_tab

        build_history_tab(self)

    def _build_history_panel(self, frame: ttk.Frame) -> ttk.Frame:
        """Build the conversation-history panel into *frame*.

        Pro hosts it as its own tab; Core hosts the same panel inside Chat, so
        history, session recovery and persistence behave identically in both
        editions and neither duplicates the code.
        """
        ttk.Label(frame, text="Conversation History", font=("Segoe UI", 12, "bold")).pack(
            anchor=tk.W, pady=(0, 8)
        )
        body = ttk.Frame(frame)
        body.pack(fill=tk.BOTH, expand=True)

        self._conversation_history_listbox = tk.Listbox(body, height=12, exportselection=False)
        self._conversation_history_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._conversation_history_listbox.bind("<<ListboxSelect>>", self._show_conversation_detail)
        add_text_context_menu(
            self._conversation_history_listbox, editable=False, name="Conversation"
        )


        detail = ttk.Frame(body)
        detail.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        self._conversation_detail = tk.Text(detail, state=tk.DISABLED, wrap=tk.WORD)
        self._conversation_detail.pack(fill=tk.BOTH, expand=True)
        add_text_context_menu(self._conversation_detail, editable=False)


        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="Open Conversation", command=self._open_conversation).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Rename", command=self._rename_selected_conversation).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(buttons, text="Delete", command=self._delete_selected_conversation).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(buttons, text="New Chat", command=self._new_history_conversation).pack(
            side=tk.RIGHT
        )
        self._refresh_conversation_history()
        return frame

    def _create_adspace_tab(self) -> None:
        """Core only: the GisCode AdSpace panel (Pro is ad-free)."""
        from giscode.ads.service import AdSpace
        from giscode.gui.adspace import AdSpacePanel

        frame = ttk.Frame(self._notebook)
        self._notebook.add(frame, text="AdSpace")
        self._tab_frames["AdSpace"] = frame
        self._adspace_panel = AdSpacePanel(frame, service=AdSpace(settings=_SettingsBridge(self)))
        self._adspace_panel.pack(fill=tk.BOTH, expand=True)

    def _create_status_tab(self) -> None:
        # Pro-only: the builder lives in giscode.gui.pro_tabs, which the
        # Core export does not ship. Importing it here is the module
        # boundary: a build without it raises ModuleNotFoundError and the
        # tab explains itself instead of the window failing to start.
        from giscode.gui.pro_tabs import build_status_tab

        build_status_tab(self)

    def _create_ios_xtool_tab(self) -> None:
        from giscode.gui.ios_xtool_view import IosXtoolView

        frame = ttk.Frame(self._notebook, padding=4)
        self._notebook.add(frame, text="iOS / xtool")
        self._tab_frames["iOS / xtool"] = frame
        self._ios_xtool_view = IosXtoolView(frame, service=self._iosxtool_service)

    def _create_gislogo_tab(self) -> None:
        # Pro-only: imported here so Core never loads the branding designer.
        from giscode.gui.gislogo_view import GisLogoView

        frame = ttk.Frame(self._notebook, padding=4)
        self._notebook.add(frame, text="GisLogo")
        self._tab_frames["GisLogo"] = frame
        self._gislogo_view = GisLogoView(frame, gisai_engine=self._gisai_engine)

    def _load_selected_model(self) -> None:
        self._on_model_selected()

    def _on_model_selected(self, _event=None) -> None:
        if self._model_manager is None:
            self._status_var.set("Model manager unavailable")
            return
        selected = self._model_var.get()
        if not selected or selected == "Auto":
            return
        if self._model_switch_thread is not None and self._model_switch_thread.is_alive():
            self._status_var.set("A model switch is already in progress")
            return
        current = self._model_manager.get_current_model()
        self._model_switch_previous = getattr(current, "name", "Auto") if current is not None else "Auto"
        self._model_switch_epoch += 1
        epoch = self._model_switch_epoch
        self._model_switch_active = True
        self._model_status_var.set(f"Loading {selected}, this can take a few minutes...")
        self._status_var.set(f"Switching model to {selected}...")
        try:
            from giscode.inference.runtime import estimate_ready_timeout
            mm = self._model_manager._inference.get_model_status()
            if mm is not None:
                mins = estimate_ready_timeout(mm.path) / 60.0
                if mins >= 1.0:
                    self._model_status_var.set(f"Loading {selected}, this can take {mins:.0f} minutes...")
        except Exception:
            pass
        self._model_switch_thread = threading.Thread(
            target=self._load_model_worker,
            args=(selected, epoch),
            daemon=True,
            name="giscode-model-switch",
        )
        self._model_switch_thread.start()

    def _load_model_worker(self, selected: str, epoch: int) -> None:
        try:
            metadata = self._model_manager.load_model(selected)
            if metadata is None:
                metadata = self._model_manager.get_current_model()
            if metadata is None:
                raise RuntimeError("model load completed without a current model")
            result = (metadata, None, epoch)
        except Exception as exc:
            result = (None, exc, epoch)
        self._model_switch_results.put(result)

    def _finish_model_switch(self, metadata, error, epoch: int) -> None:
        if epoch != self._model_switch_epoch:
            return
        self._model_switch_active = False
        if error is not None:
            reason = str(error) or type(error).__name__
            self._last_model_load_error = reason
            self._model_var.set(self._model_switch_previous)
            self._model_status_var.set(f"Model switch failed: {reason}")
            self._status_var.set(f"Model switch failed: {reason}")
            return
        self._model_var.set(metadata.name)
        self._last_model_load_error = ""
        self._model_status_var.set(f"{metadata.name} (Ready)")
        self._status_var.set(f"Model loaded: {metadata.name}")
        note = ""
        try:
            from giscode.inference.runtime import explain_cpu_only_load

            note = explain_cpu_only_load(
                getattr(metadata, "gpu_layers", 0), getattr(metadata, "name", "")
            )
        except Exception:
            note = ""
        self._set_model_note(note)

    def _unload_current_model(self) -> None:
        if self._model_manager is None:
            self._status_var.set("Model manager unavailable")
            return
        getter = getattr(self._model_manager, "get_current_model", None)
        current = getter() if getter is not None else None
        if current is not None:
            self._model_manager.unload_model(current.name)
        self._set_model_note("")

    def _on_terminal_run(self) -> None:
        cmd = self._terminal_input_var.get().strip()
        if not cmd:
            return
        self._terminal_input_var.set("")
        self._terminal_output.config(state=tk.NORMAL)
        self._terminal_output.insert(tk.END, f"$ {cmd}\n")
        self._terminal_output.config(state=tk.DISABLED)
        self._terminal_output.see(tk.END)

        # Run command in background
        def run_cmd():
            import subprocess
            try:
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=".")
                output = result.stdout
                if result.stderr:
                    output += "\n" + result.stderr
            except subprocess.TimeoutExpired:
                output = "Command timed out"
            except Exception as e:
                output = f"Error: {e}"

            self.root.after(0, lambda: self._append_terminal_output(output))

        import threading
        threading.Thread(target=run_cmd, daemon=True).start()

    def _append_terminal_output(self, output: str) -> None:
        self._terminal_output.config(state=tk.NORMAL)
        self._terminal_output.insert(tk.END, output + "\n")
        self._terminal_output.config(state=tk.DISABLED)
        self._terminal_output.see(tk.END)

    def _show_startup_model_note(self) -> None:
        """Say once that the saved model was not found, and what is used instead.

        Model autoload resolves the saved selection before this window exists,
        so the fact is recorded on the model manager and shown here - once - in
        the Model Manager note line ("Saved model 'X' was not found; using
        'Y'", or "...; no model loaded"). It reports which model was picked;
        it never changes that choice.
        """
        if getattr(self, "_startup_model_note_shown", False):
            return
        if self._model_manager is None:
            return
        note = str(getattr(self._model_manager, "startup_model_note", "") or "")
        if not note:
            return
        self._startup_model_note_shown = True
        self._set_model_note(note)

    def _poll_model_state(self) -> None:
        self._refresh_backend_note()
        self._show_startup_model_note()
        if self._model_manager is None:
            self._update_window_title(self._compose_title(None, APP_TITLE))
            self.root.after(1000, self._poll_model_state)
            return
        if self._drain_model_switch_results():
            self._refresh_window_title_from_state()
            self.root.after(1000, self._poll_model_state)
            return
        if self._model_switch_active:
            self._update_window_title(self._compose_title("Loading...", APP_TITLE))
            self.root.after(1000, self._poll_model_state)
            return
        getter = getattr(self._model_manager, "get_current_model", None)
        current = getter() if getter is not None else None
        if current is not None:
            state = getattr(getattr(current, "state", None), "value", "Ready")
            selector_name = current.name
            try:
                current_path = os.path.normcase(os.path.abspath(current.path))
                for registered in self._model_manager.get_registry().list_all():
                    if os.path.normcase(os.path.abspath(getattr(registered, "path", ""))) == current_path:
                        selector_name = registered.name
                        break
            except Exception:
                pass
            self.model_name_var.set(selector_name)
            self.model_state_var.set(state.capitalize())
            ctx = getattr(current, "context_length", None)
            ctx_text = f" [n_ctx={ctx}]" if ctx else ""
            layers_text = f" gpu_layers={getattr(current, 'gpu_layers', 0)}"
            self._model_status_var.set(f"{current.name} ({state.capitalize()}){ctx_text}{layers_text}")
            if selector_name in list(self._model_combo["values"] or ()):
                self._model_var.set(selector_name)
            message = None
            if state.lower() == "ready":
                self._set_model_error("")
                message = f"Loaded: {current.name}{ctx_text}"
            elif state.lower() == "error":
                err = getattr(current, "error_message", "") or "load failed"
                self._set_model_error(err)
                message = f"Error: {current.name} - {err}"
            self._update_window_title(self._compose_title(message, APP_TITLE))
        else:
            settings = getattr(self._model_manager, "_settings", {})
            selected = settings.get("model.selected_model", "") if hasattr(settings, "get") else ""
            self.model_name_var.set(selected or "None")
            self.model_state_var.set("Unloaded")
            self._model_status_var.set(selected or "No model")
            last_error = self._last_model_load_error or self._model_metadata_error()
            if last_error:
                self._set_model_error(last_error)
                self._update_window_title(self._compose_title(f"No model - {last_error}", APP_TITLE))
            else:
                self._set_model_error("")
                self._update_window_title(self._compose_title("No model loaded", APP_TITLE))
        self.root.after(1000, self._poll_model_state)

    def _model_metadata_error(self) -> str:
        """Return the last load-failure message recorded on the selected model,
        so the window header can warn the user when 'No model loaded' is the
        result of a real error rather than an empty selection."""
        if self._model_manager is None:
            return ""
        try:
            settings = getattr(self._model_manager, "_settings", {})
            selected = settings.get("model.selected_model", "") if hasattr(settings, "get") else ""
            if not selected:
                return ""
            meta = self._model_manager.get_registry().get(selected)
        except Exception:
            return ""
        if meta is None:
            return ""
        err = getattr(meta, "error_message", None)
        if err and "Insufficient resources" not in str(err):
            return str(err)
        if err and "Insufficient resources" in str(err):
            return str(err)
        return ""

    def _compose_title(self, message: Optional[str], base: str) -> str:
        if message:
            return f"{base} - {message}"
        return base

    def _update_window_title(self, title: str) -> None:
        try:
            if getattr(self, "_last_title_state", None) != title:
                self.root.title(title)
                self._last_title_state = title
        except Exception:
            try:
                self.root.title(title)
            except Exception:
                pass

    def _refresh_window_title_from_state(self) -> None:
        getter = getattr(self._model_manager, "get_current_model", None) if self._model_manager else None
        current = getter() if getter is not None else None
        if current is not None:
            ctx = getattr(current, "context_length", None)
            ctx_text = f" [n_ctx={ctx}]" if ctx else ""
            self._set_model_error("")
            self._update_window_title(self._compose_title(f"Loaded: {current.name}{ctx_text}", APP_TITLE))
        else:
            err = self._last_model_load_error or self._model_metadata_error()
            if err:
                self._set_model_error(err)
                self._update_window_title(self._compose_title(f"No model - {err}", APP_TITLE))
            else:
                self._set_model_error("")
                self._update_window_title(self._compose_title("No model loaded", APP_TITLE))

    def _set_model_error(self, message: str) -> None:
        """Show (or clear) the model-loading error in the Model Manager tab.

        Called from the existing model poll loop on the Tk thread. The error
        variable and label may not exist yet during early startup, so a
        missing or unusable variable is ignored instead of raising.
        """
        var = getattr(self, "_model_error_var", None)
        if var is None:
            return
        try:
            var.set(message or "")
        except Exception:
            pass

    def _set_model_note(self, message: str) -> None:
        """Show (or clear) the grey backend/CPU-only note in Model Manager."""
        var = getattr(self, "_model_note_var", None)
        if var is None:
            return
        try:
            var.set(message or "")
        except Exception:
            pass

    def _refresh_backend_note(self) -> None:
        """Fill the backend sentence once the runtime has resolved it.

        The runtime resolves the build once per session; until then (and in
        tests that inject a mock runtime) this stays empty rather than probing
        again from the Tk thread.
        """
        var = getattr(self, "_backend_note_var", None)
        if var is None:
            return
        try:
            from giscode.inference.runtime import describe_backend_choice

            text = describe_backend_choice()
            if text and text != var.get():
                var.set(text)
        except Exception:
            pass

    def _drain_model_switch_results(self) -> bool:
        """Apply worker results on the Tk thread via the existing poll loop."""
        processed = False
        while True:
            try:
                metadata, error, epoch = self._model_switch_results.get_nowait()
            except queue.Empty:
                return processed
            self._finish_model_switch(metadata, error, epoch)
            processed = True

    def _poll_task_state(self) -> None:
        if self._task_manager is None:
            return
        if self._active_task and self._task_clock_expired():
            self._force_task_timeout()
            return
        if self._active_gisai_task is not None and self._active_task:
            live = self._live_activity_fresh()
            label = live or self._activity_label_for_gisai(self._active_gisai_task)
            self.phase_var.set(label)
            self.status_var.set(f"GisAi: {label.lower()}")
            self.root.after(250, self._poll_task_state)
            return
        if not self._active_task and self._terminal_state_until and time.time() >= self._terminal_state_until:
            self.phase_var.set("Idle")
            self._terminal_state_until = 0.0
        if self._active_task and self._task_clock_expired():
            self._force_task_timeout()
            return
        task = self._task_manager.get_active_task()
        if task is not None:
            self._status_var.set(f"{task.current_phase.value.capitalize()}: {task.status.value.capitalize()} ({self._task_manager.get_elapsed():.1f}s)")
        else:
            self._status_var.set("Ready")
        self.root.after(250, self._poll_task_state)

    def _force_task_timeout(self) -> None:
        """Stop the task at the time limit and report the real outcome.

        The report is built before the handles are cleared so it can still see
        the running task and its snapshot, and it is written through the normal
        report path, which is what puts it in the chat and in the History tab.
        """
        report = self._task_stop_report(kind="timeout")
        self._task_epoch += 1
        epoch = self._task_epoch
        try:
            if self._gisai_engine is not None:
                self._gisai_engine.cancel()
        except Exception:
            pass
        self._active_task = False
        self._active_gisai_task = None
        self._show_chat_result(report, None, time.time() - self._task_start_time, epoch)
        self._set_ui_state("STOPPED")
        self.phase_var.set("Timeout")
        if self._task_manager is not None:
            try:
                self._task_manager.cancel()
            except Exception:
                pass

    def _reset_task_ui(self, epoch: int) -> None:
        if epoch != self._task_epoch:
            return
        self._active_task = False
        self._set_ui_state("IDLE")
        if getattr(self, "_last_outcome", "") == "waiting":
            self.phase_var.set("Waiting for input")
        else:
            self.phase_var.set("Idle")

    def _append_if_current(self, epoch: int, content: str) -> None:
        if epoch == self._task_epoch:
            self._append_response(content)

    def _clear_active_gisai_task(self) -> None:
        self._active_gisai_task = None

    def _live_activity_fresh(self) -> Optional[str]:
        if getattr(self, "_live_activity", None) and time.time() - getattr(self, "_live_activity_at", 0) < 10:
            return self._live_activity
        return None

    def _activity_label_for_gisai(self, task) -> str:
        status = getattr(getattr(task, "status", None), "value", getattr(task, "status", ""))
        state = getattr(getattr(task, "state", None), "value", getattr(task, "state", ""))
        if status == "completed" or state == "complete":
            return "Completed"
        if status == "cancelled" or state == "cancelled":
            return "Cancelled"
        if status == "timeout" or (getattr(task, "failure_info", None) and task.failure_info.get("reason") == "execution_timeout"):
            return "Timeout"
        if status == "failed" or state == "failed":
            return "Error"
        if state == "waiting_input":
            return "Waiting for input"
        if state == "planning":
            return "Planning"
        if state == "evaluating":
            return "Verifying"
        return "Thinking"

    def _dispatch_result(self, result, elapsed: float, epoch: int) -> None:
        if epoch != self._task_epoch:
            return
        if isinstance(result, dict) and result.get("status") == "waiting_for_input":
            self._last_outcome = "waiting"
            self._set_ui_state("IDLE")
            self.phase_var.set("Waiting for input")
            self._append_response("Waiting for input.")
            return
        self._show_chat_result(self._result_text(result), None, elapsed, epoch)

    def _poll_coding_agent_state(self) -> None:
        if self._coding_agent is None:
            return
        result = self._coding_agent.get_current_state()
        if result is not None and result.status is not None:
            status = getattr(result.status, "value", result.status)
            phase = getattr(result, "phase", status)
            phase = getattr(phase, "value", phase)
            self.phase_var.set(self._live_activity_fresh() or str(phase).capitalize())
            self.agent_phase_var.set(str(phase).capitalize())
            self.agent_status_var.set(str(status).capitalize())
            if not self._active_task:
                self.status_var.set(f"{getattr(result, 'task_id', '')}: {str(status).capitalize()}")
        else:
            self.agent_phase_var.set("Idle")
            self.agent_status_var.set("No active task")
        self.root.after(250, self._poll_coding_agent_state)

    def _poll_gisai_state(self) -> None:
        if self._gisai_engine is None:
            self.gisai_phase_var.set("Unavailable")
            self.gisai_status_var.set("Unavailable")
            if hasattr(self, "_gisai_header_label"):
                self._gisai_header_label.config(text="GisAI — Unavailable")
            self.root.after(1000, self._poll_gisai_state)
            return
        model_loaded = False
        if self._model_manager is not None:
            try:
                model_loaded = self._model_manager.get_current_model() is not None
            except Exception:
                model_loaded = False
        task = self._gisai_engine.get_active_task()
        if task is not None:
            status = getattr(task.status, "value", str(task.status))
            state = getattr(task.state, "value", str(task.state))
            self.gisai_phase_var.set(state.capitalize())
            self.gisai_status_var.set(status.capitalize())
            if hasattr(self, "_gisai_header_label"):
                self._gisai_header_label.config(text=f"GisAI — {state.capitalize()}")
        else:
            # Use the live activity if fresh, otherwise show Ready
            live = self._live_activity_fresh()
            if live:
                self.gisai_phase_var.set("Working")
                self.gisai_status_var.set(live)
                if hasattr(self, "_gisai_header_label"):
                    self._gisai_header_label.config(text=f"GisAI — {live}")
            else:
                self.gisai_phase_var.set("Ready")
                self.gisai_status_var.set("Ready" if model_loaded else "No model loaded")
                if hasattr(self, "_gisai_header_label"):
                    self._gisai_header_label.config(
                        text="GisAI — Ready" if model_loaded else "GisAI — No model loaded"
                    )
        self.root.after(500, self._poll_gisai_state)

    def _poll_terminal_state(self) -> None:
        if self._terminal_executor is None:
            return
        self.terminal_status_var.set("Ready")
        self.terminal_elapsed_var.set("0.0s")
        self.root.after(1000, self._poll_terminal_state)

    def _poll_sandbox_state(self) -> None:
        if self._sandbox_executor is None:
            return
        status = self._sandbox_executor.get_status()
        self.sandbox_status_var.set(status.value.capitalize())
        self.sandbox_backend_var.set(self._sandbox_executor.backend.value.capitalize())
        self.root.after(1000, self._poll_sandbox_state)

    def _poll_orchestrator_state(self) -> None:
        if self._orchestrator is None:
            return
        status = self._orchestrator.get_status()
        self.orchestration_phase_var.set(status.value.capitalize())
        current = getattr(self._orchestrator, '_current_result', None)
        if current is not None:
            self.orchestration_attempt_var.set(f"{current.current_attempt} / {current.max_attempts}")
        else:
            self.orchestration_attempt_var.set("0 / 0")
        self.root.after(1000, self._poll_orchestrator_state)

    def _poll_openhands_state(self) -> None:
        if self._openhands_adapter is None:
            return
        status = self._openhands_adapter.get_status()
        self.openhands_status_var.set(status.value.capitalize())
        runtime_state = getattr(self._openhands_adapter, 'get_runtime_state', None)
        if runtime_state is not None:
            state = self._openhands_adapter.get_runtime_state()
            self.openhands_runtime_var.set(state.replace("_", " ").capitalize())
        else:
            self.openhands_runtime_var.set("-")
        self.root.after(1000, self._poll_openhands_state)

    def _poll_knowledge_state(self) -> None:
        if self._knowledge_service is None:
            return
        count = self._knowledge_service.count()
        self.knowledge_count_var.set(f"{count} records")
        self.root.after(2000, self._poll_knowledge_state)

    def _poll_routing_state(self) -> None:
        if self._routing_service is None:
            return
        result = getattr(self._routing_service, '_last_result', None)
        if result is not None:
            self.routing_role_var.set(str(result.selected_role.value if result.selected_role else '-'))
            self.routing_model_var.set(str(result.selected_model or '-'))
            self.routing_status_var.set(result.availability.value.capitalize())
        else:
            self.routing_role_var.set("-")
            self.routing_model_var.set("-")
            self.routing_status_var.set("-")
        self.root.after(2000, self._poll_routing_state)

    def _poll_ocr_state(self) -> None:
        if self._ocr_service is None:
            return
        engine = self._ocr_service._engine
        status = getattr(engine, '_last_status', None)
        if status is None:
            status = OCRStatus.UNAVAILABLE if not engine.is_available() else OCRStatus.READY
        self.ocr_status_var.set(status.value.capitalize())
        self.root.after(2000, self._poll_ocr_state)

    def _poll_monitoring_state(self) -> None:
        if self._monitoring_service is None:
            return
        self.monitoring_status_var.set(self._monitoring_service.status.value.capitalize())
        self.root.after(2000, self._poll_monitoring_state)

    def _poll_packaging_state(self) -> None:
        if self._packaging_builder is None:
            return
        self.packaging_status_var.set(self._packaging_builder.status.value.capitalize())
        config = getattr(self._packaging_builder, '_config', None)
        if config is not None:
            self.packaging_type_var.set(getattr(config, 'package_type', '-').capitalize())
        self.root.after(5000, self._poll_packaging_state)

    def _poll_signing_state(self) -> None:
        if self._signing_service is None:
            return
        self.signing_status_var.set(self._signing_service.get_status().value.capitalize())
        self.root.after(5000, self._poll_signing_state)

    def _poll_self_improvement_state(self) -> None:
        if self._self_improvement_service is None:
            return
        status = "Enabled" if self._self_improvement_service.enabled else "Disabled"
        self.self_improvement_status_var.set(status)
        self.root.after(5000, self._poll_self_improvement_state)

    def _poll_research_state(self) -> None:
        if self._research_service is None:
            return
        status = "Enabled" if self._research_service.enabled else "Disabled"
        provider = "Available" if self._research_service.provider_available else "Unavailable"
        self.research_status_var.set(f"{status} ({provider})")
        self.root.after(5000, self._poll_research_state)

    def set_task_controller(self, controller: Optional[object]) -> None:
        self._task_controller = controller

    def update_task_display(self, phase: str, status: str, elapsed: float) -> None:
        self._status_var.set(f"{phase}: {status} ({elapsed:.1f}s)")
