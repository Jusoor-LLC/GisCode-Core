try:  # Tk is optional for headless collection; GUI tests skip explicitly.
    import tkinter as tk
except ImportError:  # pragma: no cover - depends on the Python installation
    class _UnavailableTk:
        class TclError(Exception):
            pass

        _default_root = None

        @staticmethod
        def Tk():
            raise _UnavailableTk.TclError("tkinter is not installed")

    tk = _UnavailableTk()
import pytest
import threading
import time
import subprocess
import os
from typing import Any, Optional



# ---------------------------------------------------------------------------
# the optional GisAI package
# ---------------------------------------------------------------------------
# release/manifests/exclude.toml keeps giscode.gisai out of the public Core
# tree, so tests that need the package itself - a compatibility shim, or the
# engine's task state machine - cannot run there. Comparing every assertion in
# such a test would only move the failure, so they carry @requires_gisai: the
# limitation then shows up as a skip with this reason instead of a failure, and
# they still run unchanged in this repository, where the package exists.

GISAI_ABSENT_REASON = (
    "needs the GisAI package, which the Core release profile does not ship; "
    "the test runs in the full tree"
)


def gisai_available() -> bool:
    """True when ``giscode.gisai`` is importable in this tree."""
    import importlib.util

    try:
        return importlib.util.find_spec("giscode.gisai") is not None
    except (ImportError, ValueError):  # a broken install is not "available"
        return False


requires_gisai = pytest.mark.skipif(not gisai_available(), reason=GISAI_ABSENT_REASON)


# ---------------------------------------------------------------------------
# llama-server lifecycle safety
# ---------------------------------------------------------------------------
#
# This suite used to run ``taskkill /F /IM llama-server.exe`` before and after
# every single test. Two things were wrong with that:
#
# * it killed by image name, so the *first* test of a run force-killed the
#   developer's own model server (LM Studio's included) and lost their loaded
#   model - the suite was not safe to run on a machine that was doing anything;
# * ``GISCODE_KEEP_LLAMA_SERVER=1`` disabled it wholesale, which also stopped
#   the cleanup of servers the suite's *own* tests had spawned, so those
#   survived as orphans with dead parent PIDs (six were still running when
#   this was written).
#
# The rule now is: the session stops every llama-server process it started
# (by PID, re-checking the image name), and never one it did not start.
#
# ``GISCODE_KEEP_LLAMA_SERVER=1`` means "touch no server that this session did
# not start". It never authorises touching another program's server; the
# session's own servers are still stopped, including under that variable and
# after a failing test.
#
# ``GISCODE_TEST_KILL_ALL_SERVERS=1`` restores the old machine-wide
# ``taskkill /F /IM llama-server.exe`` for dedicated CI machines, where no
# other server is ever running. It is opt-in only, and
# ``GISCODE_KEEP_LLAMA_SERVER=1`` wins over it.

KEEP_LLAMA_SERVER_ENV = "GISCODE_KEEP_LLAMA_SERVER"
KILL_ALL_SERVERS_ENV = "GISCODE_TEST_KILL_ALL_SERVERS"
LLAMA_SERVER_IMAGE = "llama-server"
TEST_MODE_ENV = "GISCODE_TEST_MODE"

# Set at import time, not only in a fixture: model discovery can run while
# modules are being collected, and the point of the switch is that the suite
# never sees (or autoloads) the developer's personal LM Studio weights. The
# session fixture below re-arms it for tests that remove it on purpose.
os.environ.setdefault(TEST_MODE_ENV, "1")


def _foreign_kill_enabled() -> bool:
    """True only when a machine-wide kill was explicitly asked for."""
    if os.environ.get(KEEP_LLAMA_SERVER_ENV) == "1":
        return False
    return os.environ.get(KILL_ALL_SERVERS_ENV) == "1"


def _session_server_pids() -> list[int]:
    """PIDs of llama-server processes this pytest session started.

    Two independent sources, both scoped to this process:

    * ``_OWNED_SERVER_PIDS`` in ``giscode.inference.runtime``, which the
      production loader populates whenever it spawns a server;
    * a psutil walk of this process's own child tree filtered to
      ``llama-server``, which also catches a server a test spawned by hand.

    Another program's server is in neither set: its PID was never registered
    here, and it is not a descendant of this process. A server whose parent
    has already exited is not a descendant either, which is exactly why a
    pre-existing orphan is invisible to this scan.
    """
    pids: set[int] = set()
    try:
        from giscode.inference.runtime import _OWNED_SERVER_PIDS

        pids.update(int(pid) for pid in _OWNED_SERVER_PIDS)
    except Exception:
        pass
    try:
        import psutil
    except Exception:
        return sorted(pids)
    try:
        for child in psutil.Process().children(recursive=True):
            try:
                if LLAMA_SERVER_IMAGE in (child.name() or "").lower():
                    pids.add(int(child.pid))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return sorted(pids)


def _kill_pid_tree(pid: int) -> bool:
    """Kill one PID and its children. Only ever called for a PID we started."""
    try:
        from giscode.inference.runtime import _kill_process_tree
    except Exception:
        _kill_process_tree = None
    if _kill_process_tree is not None:
        return bool(_kill_process_tree(pid))
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _stop_session_llama_servers() -> list[int]:
    """Stop every llama-server this session started; return the PIDs stopped.

    The image name is re-checked immediately before the kill, so a recycled PID
    can never take out an unrelated process, and the whole tree of the PID is
    killed so a spawned server's helpers go with it.
    """
    stopped: list[int] = []
    pids = _session_server_pids()
    if not pids:
        return stopped
    try:
        import psutil
    except Exception:
        psutil = None
    for pid in pids:
        if psutil is not None:
            try:
                name = (psutil.Process(pid).name() or "").lower()
            except Exception:
                continue
            if LLAMA_SERVER_IMAGE not in name:
                # Recycled PID, or gone: never kill what we do not recognise.
                continue
        if _kill_pid_tree(pid):
            stopped.append(pid)
            try:
                from giscode.inference.runtime import _OWNED_SERVER_PIDS

                _OWNED_SERVER_PIDS.discard(pid)
            except Exception:
                pass
    return stopped


def _cleanup_llama_servers() -> list[int]:
    """The session's own llama-servers, plus a machine-wide kill only if opted in."""
    stopped = _stop_session_llama_servers()
    if _foreign_kill_enabled():
        try:
            subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    return stopped


def _kill_llama_servers():
    """Kill any orphan llama-server processes.

    Historic name, kept because ``tests/test_resource_budget.py`` imports it and
    because ``tools/rerun_harness.py`` injects an early ``return`` directly under
    this ``def`` line to protect the model server its runs depend on. Its
    behaviour is the safe default above: this session's own servers only, plus a
    machine-wide kill when ``GISCODE_TEST_KILL_ALL_SERVERS=1`` asked for one.
    The fixtures call ``_cleanup_llama_servers`` directly, so that injected
    ``return`` cannot silently disable the per-session cleanup.
    """
    return _cleanup_llama_servers()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_llama_servers_session():
    """Stop the servers this session started, before and after the session."""
    _cleanup_llama_servers()
    yield
    _cleanup_llama_servers()


@pytest.fixture
def auto_approve_edits(monkeypatch):
    """Enable automatic edit approval for a single test.

    Real-work tests wire the production write guard, which requires explicit
    approval whenever a ``task_id`` is present. This fixture lets those tests
    exercise file writes without weakening the guard for the rest of the suite.
    """
    monkeypatch.setenv("GISCODE_AUTO_APPROVE_EDITS", "1")


@pytest.fixture(autouse=True)
def _cleanup_llama_servers_function():
    """Stop any llama-server this test started, even when the test fails.

    Nothing happens before the test: killing first would take out a server a
    class- or session-scoped fixture had just set up. The old fixture ran a
    machine-wide ``taskkill`` twice per test - about 6 400 process launches for
    a full run - while this one costs a process-tree scan and only spawns a
    kill when it really finds a server this session started.
    """
    yield
    _cleanup_llama_servers()


@pytest.fixture(scope="session", autouse=True)
def _isolate_model_discovery():
    """Keep model discovery away from the developer's installed models.

    ``GISCODE_HOME`` already redirects every profile file, but it deliberately
    leaves ``~/.lmstudio`` visible. Stage 3C's packaged run proved the app then
    discovers those weights and tries to load one at startup. The suite asks
    for the explicit test switch instead: only the profile's models folder is
    scanned and autoload is off. Unset it and the application behaves exactly
    as before, which ``tests/test_test_mode_model_isolation.py`` asserts.
    """
    previous = os.environ.get(TEST_MODE_ENV)
    os.environ[TEST_MODE_ENV] = "1"
    yield
    if previous is None:
        os.environ.pop(TEST_MODE_ENV, None)
    else:
        os.environ[TEST_MODE_ENV] = previous


@pytest.fixture(autouse=True)
def _keep_test_mode_on(_isolate_model_discovery):
    """Re-arm the test switch after a test unsets it deliberately."""
    os.environ.setdefault(TEST_MODE_ENV, "1")
    yield


@pytest.fixture
def model_autoload_enabled(monkeypatch):
    """Run the real startup-autoload path for one test, with fakes.

    The suite sets ``GISCODE_TEST_MODE`` so no test discovers or loads the
    developer's own models. A test that deliberately exercises
    ``_try_autoload_model`` (with a fake manager and a temp models folder)
    opts out here, explicitly, instead of the isolation being loosened for
    everyone.
    """
    monkeypatch.delenv(TEST_MODE_ENV, raising=False)
    yield
    os.environ[TEST_MODE_ENV] = "1"


@pytest.fixture(scope="session", autouse=True)
def _isolate_giscode_home(tmp_path_factory):
    """Point every profile write at a throwaway ``.giscode`` folder.

    ``SettingsManager()`` with no explicit path resolves
    ``giscode_home() / "config.json"``, which is ``~/.giscode/config.json``
    unless ``GISCODE_HOME`` says otherwise (``giscode/config/paths.py`` reads
    that variable on every call). Nothing in this suite redirected it, so a
    test that called ``settings.set(...)`` edited the developer's real
    profile: the window-title test in ``test_resource_budget.py`` left
    ``"selected_model": "Broken"`` in the real config on 2026-10-27.

    The leaf folder is named ``.giscode`` so an isolated path has exactly the
    same shape as the real one, and all profile files - ``config.json``,
    ``gisai_history.db``, ``learning.db``, ``knowledge.db``, snapshots - land
    inside it.

    The folder is anchored at pytest's own base temp directory, not at a fresh
    ``mktemp`` sibling, because the profile root has to keep *containing* the
    tests' ``tmp_path`` directories: ``KnowledgeStoreConfig`` rejects an
    explicit storage path that is not under ``profile_root()``, and those tests
    build their stores in ``tmp_path`` (``<basetemp>/<test>0``). With the
    profile rooted anywhere else, 17 knowledge-store tests fail with
    ``PathSafetyError``.
    """
    home = tmp_path_factory.getbasetemp() / ".giscode"
    home.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("GISCODE_HOME")
    os.environ["GISCODE_HOME"] = str(home)
    yield home
    if previous is None:
        os.environ.pop("GISCODE_HOME", None)
    else:
        os.environ["GISCODE_HOME"] = previous


@pytest.fixture(autouse=True)
def _keep_giscode_home_isolated(_isolate_giscode_home):
    """Re-arm the isolation for tests that delete ``GISCODE_HOME``.

    ``tests/test_isolation.py`` removes the variable to assert the documented
    default (``~/.giscode``). Without this, the rest of the session would be
    back to writing the developer's real profile after that file ran.
    """
    from giscode.config.paths import giscode_home_configured

    if not giscode_home_configured():
        os.environ["GISCODE_HOME"] = str(_isolate_giscode_home)
    yield


class _FakeGisAiEngine:
    def __init__(self, inference, project_context=None):
        self._inference = inference
        self._active_task = None
        self._project_context = project_context
        self.project_context_syncs = []

    def receive(self, text: str, attachments=None, project_context=None):
        from giscode.gisai.types import GisAiTask, GisAiPhase, GisAiTaskStatus
        ctx = project_context if project_context is not None else self._project_context
        if ctx is not None:
            self._project_context = ctx
        project_path = ""
        if ctx is not None:
            project_path = getattr(ctx, "root_path", "")
        task = GisAiTask(
            task_id="fake-gisai-task",
            project_id="test",
            project_path=project_path,
            section="Assistant",
            raw_request=text,
            state=GisAiPhase.EXECUTING,
            status=GisAiTaskStatus.RUNNING,
        )
        self._active_task = task
        return task

    def set_project_context(self, project_context=None):
        self._project_context = project_context
        self.project_context_syncs.append(project_context)

    def execute(self, task, on_status=None):
        class _Msg:
            def __init__(self, role, content):
                self.role = role
                self.content = content
        system_content = "You are GisAi, the intelligence layer of GisCode."
        ctx = self._project_context
        if ctx is not None and getattr(ctx, "root_path", ""):
            system_content += (
                "\n\nActive project context:\n"
                f"- project name: {getattr(ctx, 'name', '') or ''}\n"
                f"- project path: {ctx.root_path}\n"
                f"- active section: {getattr(ctx, 'active_section', '') or 'Overview'}\n"
                "The active project is a SOFTWARE DEVELOPMENT PROJECT. "
                "This GisCode app is a software-development tool; commands such as "
                "'inspect', 'code', 'test', 'build' or 'fix' must be interpreted as "
                "software-development operations on the active project path above, "
                "NOT as GIS/geospatial data operations, unless the user explicitly "
                "says they are working on geospatial/GIS data."
            )
        class _Request:
            def __init__(self, text):
                self.messages = [
                    _Msg("system", system_content),
                    _Msg("user", text),
                ]
        request = _Request(task.raw_request or "")
        response = self._inference.chat_generate(request)
        return {"response": response.text}

    def get_active_task(self):
        return self._active_task

    def cancel(self):
        self._inference.cancel()
        return True


@pytest.fixture(autouse=True)
def _isolate_default_app_stores(tmp_path, monkeypatch):
    """Keep tests off the developer's real ``~/.giscode`` databases.

    ``ProjectStore()`` and ``ChatStore()`` fall back to
    ``~/.giscode/projects.db`` / ``~/.giscode/chats.db`` when no ``db_path``
    is given, and ``MainWindow`` constructs exactly those when a test does not
    pass its own. ``ProjectContextStore`` likewise defaults to
    ``~/.giscode/session_contexts``. Any test that creates, opens, switches or
    closes a project through that path would therefore read - or worse, write -
    the developer's real project data.

    Tests that care about the default path itself can still exercise it by
    patching these functions themselves; this fixture only redirects the
    implicit default so the default is never the user's live data.
    """
    import giscode.projects.store as projects_store
    import giscode.storage.chat_store as chat_store

    monkeypatch.setattr(
        projects_store,
        "_default_db_path",
        lambda: str(tmp_path / "projects.db"),
    )
    monkeypatch.setattr(
        chat_store,
        "_default_db_path",
        lambda: str(tmp_path / "chats.db"),
    )

    # ``ProjectContextStore`` persists contexts as JSON under
    # ``~/.giscode/session_contexts`` and binds that directory as an argument
    # default, so the module constant cannot be patched - rebind the default
    # itself. Without this, any test that opens or switches a project through
    # MainWindow writes a new ``<project id>.json`` file into the developer's
    # real session directory.
    from giscode.projects.context import ProjectContextStore

    monkeypatch.setattr(
        ProjectContextStore.__init__,
        "__defaults__",
        (tmp_path / "session_contexts",),
    )


def _drain_background_threads(timeout: float = 0.25) -> None:
    """Give short-lived workers a brief chance to finish before ``root.destroy()``.

    A Tk root that is destroyed while a worker thread is still running leaves
    that thread holding the last references to the view's tkinter Variables.
    CPython then finalizes them on the *worker* thread, and
    ``tkinter.Variable.__del__`` calls into Tcl off the main loop, which
    raises ``RuntimeError: main thread is not in main loop``. pytest surfaces
    that as an unraisable-exception failure/error in whichever test happens to
    be running at the time, which makes the failure look unrelated to its
    actual cause.

    Waiting briefly for the workers before ``root.destroy()`` keeps all Tk
    finalization on the main thread. This is a test-lifecycle concern only, so
    it is handled here rather than in production code.

    The budget is deliberately small. The workers this guards against (the
    ``_send_worker``/``_run_task`` threads that publish into a plain dict) finish
    in milliseconds, so a short wait is enough. Some tests deliberately leave a
    task in flight; that thread ends up wedged inside a Tk finalizer for the
    rest of the session and can never be joined, so paying a long timeout for
    it on every subsequent test would dominate the whole run for no benefit.
    """
    main = threading.current_thread()
    deadline = time.monotonic() + timeout
    for thread in list(threading.enumerate()):
        if thread is main or not thread.is_alive():
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            thread.join(timeout=remaining)
        except RuntimeError:
            # Thread already finished between the check and the join.
            pass


def _safe_destroy(root) -> None:
    """Tear down a Tk root without leaking off-main-thread finalization."""
    _drain_background_threads()
    try:
        root.destroy()
    except tk.TclError:
        # Already destroyed, or the interpreter went away with a worker.
        pass


@pytest.fixture
def tk_root():
    root = _create_tk_root()
    previous_default_root = _make_default_root(root)
    root.withdraw()
    yield root
    # Restore the default root before destroying this one, so the state other
    # modules observe afterwards is exactly the one they would have seen
    # without the fixture.
    _clear_default_root(root, previous_default_root)
    _safe_destroy(root)


def _make_default_root(root):
    """Point tkinter's default root at *root* for the duration of the test.

    ``tkinter`` keeps a single default root. ``tk.PhotoImage`` registers the
    image on the *default root's* interpreter, while the widget showing it
    lives on the interpreter of the master it was given. When the default
    root is a different (or already destroyed) interpreter, the widget cannot
    see the image ("image pyimageNNN doesn't exist") and the top-bar logo
    silently fails to load. Re-pointing the default root keeps image
    creation and widget construction on the same, live interpreter.

    Returns the default root that was in place before, so the caller can put
    it back afterwards.
    """
    try:
        previous = tk._default_root
    except Exception:
        previous = None
    if previous is root:
        # The root became the default root on creation; putting it back
        # after the test would leave a destroyed interpreter behind.
        previous = None
    try:
        tk._default_root = root
    except Exception:
        pass
    return previous


def _clear_default_root(root, previous=None) -> None:
    """Put the default root that was in place before the test back."""
    try:
        if tk._default_root is root:
            tk._default_root = previous
    except Exception:
        pass


#: How many root creations a probe attempts, and the pause between them.
#: One attempt is exactly what made module-level probes nondeterministic: while
#: pytest imports modules, creating a Tcl interpreter intermittently fails with
#: "Can't find a usable init.tcl" and succeeds milliseconds later.
TK_ATTEMPTS = 5
TK_RETRY_DELAY = 0.05
TK_UNAVAILABLE_REASON = "Tk not available in this environment"

#: The last failure a probe saw, so a skip can say *why* Tk is unavailable.
_LAST_TK_ERROR = ""


def _record_tk_error(exc: BaseException | None) -> None:
    global _LAST_TK_ERROR
    if exc is None:
        _LAST_TK_ERROR = ""
        return
    message = str(exc).strip()
    _LAST_TK_ERROR = f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _create_tk_root():
    """Create a Tk root, retrying the transient ``init.tcl`` failure.

    Raises the last ``tk.TclError`` rather than a bare ``raise`` so a caller can
    tell "headless" apart from a programming error, and records it so the skip
    reason can quote it.
    """
    last: tk.TclError | None = None
    for _attempt in range(TK_ATTEMPTS):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            last = exc
            time.sleep(TK_RETRY_DELAY)
            continue
        _record_tk_error(None)
        return root
    _record_tk_error(last)
    raise last or tk.TclError("Tk could not create an interpreter")


def tk_available() -> bool:
    """True when this process can create a Tcl interpreter right now.

    Used by the module-level GUI probes. Creating interpreters repeatedly in
    one process intermittently fails here with "can't find a usable
    init.tcl" (or "tk.tcl") while pytest is still importing modules -- see
    ``tests/test_gui_text_interaction.py`` for the same, pre-existing flake.
    A bare, single ``tk.Tk()`` attempt used to disable a whole module for the
    session when it lost that race, even though the next creation in the same
    process succeeded. Retry like ``_create_tk_root`` does, and only report
    the GUI as unavailable when every attempt fails (a genuinely headless
    environment).
    """
    try:
        root = _create_tk_root()
    except Exception as exc:  # pragma: no cover - environment guard
        _record_tk_error(exc)
        return False
    try:
        root.withdraw()
    except tk.TclError:
        pass
    finally:
        _safe_destroy(root)
    return True


def tk_skip_reason() -> str:
    """The skip reason for a GUI test: what is missing, and why.

    ``Tk not available in this environment (TclError: no display name ...)``
    tells a reviewer whether the machine is headless or the interpreter is
    broken, instead of the bare phrase every probe used to emit.
    """

    detail = _LAST_TK_ERROR.strip()
    return f"{TK_UNAVAILABLE_REASON} ({detail})" if detail else TK_UNAVAILABLE_REASON


def pytest_sessionfinish(session, exitstatus):
    """Last-resort cleanup, for a session that dies before fixture teardown."""
    _cleanup_llama_servers()
