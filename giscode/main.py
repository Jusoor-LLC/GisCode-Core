from __future__ import annotations

import multiprocessing
import os
import json
import stat
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from giscode.agentic.loop import CodingAgentLoop
from giscode.config.settings import SettingsManager
from giscode.inference.api import InferenceAPI
from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.service import KnowledgeService
from giscode.logging.logger import default_log_file, get_logger, setup_logger
from giscode.models.manager import ModelManager
from giscode.ocr.config import OCRConfig
from giscode.ocr.engine import MockOCREngine
from giscode.ocr.service import OCRService
from giscode.openhands.adapter import MockOpenHandsAdapter, RealOpenHandsAdapter
from giscode.openhands.config import OpenHandsConfig
from giscode.orchestration.orchestrator import BuildTestOrchestrator
from giscode.packaging.config import PackagingConfig, SigningConfig
from giscode.packaging.builder import PackagingBuilder, SigningService
from giscode.monitoring.config import MonitoringConfig
from giscode.monitoring.service import MonitoringService
from giscode.routing.service import RoutingService
from giscode.sandbox.executor import SandboxExecutor
from giscode.self_improvement.config import SelfImprovementConfig
from giscode.self_improvement.service import SelfImprovementService
from giscode.task_control.manager import TaskManager
from giscode.terminal.executor import CommandExecutor
from giscode.research.config import ResearchConfig
from giscode.research.service import ResearchService
from giscode.workspace.workspace import Workspace
# GisAI is optional: a Core-only bundle does not contain giscode.gisai. These
# names are only used by bootstrap's guarded branch and by the tool registration
# helper it calls, so an absent package must not break importing this module.
try:
    from giscode.gisai.engine import GisAiEngine
    from giscode.gisai.tools.adapters import (
        AddSymbolTool,
        EditFileTool,
        ProjectRootCommandExecutor,
        ProjectRootWorkspaceExecutor,
        ReadFileTool,
        ReplaceSymbolTool,
        RunCommandTool,
        SetDocstringTool,
        WriteFileTool,
    )
except ImportError as exc:
    # Only the absence of the package itself is tolerated: a missing package
    # names "giscode.gisai", and a build that blocks it may raise an
    # ImportError with no name at all. A missing *submodule* means the
    # install is broken, which must stay visible instead of silently
    # disabling GisAI.
    name = exc.name or ""
    if name and name != "giscode.gisai":
        raise
    GisAiEngine = None
    AddSymbolTool = EditFileTool = ProjectRootCommandExecutor = None
    ProjectRootWorkspaceExecutor = ReadFileTool = ReplaceSymbolTool = None
    RunCommandTool = SetDocstringTool = WriteFileTool = None


def _is_frozen() -> bool:
    """True when running from a PyInstaller bundle (one-file/onedir)."""
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def _resolve_candidate(value: "str | Path | None") -> "Path | None":
    try:
        return Path(value).expanduser().resolve() if value else None
    except (OSError, TypeError, ValueError):
        return None


def _is_transient_extraction_root(root: Path) -> bool:
    """The PyInstaller extraction dir (``sys._MEIPASS``) must never be used as
    the project root: it is a transient temp directory, not the real project."""
    meipass = _resolve_candidate(getattr(sys, "_MEIPASS", None))
    if meipass is not None and root is not None:
        if root == meipass:
            return True
        try:
            root.relative_to(meipass)
            return True
        except ValueError:
            pass
    if root is not None and root.name.startswith("_MEI"):
        return True
    return False


def _is_valid_project_root(root: "Path | None") -> bool:
    if root is None or _is_transient_extraction_root(root):
        return False
    try:
        if not root.is_dir():
            return False
    except OSError:
        return False
    # Frozen-safe markers: the giscode package is bytecode inside the PYZ in a
    # packaged app (never a real directory), so also accept repo markers.
    return (
        (root / "giscode").is_dir()
        or (root / ".git").is_dir()
        or (root / "GisCode.spec").is_file()
        or (root / "pyproject.toml").is_file()
    )


def _built_project_root() -> "str | None":
    """Root embedded into the bundle at build time (GisCode.spec), delivered as
    a data file under ``sys._MEIPASS/giscode_build``. Reliable even when the
    packaged exe was copied away from its original location."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    marker = Path(meipass) / "giscode_build" / "giscode_project_root.txt"
    try:
        return marker.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def detect_current_giscode_project(giscode_package_dir: "Path | None" = None) -> "ProjectContext | None":
    """Resolve GisCode's *current* project to an active project context.

    The current project is the source root that this running ``giscode``
    package belongs to (for the standard checkout, the directory that contains
    the ``giscode`` package, e.g. ``Desktop\\Projects\\GisCode``). It is
    derived from the application's own location - never from a persisted
    session and never from the app-level projects directory - so GisAi and its
    real-work tools are bound to the actual project root instead of silently
    falling back to ``paths.projects_dir``.

    Resolution order:
      1. ``GISCODE_PROJECT_ROOT`` override (highest priority).
      2. Frozen builds only:
         a. the build-embedded project root data marker;
         b. the directory containing the running executable's ``dist`` folder
            (``Path(sys.executable).parent.parent``).
      3. Source checkout: the directory that contains the running ``giscode``
         package (``Path(__file__).parent.parent``, or the
         ``giscode_package_dir`` override).

    The PyInstaller extraction directory (``sys._MEIPASS``) is never accepted;
    every candidate is validated with project markers before adoption.

    Returns ``None`` when no project can be identified; callers then keep the
    existing safe no-project behavior (app-level projects directory fallback).
    """
    from giscode.projects.context import ProjectContext

    candidates: "list[Path | None]" = []

    env_root = os.environ.get("GISCODE_PROJECT_ROOT")
    if env_root:
        candidates.append(_resolve_candidate(env_root))

    if _is_frozen():
        candidates.append(_resolve_candidate(_built_project_root()))
        try:
            candidates.append(Path(sys.executable).resolve().parent.parent)
        except (OSError, TypeError, ValueError):
            pass
    else:
        try:
            pkg_dir = Path(giscode_package_dir or Path(__file__).resolve().parent).resolve()
            candidates.append(pkg_dir.parent)
        except (OSError, TypeError, ValueError):
            pass

    for root in candidates:
        if _is_valid_project_root(root):
            return ProjectContext.create(str(root), name="GisCode", section="Overview")
    return None


def bootstrap() -> tuple[SettingsManager, Workspace, TaskManager, InferenceAPI, ModelManager, CodingAgentLoop, CommandExecutor, SandboxExecutor, BuildTestOrchestrator, MockOpenHandsAdapter, KnowledgeService, RoutingService, OCRService, MonitoringService, PackagingBuilder, SigningService, SelfImprovementService, ResearchService, GisAiEngine]:
    # A windowed build has no console, so without a log file there is no record
    # of anything that happened. Rotating keeps it bounded.
    log_file = default_log_file()
    setup_logger(log_file=log_file)
    logger = get_logger("giscode.main")
    logger.info("Bootstrapping GisCode...")
    logger.info("Logging to %s", log_file)

    settings = SettingsManager()
    workspace = Workspace(settings.get("paths.projects_dir"))
    task_manager = TaskManager()
    n_ctx = int(settings.get("inference.context_length", 16384)) or 16384
    n_gpu_layers = int(settings.get("model.gpu_layers", 0))
    inference = InferenceAPI(n_ctx=n_ctx, n_gpu_layers=n_gpu_layers)
    model_manager = ModelManager(settings, inference)
    knowledge_config = KnowledgeStoreConfig()
    knowledge_service = KnowledgeService(config=knowledge_config)
    terminal_executor = CommandExecutor(workspace)
    sandbox_executor = SandboxExecutor(workspace)
    openhands_config = OpenHandsConfig()
    real_adapter = RealOpenHandsAdapter(workspace, config=openhands_config, task_manager=task_manager)
    if real_adapter.runtime_available:
        openhands_adapter = real_adapter
        logger.info("OpenHands runtime detected")
    else:
        openhands_adapter = MockOpenHandsAdapter(workspace, config=openhands_config, task_manager=task_manager)
        logger.info("OpenHands runtime not available, using mock adapter")
    orchestrator = BuildTestOrchestrator(workspace, sandbox_executor, task_manager)
    routing_service = RoutingService(model_manager=model_manager, knowledge_service=knowledge_service)
    ocr_config = OCRConfig()
    ocr_service = OCRService(config=ocr_config, task_manager=task_manager, knowledge_service=knowledge_service)

    packaging_config = PackagingConfig(
        version=settings.get("app.version", "0.1.0"),
        output_dir=Path(settings.get("paths.packaging_output_dir", "dist")),
        artifacts_dir=Path(settings.get("paths.packaging_artifacts_dir", "build")),
    )
    packaging_builder = PackagingBuilder(config=packaging_config)

    signing_config = SigningConfig(
        enabled=settings.get("signing.enabled", False),
        timestamp_server=settings.get("signing.timestamp_server", "http://timestamp.digicert.com"),
    )
    signing_service = SigningService(config=signing_config)

    monitoring_config = MonitoringConfig(
        enabled=settings.get("monitoring.enabled", True),
        namespace=settings.get("monitoring.namespace", "default"),
        application_version=settings.get("app.version", "0.1.0"),
    )
    monitoring_service = MonitoringService(config=monitoring_config, knowledge_service=knowledge_service)

    self_improvement_config = SelfImprovementConfig(
        enabled=settings.get("self_improvement.enabled", True),
        namespace=settings.get("self_improvement.namespace", "default"),
        max_retrieved_patterns=settings.get("self_improvement.max_retrieved_patterns", 5),
        min_verification_status=settings.get("self_improvement.min_verification_status", "verified_pair"),
        dataset_export_path=settings.get("self_improvement.dataset_export_path", "build/self_improvement_dataset.json"),
    )
    self_improvement_service = SelfImprovementService(config=self_improvement_config, knowledge_service=knowledge_service)

    research_config = ResearchConfig(
        enabled=settings.get("research.enabled", True),
        provider=settings.get("research.provider", "mock"),
        max_results=settings.get("research.max_results", 5),
        timeout=settings.get("research.timeout", 30.0),
        max_query_length=settings.get("research.max_query_length", 500),
        max_response_size=settings.get("research.max_response_size", 8192),
    )
    research_service = ResearchService(config=research_config, knowledge_service=knowledge_service)

    # The active coding profile may point at Ollama, LM Studio or another local
    # endpoint. The router keeps the built-in InferenceAPI for llama.cpp targets
    # and gives the planner and coding loop the provider-backed backend
    # otherwise, so the approval gate, executor and verifier below are unchanged.
    from giscode.inference.router import ModelRouter

    routed_inference = ModelRouter(builtin=inference, settings=settings, purpose="coding")
    coding_agent = CodingAgentLoop(workspace, routed_inference, task_manager, knowledge_service=knowledge_service, routing_service=routing_service, ocr_service=ocr_service, self_improvement_service=self_improvement_service, research_service=research_service, settings=settings)

    try:
        from giscode.projects.context import ProjectContextStore
        store = ProjectContextStore()
        # Project identity isolation: the session store logs resumable history
        # and must never seed the active project. Instead, GisCode's *current*
        # project is established explicitly here (derived from the running
        # application's own location) so GisAi and its real-work tools are
        # bound to the actual project root - never to a foreign persisted
        # session, and never silently to the app-level projects directory.
        active_project_context = store.load_active_project_context()
        if active_project_context is None:
            active_project_context = detect_current_giscode_project()
    except Exception:
        active_project_context = detect_current_giscode_project()

    vision_service = None
    try:
        from giscode.vision.service import VisionService
        vision_service = VisionService(model_manager=model_manager, task_manager=task_manager)
    except Exception:
        vision_service = None

    gisai_engine = (
        GisAiEngine(
            model_manager=model_manager,
            vision_service=vision_service,
            coding_agent=coding_agent,
            project_context=active_project_context,
            project_store=None,
            task_manager=task_manager,
            inference=inference,
            openhands_adapter=openhands_adapter,
            planer_engine=None,
        )
        if GisAiEngine is not None
        else None
    )

    if gisai_engine is not None:
        _register_gisai_real_work_tools(
            gisai_engine, workspace, active_project_context, terminal_executor
        )
        _register_gisai_real_work_verifier(gisai_engine, terminal_executor)

    # Hosted writer: when enabled (globally or for this project) the GisAi
    # planner/writer surface swaps to the hosted provider. The key itself is
    # never read here - only at call time inside the provider.
    try:
        from giscode.inference.hosted import build_writer_provider
        project_root = getattr(active_project_context, "root_path", "") or ""
        writer_provider = build_writer_provider(settings, project_root=project_root)
        if writer_provider is not None and gisai_engine is not None:
            gisai_engine.set_writer_provider(writer_provider)
            logger.info(
                "Hosted writer enabled (model=%s, style=%s)",
                getattr(writer_provider, "model", "?"),
                getattr(writer_provider, "style", "?"),
            )
    except Exception as hosted_exc:
        logger.warning(f"Hosted writer setup failed; staying on local inference: {hosted_exc}")

    logger.info("Bootstrap complete")
    return settings, workspace, task_manager, inference, model_manager, coding_agent, terminal_executor, sandbox_executor, orchestrator, openhands_adapter, knowledge_service, routing_service, ocr_service, monitoring_service, packaging_builder, signing_service, self_improvement_service, research_service, gisai_engine


def _register_gisai_real_work_tools(gisai_engine, workspace, project_context, terminal_executor) -> None:
    """Wire the existing production file/command executors into GisAi's real-work
    tool registry, rooted at the active project (falls back to the app-level
    workspace until a project is opened).

    run_command/verification are re-rooted at the active project too: the app
    terminal keeps running at the app-level projects directory, but GisAi's
    command execution follows the open project so real work (and the real
    self-test) executes inside that project instead of escaping it.
    """
    project_executor = ProjectRootWorkspaceExecutor(workspace)
    project_terminal = ProjectRootCommandExecutor(terminal_executor)
    if project_context is not None:
        root = getattr(project_context, "root_path", "") or ""
        if root:
            project_executor.activate(root)
            project_terminal.activate(root)
    gisai_engine.set_workspace_action_executor(project_executor)
    gisai_engine.set_project_command_executor(project_terminal)
    gisai_engine.register_tool(ReadFileTool(project_executor))
    gisai_engine.register_tool(WriteFileTool(project_executor))
    gisai_engine.register_tool(EditFileTool(project_executor))
    gisai_engine.register_tool(ReplaceSymbolTool(project_executor))
    gisai_engine.register_tool(AddSymbolTool(project_executor))
    gisai_engine.register_tool(SetDocstringTool(project_executor))
    gisai_engine.register_tool(RunCommandTool(project_terminal))


def _register_gisai_real_work_verifier(gisai_engine, terminal_executor) -> None:
    """Wire the existing production test runner into GisAi RealWork so the
    generated tests actually execute against the active project during
    verification (no mock verifier, no fabricated success)."""
    terminal = getattr(gisai_engine, "_project_command_executor", None) or terminal_executor
    gisai_engine.set_terminal_executor(terminal)


def _model_is_vision(model: Any) -> bool:
    """Whether a model is vision-capable, by the project's existing rule.

    No model name is hardcoded: discovery already tags capabilities from name
    hints (``vl``, ``vision``, ...) and a sibling ``*.mmproj`` projector file
    (giscode/vision/capabilities.py).
    """
    from giscode.vision.capabilities import is_vision_capable

    return bool(is_vision_capable(model))


def _choose_fallback_model(registered, *, loadable, fits, is_vision, prefer_coding_of=None):
    """Choose the startup fallback without naming any model.

    Order: a non-vision model the resource plan fits, then a vision model it
    fits, then no model. A coding task on a vision-only model is what this
    avoids: the retired rule loaded a vision model first regardless of fit.

    When the model being replaced was coding-capable (``prefer_coding_of``), a
    coding-capable candidate is required first; a vision-only model is never
    offered as that replacement, and a non-coding text model is only used with
    ``why`` saying so.

    The intended first step - "the last model that loaded successfully" - is
    not available: the settings file has no key for it (``model`` holds only
    selected_model, context_size, gpu_layers, vram_budget_bytes,
    reasoning_budget and generation_defaults), so nothing is invented here.

    Returns ``(model_or_None, why)``; ``why`` is a short phrase for the one
    startup note sentence.
    """
    from giscode.models.feasibility import is_coding_capable
    from giscode.vision.capabilities import is_mmproj_path

    # A projector beside a VLM is not a model: loading it answers nothing.
    usable = [
        item
        for item in registered
        if loadable(item) and not is_mmproj_path(getattr(item, "path", ""))
    ]
    if not usable:
        return None, "no discovered GGUF is loadable"
    if prefer_coding_of is not None and is_coding_capable(prefer_coding_of):
        coding = [
            item
            for item in usable
            if is_coding_capable(item) and not is_vision(item) and fits(item)
        ]
        if coding:
            return coding[0], "a coding-capable model the resource plan fits"
        # A generic text model is not a coding substitute: with no coding-capable
        # model that fits, the honest outcome is that no model is loaded.
        return (
            None,
            "no coding-capable model fits the current resource plan "
            "(a generic text model is not substituted for coding)",
        )
    text_models = [item for item in usable if not is_vision(item) and fits(item)]
    if text_models:
        return text_models[0], "a non-vision model the resource plan fits"
    vision_models = [item for item in usable if is_vision(item) and fits(item)]
    if vision_models:
        return vision_models[0], "only a vision model fits the resource plan"
    return None, "no discovered model fits the current resource plan"


def _try_autoload_model(logger, settings, model_manager: "ModelManager") -> None:
    from giscode.config.paths import test_mode

    if test_mode():
        # An isolated test run must never load the developer's own model: no
        # discovery, no model selection, no server. Explicit loading through
        # the Device panel (or a test's own fake) is unaffected.
        logger.info("Model autoload skipped: GISCODE_TEST_MODE is set")
        return
    try:
        model_manager.auto_register_discovered()
    except Exception as e:
        logger.warning(f"Model auto-discovery failed: {e}")
    selected_name = settings.get("model.selected_model", "")
    if not selected_name:
        logger.info("No model selected in config; leaving models unloaded (load a model from the Device panel)")
        return
    registered = list(model_manager.get_registry().list_all())
    selected = model_manager.get_registry().get(selected_name)
    missing = selected is None or not model_manager.validate_model_path(
        getattr(selected, "path", "")
    )
    verdict = None
    if not missing:
        checker = getattr(model_manager, "feasibility", None)
        if callable(checker):
            try:
                verdict = checker(selected_name)
            except Exception as exc:
                logger.warning(f"Feasibility check for '{selected_name}' failed: {exc}")
    if missing or (verdict is not None and not verdict.feasible):
        if missing:
            absence = f"Saved model '{selected_name}' was not found"
        else:
            absence = (
                f"Saved model '{selected_name}' cannot be loaded "
                f"({verdict.label}: {verdict.reason})"
            )

        def _loadable(item):
            return model_manager.validate_model_path(getattr(item, "path", ""))

        def _fits(item):
            try:
                return bool(model_manager.check_budget(getattr(item, "path", "")))
            except Exception:
                return False

        preferred, why = _choose_fallback_model(
            registered,
            loadable=_loadable,
            fits=_fits,
            is_vision=_model_is_vision,
            prefer_coding_of=selected,
        )
        if preferred is None:
            logger.warning("%s; %s", absence, why)
            # Recorded for the window to show once, in the Model Manager note
            # line: which model (if any) was used and why.
            model_manager.startup_model_note = f"{absence}; no model loaded ({why})"
            return
        if preferred.name == selected_name:
            # Nothing replaced the saved model, so there is nothing to report:
            # the loader (and its own fallback) decide what actually happens.
            logger.info(
                "Saved model '%s' is the only candidate; leaving the load to the loader",
                selected_name,
            )
            selected_name = preferred.name
        else:
            logger.warning("%s; falling back to '%s' (%s)", absence, preferred.name, why)
            model_manager.startup_model_note = (
                f"{absence}; using '{preferred.name}' ({why})"
            )
            selected_name = preferred.name
            settings.set("model.selected_model", selected_name)
    try:
        metadata = model_manager.load_model(selected_name)
        logger.info(f"Auto-loaded model '{metadata.name}' from {metadata.path}")
    except Exception as e:
        logger.warning(f"Auto-load of model '{selected_name}' failed: {e}")


def _run_pending_recovery_cli() -> None:
    """Recover an interrupted Core transaction from the packaged application.

    This deliberately runs before bootstrap and the GUI: recovery must work
    without a model, network access, GisAI, or any project window. ``--yes``
    is the explicit approval for changing files; without it the transaction is
    reported as still requiring recovery and nothing is written.
    """
    import argparse

    from giscode.agentic.recover_edits import recover_pending_transactions

    parser = argparse.ArgumentParser(
        prog="GisCode --recover-pending",
        description="Roll back a crash-interrupted Core coding transaction.",
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--profile-root", default="")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", default="")
    args, _unknown = parser.parse_known_args()

    approve = (lambda _task, _files: True) if args.yes else None
    results = recover_pending_transactions(
        args.project,
        profile_root=args.profile_root or None,
        approve=approve,
        dry_run=args.dry_run,
    )
    payload = {
        "format": "giscode.pending_recovery.v1",
        "project": os.path.abspath(os.path.expanduser(args.project)),
        "profile_root": args.profile_root,
        "approved": bool(args.yes),
        "dry_run": bool(args.dry_run),
        "results": [item.as_dict() for item in results],
    }
    if args.json:
        target = Path(args.json).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    for item in results:
        try:
            print(f"{item.task}: {item.status}")
            if item.blocked:
                print(f"  {item.blocked}")
        except Exception:
            pass
    ok = all(item.status in {"recovered", "would_recover"} for item in results)
    sys.exit(0 if ok else 1)

def _run_coding_task_cli() -> None:
    """Run one real coding task from the packaged Core executable, without a GUI.

    This is the native packaged path for qualification and headless automation:
    it goes through the same bootstrap, profile router, CodingAgentLoop, guarded
    writer, verifier and recovery records as the application. It deliberately
    does not use GisAI or any Pro service.
    """
    import argparse
    from pathlib import Path as _Path

    from giscode.agentic.types import TaskRequest

    parser = argparse.ArgumentParser(prog="GisCode --run-coding-task")
    parser.add_argument("--project", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--max-duration", type=float, default=300.0)
    parser.add_argument("--json", default="")
    args, _unknown = parser.parse_known_args()

    (
        _settings,
        _workspace,
        _task_manager,
        _inference,
        _model_manager,
        coding_agent,
        _terminal_executor,
        _sandbox_executor,
        _orchestrator,
        _openhands_adapter,
        _knowledge_service,
        _routing_service,
        _ocr_service,
        _monitoring_service,
        _packaging_builder,
        _signing_service,
        _self_improvement_service,
        _research_service,
        _gisai_engine,
    ) = bootstrap()

    root = _Path(args.project).expanduser().resolve()
    task_id = args.task_id or f"packaged-task-{int(time.time())}"
    request = TaskRequest(
        task_id=task_id,
        description=args.request,
        max_duration=args.max_duration,
        completion_criteria={
            "project_root": str(root),
            "project_id": "packaged-recovery",
        },
    )
    setter = getattr(coding_agent, "set_edit_approval_callback", None)
    if callable(setter):
        setter(lambda _path, _diff: True)
    result = coding_agent.start(request, root_path=str(root))
    actions = []
    for action in getattr(result, "actions", []) or []:
        actions.append(
            {
                "type": str(getattr(getattr(action, "action_type", None), "value", getattr(action, "action_type", ""))),
                "path": str(getattr(action, "target_path", "")),
                "success": bool(getattr(action, "success", False)),
                "error": getattr(action, "error", None),
            }
        )
    verification = getattr(result, "verification", None)
    payload = {
        "format": "giscode.coding_task.v1",
        "task_id": task_id,
        "project": str(root),
        "status": str(getattr(getattr(result, "status", None), "value", getattr(result, "status", ""))),
        "error": getattr(result, "error", None),
        "response": getattr(result, "response", None),
        "changed_files": list(coding_agent.changed_files_for(task_id) or []),
        "actions": actions,
        "verification": {
            "success": bool(getattr(verification, "success", False)),
            "errors": list(getattr(verification, "errors", []) or []),
            "details": list(getattr(verification, "details", []) or []),
        },
    }
    if args.json:
        target = _Path(args.json).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        print(json.dumps({"task_id": task_id, "status": payload["status"], "changed_files": payload["changed_files"]}))
    except Exception:
        pass
    sys.exit(0 if payload["status"] == "complete" else 1)


def run() -> None:
    # Refuse an unsafe or malformed acceptance run before bootstrap() creates
    # the profile, opens a database or writes a log line.
    refusal = _acceptance_refusal_reason()
    if refusal is not None:
        print(f"SCENARIO_REFUSED: {refusal}", flush=True)
        sys.stdout.flush()
        os._exit(2)
    if "--recover-pending" in sys.argv:
        _run_pending_recovery_cli()
        return
    if "--run-coding-task" in sys.argv:
        _run_coding_task_cli()
        return
    settings, workspace, task_manager, inference, model_manager, coding_agent, terminal_executor, sandbox_executor, orchestrator, openhands_adapter, knowledge_service, routing_service, ocr_service, monitoring_service, packaging_builder, signing_service, self_improvement_service, research_service, gisai_engine = bootstrap()
    logger = get_logger("giscode.main")

    if "--smoke" in sys.argv:
        _run_smoke(logger, model_manager, coding_agent)
        return
    if "--splash-check" in sys.argv:
        import sys as _sys
        from giscode.gui.splash import run_splash_check
        code = run_splash_check()
        _sys.exit(code)
    if "--gui-check" in sys.argv:
        _run_gui_check(logger, terminal_executor)
        return
    if "--terminal-check" in sys.argv:
        _run_terminal_check(logger, terminal_executor)
        return

    try:
        from giscode.inference.runtime import report_foreign_servers
        for foreign in report_foreign_servers():
            logger.warning(
                "llama-server pid=%d is not owned by this application (started before launch); leaving it running",
                foreign["pid"],
            )
    except Exception as report_exc:
        logger.warning(f"Foreign llama-server probe failed: {report_exc}")

    try:
        monitoring_service.startup(namespace="default")
    except Exception as e:
        logger.warning(f"Monitoring startup event failed: {e}")

    try:
        from giscode.gui import launch_gui
        try:
            # iOS / xtool is a Pro capability; the public Core export does not
            # ship it, so its absence must not stop the GUI from launching.
            from giscode.iosxtool import create_iosxtool_service
        except ImportError:
            create_iosxtool_service = None
        iosxtool_service = create_iosxtool_service() if create_iosxtool_service else None
        # The Agent Registry and Capability Gateway are a Pro capability. A Core
        # launch never imports the package; a Pro launch builds the service that
        # owns the edition gate for every capability call.
        agent_registry_service = None
        try:
            from giscode import edition as _edition

            if _edition.is_pro() and _edition.implementation_available("agent_registry"):
                from giscode.agent_registry import HostAuthority
                from giscode.agent_registry.audit import JsonlAuditSink
                from giscode.agent_registry.service import ProCapabilityService
                from giscode.config.paths import giscode_home

                # Strict verification success is only acceptable with durable
                # audit evidence, so the packaged service owns a real fsynced
                # sink.  A sink that cannot be created makes the service
                # unavailable rather than silently un-audited.
                audit_sink = JsonlAuditSink(
                    giscode_home() / "audit" / "agent_registry.jsonl"
                )
                # The host bootstrap owns the authority.  It starts empty, so
                # every capability stays denied until the host enrols a project.
                agent_registry_service = ProCapabilityService(
                    audit=audit_sink, authority=HostAuthority()
                )
        except Exception as registry_exc:
            logger.warning(f"Agent Registry service unavailable: {registry_exc}")
            agent_registry_service = None
        else:
            # Observable initialisation: the packaged qualification reads this
            # line to tell a wired registry from a service that failed to build.
            if agent_registry_service is not None:
                logger.info("Agent Registry service ready")
        # The managed runtime is Pro-owned too, and is built only in a Pro
        # launch. Construction starts no process and loads no model; a runtime
        # is created only when the Agent Registry pane asks for one.
        agent_runtime_manager = None
        try:
            from giscode import edition as _runtime_edition

            if (
                _runtime_edition.is_pro()
                and _runtime_edition.implementation_available("agent_runtime")
                and agent_registry_service is not None
            ):
                from giscode.agent_runtime.bootstrap import (
                    build_gismac_runtime_factory,
                )
                from giscode.agent_runtime.manager import AgentRuntimeManager

                # The factory builds the strict policy, receipt key (created and
                # ACL-hardened), consumption store and audit sink.  A missing
                # dependency raises and leaves the manager unavailable rather
                # than creating a runtime that cannot verify or audit.
                agent_runtime_manager = AgentRuntimeManager(
                    agent_registry_service,
                    adapter_factory=build_gismac_runtime_factory(
                        agent_registry_service
                    ),
                )
                logger.info("Agent runtime manager ready")
        except Exception as runtime_exc:
            logger.warning(f"Agent runtime manager unavailable: {runtime_exc}")
            agent_runtime_manager = None
        root = launch_gui(task_manager=task_manager, model_manager=model_manager, coding_agent=coding_agent, terminal_executor=terminal_executor, sandbox_executor=sandbox_executor, orchestrator=orchestrator, openhands_adapter=openhands_adapter, knowledge_service=knowledge_service, routing_service=routing_service, ocr_service=ocr_service, monitoring_service=monitoring_service, packaging_builder=packaging_builder, signing_service=signing_service, self_improvement_service=self_improvement_service, research_service=research_service, gisai_engine=gisai_engine, iosxtool_service=iosxtool_service, agent_registry_service=agent_registry_service, agent_runtime_manager=agent_runtime_manager)
        logger.info("Launching GUI")
        # Defer model/server autoload to a background thread so the GUI can
        # appear immediately while llama-server loads the selected model.
        autoload_thread = None
        try:
            from giscode.inference.hosted import hosted_enabled
            hosted_on = hosted_enabled(settings)
        except Exception:
            hosted_on = False
        from giscode.config.paths import test_mode

        if hosted_on:
            logger.info("Hosted writer enabled: skipping local model autoload")
        elif test_mode():
            logger.info("Model autoload skipped: GISCODE_TEST_MODE is set")
        elif not (
            _acceptance_automation_enabled()
            and os.environ.get("GISCODE_ACCEPTANCE_MODEL", "").strip()
        ):
            autoload_thread = threading.Thread(
                target=_try_autoload_model,
                args=(logger, settings, model_manager),
                daemon=True,
                name="giscode-autoload",
            )
            autoload_thread.start()
        _schedule_gui_acceptance(root, model_manager)
        try:
            root.mainloop()
        finally:
            # Do not let a deferred load finish after runtime cleanup; that
            # race can leave a spawned llama-server behind the GUI process.
            if autoload_thread is not None:
                autoload_thread.join()
            # Stop every managed agent runtime the session created; the
            # manager releases reservations and verifies process cleanup.
            if agent_runtime_manager is not None:
                try:
                    agent_runtime_manager.dispose_all()
                except Exception as runtime_exc:
                    logger.warning(f"Agent runtime cleanup failed: {runtime_exc}")
            # Release the inference runtime after the GUI exits so a spawned
            # llama-server cannot outlive the application process.
            try:
                current = model_manager._inference.get_model_status()
                if current is not None:
                    try:
                        model_manager.unload_model(getattr(current, "name", ""))
                    except Exception:
                        # Runtime names are file stems; registry names may be
                        # aliases. Use the inference API as the cleanup authority.
                        model_manager._inference.unload_model()
            except Exception as cleanup_error:
                logger.warning(f"Model shutdown failed: {cleanup_error}")
    except Exception as e:
        logger.error(f"Failed to launch GUI: {e}")
        try:
            monitoring_service.runtime_failure(str(e), namespace="default")
        except Exception:
            pass
        sys.exit(1)

    try:
        monitoring_service.shutdown(namespace="default")
    except Exception as e:
        logger.warning(f"Monitoring shutdown event failed: {e}")


def _run_smoke(logger, model_manager: "ModelManager", coding_agent: "CodingAgentLoop") -> None:
    import time
    from giscode.inference.runtime import ChatMessage, ChatRequest

    for stream_name in ("stdout", "stderr"):
        try:
            getattr(sys, stream_name).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    model_manager.auto_register_discovered()
    candidates = [m for m in model_manager.list_registered() if "Qwen3-VL-4B" in m.name and "mmproj" not in m.name.lower()]
    if not candidates:
        print("SMOKE_FAIL: no Qwen3-VL-4B chat GGUF discovered (mmproj skipped)")
        sys.exit(2)
    target_name = candidates[0].name
    current = model_manager._inference.get_model_status()
    if current is not None and getattr(current, "name", "") == target_name:
        meta = model_manager.get_registry().get(target_name)
    else:
        if current is not None:
            try:
                model_manager.unload_model(getattr(current, "name", "") or model_manager.list_registered()[0].name)
            except Exception:
                try:
                    model_manager._inference.unload_model()
                except Exception:
                    pass
        try:
            meta = model_manager.load_model(target_name)
        except Exception as e:
            print(f"SMOKE_FAIL: load_model error: {e}")
            sys.exit(3)

    try:
        health = model_manager._inference.get_health() if hasattr(model_manager, "_inference") else None
        print(f"SMOKE_LOADED: name={meta.name} status={meta.status.value} health={health}")

        request = ChatRequest(
            messages=[ChatMessage(role="user", content="Reply with exactly: MODEL_OK")],
            max_tokens=16,
            temperature=0.0,
        )
        t0 = time.time()
        # The smoke command validates the loaded production inference path
        # directly. CodingAgentLoop additionally requires routing-service
        # model selection, which is not part of this minimal runtime probe.
        response = model_manager._inference.chat_generate(request)
        elapsed = time.time() - t0
        response_text = response.text or ""
        print("SMOKE_STATUS: completed")
        print("SMOKE_ERROR: None")
        print("SMOKE_INFERENCE_ERROR: None")
        print(f"SMOKE_RESPONSE_LEN: {len(response_text)}")
        print(f"SMOKE_RESPONSE_TEXT:")
        print(response_text)
        print(f"SMOKE_ELAPSED: {elapsed:.2f}s")
        if not response_text.strip():
            print("SMOKE_FAIL: empty response")
            sys.exit(4)
        print("SMOKE_OK")
        sys.exit(0)
    finally:
        try:
            current = model_manager._inference.get_model_status()
            if current is not None:
                model_manager.unload_model(target_name)
        except Exception as cleanup_error:
            print(f"SMOKE_CLEANUP_WARNING: {cleanup_error}")


# ------------------------------------------------------------------ acceptance
#: Set only by the purpose-built acceptance executable: the PyInstaller runtime
#: hook that ``GisCode.spec`` installs when ``GISCODE_ACCEPTANCE_ENTRY=image``.
#: An ordinary Core build never has it, so inherited environment variables
#: cannot switch the automation on.
ACCEPTANCE_BUILD_MARKER = "_giscode_acceptance_build"

#: Deliberate opt-in. The four data variables stay inert without this value.
ACCEPTANCE_OPT_IN_ENV = "GISCODE_ACCEPTANCE_OPT_IN"
ACCEPTANCE_OPT_IN_VALUE = "1"

#: Selects an interactive scenario executed against the frozen GUI.
ACCEPTANCE_SCENARIO_ENV = "GISCODE_ACCEPTANCE_SCENARIO"

#: Evidence files are written only below this directory in the temporary root.
ACCEPTANCE_EVIDENCE_ROOT = "giscode_acceptance"


def _acceptance_automation_enabled() -> bool:
    """True only in an acceptance build that was explicitly opted in.

    The build marker is set by a runtime hook baked into the acceptance
    executable and cannot be set from the environment, so a production build
    ignores inherited ``GISCODE_ACCEPTANCE_*`` variables entirely.
    """

    if not getattr(sys, ACCEPTANCE_BUILD_MARKER, False):
        return False
    return os.environ.get(ACCEPTANCE_OPT_IN_ENV, "").strip() == ACCEPTANCE_OPT_IN_VALUE


def _acceptance_refusal_reason() -> str | None:
    """Why an armed acceptance run must refuse before it starts, else ``None``.

    Scenario validation used to happen only once the GUI existed, by which point
    ``bootstrap()`` had created the profile, opened the knowledge store and
    written its first log line - so a run that was about to be refused had
    already modified the profile it refused to touch. Checking here, before any
    of that, makes the refusal genuinely read-only.
    """

    if not _acceptance_automation_enabled():
        return None
    scenario = os.environ.get(ACCEPTANCE_SCENARIO_ENV, "").strip()
    if not scenario:
        # The task-driving acceptance path validates its own variables.
        return None

    from giscode.acceptance_runtime import preflight
    from giscode.acceptance_safety import DisposableHomeError

    try:
        preflight(scenario, _acceptance_evidence_name)
    except DisposableHomeError as exc:
        return exc.reason
    return None


def _acceptance_evidence_name(value: str) -> str | None:
    """Accept only a bare file name; refuse paths, drives, separators, ``..``."""

    name = (value or "").strip()
    if not name or name in {".", ".."}:
        return None
    candidate = Path(name)
    if candidate.is_absolute() or candidate.drive or candidate.name != name:
        return None
    if ".." in name or name.endswith((".", " ")):
        return None
    return name


def _acceptance_is_reparse(info: os.stat_result) -> bool:
    """True for a symlink, junction or any other reparse point."""

    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _acceptance_root_is_safe(root: Path) -> bool:
    """True when the evidence root is a real directory, not a reparse point.

    Junctions are reparse points and ``os.path.islink`` does not report them, so
    the file attributes *and* the real path are checked: a root redirecting
    elsewhere would otherwise carry a write outside the dedicated area.
    """

    try:
        info = os.lstat(root)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return False
    try:
        real = os.path.normcase(os.path.realpath(root))
        plain = os.path.normcase(os.path.abspath(root))
    except OSError:
        return False
    if real != plain:
        return False
    return stat.S_ISDIR(info.st_mode)


def _acceptance_evidence_root() -> Path | None:
    """The dedicated temporary root, or ``None`` when it is not trustworthy."""

    root = Path(tempfile.gettempdir()) / ACCEPTANCE_EVIDENCE_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root if _acceptance_root_is_safe(root) else None


def _acceptance_evidence_path(value: str) -> Path | None:
    """Validated candidate path for ``value``; writes must go through the writer.

    This is the cheap check used before a task is scheduled. The write itself
    re-validates and inspects the opened handle, so passing here is not taken as
    permission to write at that name.
    """

    name = _acceptance_evidence_name(value)
    if name is None:
        return None
    root = _acceptance_evidence_root()
    if root is None:
        return None
    target = root / name
    try:
        if _acceptance_destination_blocks_publish(target):
            return None
    except OSError:
        return None
    return target


def _acceptance_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_ino, info.st_dev)


def _acceptance_discard_temporary(path: Path, root: Path, expected: os.stat_result) -> None:
    """Remove a temporary file only while the directory identity is unchanged."""

    try:
        current = os.lstat(root)
    except OSError:
        return
    if _acceptance_identity(current) != _acceptance_identity(expected):
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _acceptance_destination_blocks_publish(path: Path) -> bool:
    """True when the destination must not be published over.

    A real directory is a destination we may not write into, and a junction or
    symlink is a destination we refuse rather than follow or replace: the safe
    answer is to abandon the write, not to guess what the link was for.
    """

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISDIR(info.st_mode) or _acceptance_is_reparse(info)


def _acceptance_write_evidence(value: str, text: str) -> Path | None:
    """Publish evidence atomically inside the verified dedicated directory.

    The payload goes to a uniquely created temporary file in that directory,
    is flushed, and then published with ``os.replace``. Replacing a directory
    entry does not follow a destination symlink and does not write into a
    pre-existing hard-linked file, so an external alias keeps its original bytes
    even when the destination is swapped mid-operation. The directory identity
    is re-checked immediately before publishing; a changed directory, a reparse
    point or a directory at the destination aborts the publish.

    Residual: the identity check and ``os.replace`` are two calls, so a
    directory swapped in that interval is not detected. Unlinking of the
    temporary file is likewise identity-guarded, so cleanup cannot follow a
    swapped-in directory either.
    """

    name = _acceptance_evidence_name(value)
    if name is None:
        return None
    root = _acceptance_evidence_root()
    if root is None:
        return None
    try:
        before = os.lstat(root)
    except OSError:
        return None
    if not stat.S_ISDIR(before.st_mode) or _acceptance_is_reparse(before):
        return None
    destination = root / name
    try:
        if _acceptance_destination_blocks_publish(destination):
            return None
    except OSError:
        return None

    temporary = root / f".evidence-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except OSError:
        return None
    try:
        try:
            opened = os.fstat(descriptor)
            if opened.st_nlink != 1:
                return None
            os.write(descriptor, text.encode("utf-8"))
            os.fsync(descriptor)
            if os.fstat(descriptor).st_nlink != 1:
                return None
        finally:
            os.close(descriptor)

        try:
            current = os.lstat(root)
        except OSError:
            return None
        if _acceptance_identity(current) != _acceptance_identity(before):
            return None
        if _acceptance_is_reparse(current):
            return None
        try:
            if _acceptance_destination_blocks_publish(destination):
                return None
            os.replace(temporary, destination)
        except OSError:
            return None
        return destination
    finally:
        _acceptance_discard_temporary(temporary, root, before)


def _schedule_gui_acceptance(root, model_manager) -> None:
    """Drive the normal GUI callbacks for an isolated packaged acceptance run.

    Reachable only from the purpose-built acceptance executable and only with an
    explicit ``GISCODE_ACCEPTANCE_OPT_IN=1``: an ordinary launch returns here
    before reading any data variable. It does not call the engine directly -
    project selection, model selection, and task submission still use their
    ordinary GUI callbacks - and the evidence file is confined to the dedicated
    temporary directory.
    """
    if not _acceptance_automation_enabled():
        return

    # An interactive scenario drives the AI Web Chat, settings and project
    # surfaces in-process; the task-driving path below stays the default so a
    # packaged qualification of the coding loop is unchanged.
    scenario = os.environ.get(ACCEPTANCE_SCENARIO_ENV, "").strip()
    if scenario:
        from giscode.acceptance_runtime import schedule_scenario

        schedule_scenario(
            root,
            scenario,
            evidence_path=_acceptance_evidence_path,
            write_evidence=_acceptance_write_evidence,
        )
        return

    project_path = os.environ.get("GISCODE_ACCEPTANCE_PROJECT", "").strip()
    model_name = os.environ.get("GISCODE_ACCEPTANCE_MODEL", "").strip()
    request = os.environ.get("GISCODE_ACCEPTANCE_REQUEST", "")
    evidence_value = os.environ.get("GISCODE_ACCEPTANCE_EVIDENCE", "")
    if not project_path or not model_name or not request:
        return
    if _acceptance_evidence_path(evidence_value) is None:
        return

    window = getattr(root, "_giscode_main_window", None)
    if window is None:
        return

    state = {"submitted": False, "deadline": time.time() + 240.0}

    def record(status: str, detail: str = "") -> None:
        try:
            if not detail:
                # A bare FAILED hid the reason in 001W: the GUI said why in its
                # response pane, not in the evidence file. Capture that text so
                # an acceptance failure is auditable from the artifact alone.
                try:
                    detail = str(window.response_text.get("1.0", "end-1c"))[-600:].strip()
                except Exception:
                    detail = ""
            work_result = getattr(window, "_last_work_result", None)
            if work_result is None:
                work_result = getattr(getattr(window, "_gisai_engine", None), "_last_work_result", None)
            work_data = None
            if work_result is not None:
                if isinstance(work_result, dict):
                    # The Core dispatcher records a plain dictionary; an engine
                    # records a dataclass. asdict() on a dict raises, and the
                    # surrounding except then dropped the whole evidence file.
                    work_data = work_result
                else:
                    from dataclasses import asdict
                    work_data = asdict(work_result)
            _acceptance_write_evidence(
                evidence_value,
                json.dumps({
                    "status": status,
                    "detail": detail,
                    "report": getattr(window, "_last_report", ""),
                    "model_health": model_manager._inference.get_health(),
                    "work_result": work_data,
                }, ensure_ascii=False, indent=2),
            )
        except Exception:
            pass

    def poll_model() -> None:
        if time.time() > state["deadline"]:
            record("TIMEOUT", "model did not become ready")
            root.after(1000, root.destroy)
            return
        try:
            current = model_manager.get_current_model()
            if current is not None and getattr(current, "state", None).value == "ready":
                if not state["submitted"]:
                    state["submitted"] = True
                    window.task_entry.delete("1.0", "end")
                    window.task_entry.insert("1.0", request)
                    window._send_task()
                root.after(250, poll_result)
                return
        except Exception as exc:
            record("FAILED", str(exc))
            root.after(1000, root.destroy)
            return
        root.after(500, poll_model)

    def poll_result() -> None:
        if time.time() > state["deadline"]:
            record("TIMEOUT", "GUI task did not finish")
            root.after(1000, root.destroy)
            return
        if not window._active_task:
            report = getattr(window, "_last_report", "")
            record("COMPLETED" if "completed" in report.lower() or "verification: passed" in report.lower() else "FAILED")
            root.after(1000, root.destroy)
            return
        root.after(250, poll_result)

    window._select_project_path(project_path)
    window._model_var.set(model_name)
    window._on_model_selected()
    root.after(500, poll_model)


def _run_gui_check(logger, terminal_executor) -> None:
    import tkinter as tk
    from giscode import edition
    from giscode.gui.main_window import APP_TITLE, MainWindow

    for stream_name in ("stdout", "stderr"):
        try:
            getattr(sys, stream_name).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    root = tk.Tk()
    root.withdraw()
    try:
        window = MainWindow(root, terminal_executor=terminal_executor)
    except Exception as e:
        print(f"GUI_CHECK_FAIL: {type(e).__name__}: {e}")
        sys.exit(10)

    expected_tabs = edition.tabs()
    actual_tabs = tuple(window._tab_frames.keys())
    print(f"GUI_CHECK_EDITION: {edition.edition()}")
    print(f"GUI_CHECK_TITLE: {root.title()!r}")
    print(f"GUI_CHECK_TABS: {len(actual_tabs)} {list(actual_tabs)}")
    # Both support entry points are structural: a Help menu command and the About
    # dialog. The URL itself is a setting, so the probe checks the entry points,
    # not whether a page happens to be configured on this machine.
    help_labels = window.help_menu_labels()
    print(f"GUI_CHECK_SUPPORT: {help_labels}")
    if "Support the Developer" not in help_labels:
        print("GUI_CHECK_FAIL: the Help menu has no Support the Developer entry")
        sys.exit(11)
    # Core does not build the Pro panes, so the probe counts only the panes this
    # edition is supposed to build; a missing shared pane still fails the check.
    wanted: list[tuple[str, str]] = [("response_text", "chat")]
    for widget, feature in (
        ("terminal_text", "terminal"),
        ("device_canvas", "device_preview"),
    ):
        if edition.feature_enabled(feature):
            wanted.append((widget, feature))
    present = sum(1 for name, _ in wanted if getattr(window, name, None) is not None)
    print(f"GUI_CHECK_TEXT_WIDGETS: {present}/{len(wanted)}")
    has_send = getattr(window, "send_task_btn", None) is not None
    has_stop = getattr(window, "revoke_btn", None) is not None
    has_load = getattr(window, "load_model_btn", None) is not None
    has_unload = getattr(window, "unload_model_btn", None) is not None
    print(f"GUI_CHECK_CONTROLS: send={has_send} stop={has_stop} load={has_load} unload={has_unload}")
    logo_assets = False
    logo_png = False
    try:
        from giscode.branding.logo import DEFAULT_LOGO_SMALL, DEFAULT_LOGO_SPLASH, gis_logo_path
        logo_assets = all(gis_logo_path(n).is_file() for n in (DEFAULT_LOGO_SMALL, DEFAULT_LOGO_SPLASH))
    except Exception:
        logo_assets = False
    logo_png = getattr(window, "_gis_logo_photo", None) is not None
    # The custom-logo control is a Pro surface; Core is not missing it by accident.
    logo_btn = getattr(window, "custom_logo_btn", None) is not None
    if not edition.feature_enabled("custom_logo"):
        logo_btn = True
    print(f"GUI_CHECK_LOGO: assets={logo_assets} topbar_png={logo_png} custom_logo_btn={logo_btn}")
    ok = (
        root.title() == APP_TITLE
        and actual_tabs == expected_tabs
        and present == len(wanted)
        and has_send and has_stop and has_load and has_unload
        and logo_assets and logo_png and logo_btn
    )
    if not ok:
        print("GUI_CHECK_FAIL")
        sys.exit(11)
    print("GUI_CHECK_OK")
    sys.exit(0)


def _run_terminal_check(logger, terminal_executor) -> None:
    for stream_name in ("stdout", "stderr"):
        try:
            getattr(sys, stream_name).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if terminal_executor is None:
        print("TERMINAL_CHECK_FAIL: no terminal executor wired")
        sys.exit(20)
    try:
        from giscode.terminal.types import CommandRequest
        req = CommandRequest(command="cmd.exe", arguments=["/c", "echo GisCode terminal test"])
        result = terminal_executor.execute(req)
    except Exception as e:
        print(f"TERMINAL_CHECK_FAIL: {type(e).__name__}: {e}")
        sys.exit(21)
    print(f"TERMINAL_CHECK_EXIT: {result.exit_code}")
    print(f"TERMINAL_CHECK_ELAPSED: {result.elapsed_time:.2f}s")
    print(f"TERMINAL_CHECK_STDOUT: {result.stdout!r}")
    print(f"TERMINAL_CHECK_STDERR: {result.stderr!r}")
    if result.exit_code != 0 or "GisCode terminal test" not in (result.stdout or ""):
        print("TERMINAL_CHECK_FAIL")
        sys.exit(22)
    print("TERMINAL_CHECK_OK")
    sys.exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()
