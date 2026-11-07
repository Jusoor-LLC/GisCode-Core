"""Proof that the suite never touches a llama-server it did not start.

The suite used to run ``taskkill /F /IM llama-server.exe`` before and after
every single test. That killed the developer's own model server on the first
test of a run, and because ``GISCODE_KEEP_LLAMA_SERVER=1`` disabled it
wholesale, the servers the suite's *own* tests spawned survived as orphans -
six were still running when this file was written, every one of them with a
dead parent PID.

These tests pin the contract:

* a llama-server this session started is stopped, even when its test fails and
  even under ``GISCODE_KEEP_LLAMA_SERVER=1``;
* a llama-server started by something else - a pre-existing one, or one whose
  launcher has already exited - survives a whole session untouched;
* the legacy machine-wide kill is opt-in only
  (``GISCODE_TEST_KILL_ALL_SERVERS=1``) and is never the default;
* the real-model tests skip by default;
* the two ``test_inference_chat`` connection tests no longer depend on port
  8080 being free.

The "dummy servers" are real, self-contained Windows executables renamed to
``llama-server.exe``, so psutil reports the same image name the production
cleanup matches on.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import requires_gisai
REPO_ROOT = Path(__file__).resolve().parents[1]
CONFTEST = Path(__file__).resolve().parent / "conftest.py"
PYTHON = sys.executable
DUMMY_IMAGE = "llama-server.exe"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_LLAMA_PORT = 8080


# ---------------------------------------------------------------------------
# dummy servers
# ---------------------------------------------------------------------------


def _system_executable() -> Path:
    """A self-contained executable that runs for a while without a console."""
    candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe"
    if not candidate.is_file():
        pytest.skip(f"no stand-in executable available at {candidate}")
    return candidate


def _dummy_image(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / DUMMY_IMAGE
    if not target.exists():
        shutil.copy2(_system_executable(), target)
    return target


def _spawn_dummy(folder: Path, seconds: int = 90) -> subprocess.Popen:
    return subprocess.Popen(
        [str(_dummy_image(folder)), "-n", str(seconds), "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(folder),
        creationflags=CREATE_NO_WINDOW,
    )


def _spawn_foreign_dummy(folder: Path, seconds: int = 90) -> int:
    """Start a dummy server that belongs to nobody's session.

    A launcher process starts it and exits, so the dummy's parent PID is gone -
    exactly the state of a server another program left running. It is not a
    descendant of this pytest process and never appears in its child scan.
    """
    launcher = subprocess.run(
        [
            PYTHON,
            "-c",
            (
                "import subprocess, sys, time;"
                "p = subprocess.Popen([sys.argv[1], '-n', sys.argv[2], '127.0.0.1'],"
                " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=sys.argv[3],"
                " creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0));"
                "print(p.pid);"
                "time.sleep(1.0)"
            ),
            str(_dummy_image(folder)),
            str(seconds),
            str(folder),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    pid = int(launcher.stdout.strip())
    assert _wait_until(lambda: _process(pid) is not None), "the foreign dummy never started"
    return pid


def _process(pid: int):
    import psutil

    try:
        return psutil.Process(pid)
    except Exception:
        return None


def _is_alive(pid: int) -> bool:
    proc = _process(pid)
    try:
        return proc is not None and proc.is_running()
    except Exception:
        return False


def _image_name(pid: int) -> str:
    proc = _process(pid)
    if proc is None:
        return ""
    try:
        return proc.name() or ""
    except Exception:
        return ""


def _wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.15)
    return bool(predicate())


def _wait_gone(pid: int, timeout: float = 20.0) -> bool:
    return _wait_until(lambda: not _is_alive(pid), timeout=timeout)


def _kill_own(pid: int) -> None:
    """Cleanup for a dummy this test started, by its own PID."""
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=20
    )


def _reap_dummies(*folders: Path) -> None:
    """Stop any dummy this test started that is still running, by its own PID.

    Used from ``finally`` blocks so that a *failing* server-safety test cannot
    itself leak a llama-server - the exact failure mode this whole file exists
    to prevent. Only processes whose command line points into one of the given
    folders can match, which a real server on this machine never does.
    """
    import psutil

    wanted = [str(folder) for folder in folders]
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            command = " ".join(proc.info.get("cmdline") or [])
        except Exception:
            continue
        if DUMMY_IMAGE.lower() not in name:
            continue
        if any(folder in command for folder in wanted):
            _kill_own(int(proc.info["pid"]))


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def _recording_run(calls: list) -> "callable":
    def _run(command, **kwargs):
        calls.append(list(command))
        return _FakeCompleted()

    return _run


# ---------------------------------------------------------------------------
# nested sessions
# ---------------------------------------------------------------------------

_NESTED_HELPER_SOURCE = '''"""Helpers for the nested sessions the server-safety tests start."""
import os
import shutil
import subprocess
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DUMMY_IMAGE = "llama-server.exe"


def spawn_dummy(folder, seconds=90):
    """A real executable named llama-server.exe, started by the nested session."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / DUMMY_IMAGE
    if not exe.exists():
        root = os.environ.get("SystemRoot", r"C:\\Windows")
        shutil.copy2(Path(root) / "System32" / "ping.exe", exe)
    return subprocess.Popen(
        [str(exe), "-n", str(seconds), "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(folder),
        creationflags=CREATE_NO_WINDOW,
    )
'''

_NESTED_SPAWN_RECORD = '''
    proc = _nested_helper.spawn_dummy(os.environ["NESTED_ROOT"])
    Path(os.environ["NESTED_PID_FILE"]).write_text(str(proc.pid), encoding="utf-8")
    assert proc.poll() is None, "the dummy server died before the assertion"
    Path(os.environ["NESTED_ALIVE_FILE"]).write_text("alive", encoding="utf-8")
'''

NESTED_FAILS_AFTER_SPAWNING = (
    "import os\nfrom pathlib import Path\n\nimport _nested_helper\n\n\n"
    "def test_spawns_a_server_then_fails():\n"
    "    \"\"\"The session must stop its own server even though this test fails.\"\"\"\n"
    + _NESTED_SPAWN_RECORD
    + "    assert False, \"deliberate failure: cleanup must still run\"\n"
)

NESTED_PASSES_AFTER_SPAWNING = (
    "import os\nfrom pathlib import Path\n\nimport _nested_helper\n\n\n"
    "def test_spawns_a_server_and_passes():\n"
    + _NESTED_SPAWN_RECORD
    + "    assert proc.pid > 0\n"
)


def _nested_env(work: Path, **extra) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["NESTED_ROOT"] = str(work / "servers")
    env["NESTED_PID_FILE"] = str(work / "dummy_pid.txt")
    env["NESTED_ALIVE_FILE"] = str(work / "dummy_alive.txt")
    for var in (
        "GISCODE_KEEP_LLAMA_SERVER",
        "GISCODE_TEST_KILL_ALL_SERVERS",
        "GISCODE_RUN_REAL_MODEL_TESTS",
    ):
        env.pop(var, None)
    env.update(extra)
    return env


def _run_nested_session(tmp_path, test_source: str, label: str, **env_extra):
    """Run a real pytest session in a temp dir with a fresh copy of conftest.

    The nested session exercises ``tests/conftest.py`` exactly as a developer
    would run it: its own process tree, its own isolated ``GISCODE_HOME`` and
    its own cleanup.
    """
    work = tmp_path / f"session-{label}"
    work.mkdir(parents=True, exist_ok=True)
    (work / "conftest.py").write_text(CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")
    (work / "_nested_helper.py").write_text(_NESTED_HELPER_SOURCE, encoding="utf-8")
    (work / "test_nested.py").write_text(test_source, encoding="utf-8")
    return subprocess.run(
        [
            PYTHON,
            "-m",
            "pytest",
            "test_nested.py",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / f"basetemp-{label}"),
        ],
        cwd=str(work),
        env=_nested_env(work, **env_extra),
        capture_output=True,
        text=True,
        timeout=300,
    ), work


def _nested_dummy_pid(work: Path) -> int:
    pid_file = work / "dummy_pid.txt"
    assert pid_file.is_file(), "the nested test never recorded its server PID"
    return int(pid_file.read_text(encoding="utf-8"))


def _repo_test_env(**extra) -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for var in ("GISCODE_TEST_KILL_ALL_SERVERS", "GISCODE_RUN_REAL_MODEL_TESTS"):
        env.pop(var, None)
    # Never let a nested run disturb a server on this machine.
    env["GISCODE_KEEP_LLAMA_SERVER"] = "1"
    env.update(extra)
    return env


def _run_repo_tests(tmp_path, targets, label: str, extra_args=(), **env_extra):
    return subprocess.run(
        [
            PYTHON,
            "-m",
            "pytest",
            *targets,
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / f"basetemp-{label}"),
            *extra_args,
        ],
        cwd=str(REPO_ROOT),
        env=_repo_test_env(**env_extra),
        capture_output=True,
        text=True,
        timeout=300,
    )


# ---------------------------------------------------------------------------
# the cleanup itself
# ---------------------------------------------------------------------------


def test_a_server_this_session_started_is_found_and_stopped(tmp_path):
    """The child scan sees a server this process started, and only that one."""
    import tests.conftest as conftest

    proc = _spawn_dummy(tmp_path / "servers")
    try:
        assert _image_name(proc.pid) == DUMMY_IMAGE, "the dummy is not named llama-server.exe"
        assert proc.pid in conftest._session_server_pids()
        stopped = conftest._cleanup_llama_servers()
        assert proc.pid in stopped, f"the cleanup did not report stopping {proc.pid}"
        assert _wait_gone(proc.pid), f"server {proc.pid} survived the cleanup"
    finally:
        if _is_alive(proc.pid):
            _kill_own(proc.pid)


def test_a_foreign_server_is_not_in_the_sessions_process_tree(tmp_path):
    """A server whose launcher exited is invisible to the child scan.

    This is what makes the new cleanup safe to run on a developer's machine:
    such a process is not a descendant of the pytest process, so the scan that
    finds the session's own servers cannot find it.
    """
    import tests.conftest as conftest

    foreign_pid = _spawn_foreign_dummy(tmp_path / "servers")
    try:
        assert _image_name(foreign_pid) == DUMMY_IMAGE
        assert foreign_pid not in conftest._session_server_pids()
    finally:
        _kill_own(foreign_pid)
        _wait_gone(foreign_pid)
        _reap_dummies(tmp_path)


def test_a_failing_test_still_gets_its_own_server_stopped(tmp_path):
    try:
        result, work = _run_nested_session(
            tmp_path, NESTED_FAILS_AFTER_SPAWNING, label="failing"
        )
        pid = _nested_dummy_pid(work)
        assert "1 failed" in result.stdout, result.stdout + result.stderr
        assert (work / "dummy_alive.txt").read_text(encoding="utf-8") == "alive", (
            "the dummy server was not alive when the nested test failed, so this "
            "proves nothing"
        )
        assert _wait_gone(pid), (
            f"the session left its own llama-server {pid} running after a failing test"
        )
    finally:
        _reap_dummies(tmp_path)


def test_keep_llama_server_still_stops_the_sessions_own_server(tmp_path):
    """``GISCODE_KEEP_LLAMA_SERVER=1`` is not an escape from our own cleanup.

    It means "touch no server that this session did not start".
    """
    try:
        result, work = _run_nested_session(
            tmp_path,
            NESTED_PASSES_AFTER_SPAWNING,
            label="keep",
            GISCODE_KEEP_LLAMA_SERVER="1",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (work / "dummy_alive.txt").read_text(encoding="utf-8") == "alive"
        pid = _nested_dummy_pid(work)
        assert _wait_gone(pid), (
            f"under GISCODE_KEEP_LLAMA_SERVER=1 the session left its own server {pid} running"
        )
    finally:
        _reap_dummies(tmp_path)


@pytest.mark.parametrize(
    "env_extra",
    [{}, {"GISCODE_KEEP_LLAMA_SERVER": "1"}],
    ids=["default", "keep-llama-server"],
)
def test_a_foreign_server_survives_a_whole_session(tmp_path, env_extra):
    """A whole nested session may not stop a server it did not start."""
    foreign_pid = _spawn_foreign_dummy(tmp_path / "foreign")
    try:
        assert _image_name(foreign_pid) == DUMMY_IMAGE
        result, work = _run_nested_session(
            tmp_path, NESTED_PASSES_AFTER_SPAWNING, label=f"foreign-{len(env_extra)}", **env_extra
        )
        assert result.returncode == 0, result.stdout + result.stderr
        nested_pid = _nested_dummy_pid(work)
        assert nested_pid != foreign_pid
        assert _wait_gone(nested_pid), "the session did not stop its own server"
        assert _is_alive(foreign_pid), (
            "a llama-server this session did not start was stopped by the session"
        )
        assert _image_name(foreign_pid) == DUMMY_IMAGE
    finally:
        _kill_own(foreign_pid)
        _wait_gone(foreign_pid)
        _reap_dummies(tmp_path)


# ---------------------------------------------------------------------------
# the opt-in machine-wide kill
# ---------------------------------------------------------------------------


def test_the_default_cleanup_never_kills_by_image_name(monkeypatch):
    import tests.conftest as conftest

    monkeypatch.delenv(conftest.KEEP_LLAMA_SERVER_ENV, raising=False)
    monkeypatch.delenv(conftest.KILL_ALL_SERVERS_ENV, raising=False)
    assert conftest._foreign_kill_enabled() is False

    calls: list = []
    monkeypatch.setattr(conftest.subprocess, "run", _recording_run(calls))
    conftest._cleanup_llama_servers()
    assert not any("/IM" in command for command in calls), calls


def test_the_machine_wide_kill_is_opt_in_and_keep_wins(monkeypatch):
    """The opt-in still produces the documented legacy command, and only then.

    ``subprocess.run`` is recorded rather than executed, so this proves the
    opt-in's behaviour without ever running a machine-wide kill - the machine
    these tests run on has real llama-servers that must not be touched.
    """
    import tests.conftest as conftest

    monkeypatch.delenv(conftest.KEEP_LLAMA_SERVER_ENV, raising=False)
    monkeypatch.setenv(conftest.KILL_ALL_SERVERS_ENV, "1")
    assert conftest._foreign_kill_enabled() is True

    calls: list = []
    monkeypatch.setattr(conftest.subprocess, "run", _recording_run(calls))
    conftest._kill_llama_servers()
    assert ["taskkill", "/F", "/IM", "llama-server.exe"] in calls, calls

    monkeypatch.setenv(conftest.KEEP_LLAMA_SERVER_ENV, "1")
    assert conftest._foreign_kill_enabled() is False
    calls.clear()
    conftest._cleanup_llama_servers()
    assert not any("/IM" in command for command in calls), calls


def test_conftest_keeps_the_harness_injection_point():
    """``tools/rerun_harness.py`` injects an early return under this exact line.

    Its own tests use a synthetic copy of conftest, so nothing else would
    notice if this refactor renamed or moved the marker.
    """
    import tests.conftest as conftest

    assert callable(conftest._kill_llama_servers)
    text = CONFTEST.read_text(encoding="utf-8")
    marker = "def _kill_llama_servers():\n"
    assert text.count(marker) == 1, "the harness injection marker is missing or duplicated"
    injected = text.replace(
        marker, marker + "    return  # harness: keep the model server this run is using\n", 1
    )
    assert "harness: keep the model server" in injected


# ---------------------------------------------------------------------------
# machine-independent test behaviour
# ---------------------------------------------------------------------------


@requires_gisai
def test_real_model_tests_skip_by_default(tmp_path):
    """The two real-model files must not load a model unless asked to."""
    result = _run_repo_tests(
        tmp_path,
        ["tests/test_vision_real_local.py", "tests/test_gisai_no_project_vision.py"],
        label="real-model",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 skipped" in result.stdout, result.stdout
    assert "GISCODE_RUN_REAL_MODEL_TESTS" in result.stdout, result.stdout


def test_connection_tests_pass_with_a_listener_on_the_default_port(tmp_path):
    """They must pass whether or not something answers on 8080.

    If 8080 is already taken, this skips instead of binding it: the server
    holding it belongs to someone else. (The reverse case - a real server on
    8080 - is the situation these tests were written in, and they pass there
    too, so both directions are covered.)
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR: on Windows that option would let this hijack a port
        # that is already in use.
        listener.bind(("127.0.0.1", DEFAULT_LLAMA_PORT))
    except OSError:
        listener.close()
        pytest.skip(
            f"127.0.0.1:{DEFAULT_LLAMA_PORT} is already in use by a server this test did "
            "not start; not disturbing it"
        )
    try:
        listener.listen(8)
        # Prove the listener really answers on that port.
        with socket.create_connection(("127.0.0.1", DEFAULT_LLAMA_PORT), timeout=5):
            pass
        result = _run_repo_tests(
            tmp_path,
            ["tests/test_inference_chat.py"],
            label="port-8080",
            extra_args=[
                "-k",
                "raises_when_server_not_running or raises_when_process_died",
            ],
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout, result.stdout
    finally:
        listener.close()
