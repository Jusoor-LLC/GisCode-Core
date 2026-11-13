"""Regression tests for the Windows installer / uninstaller lifecycle.

These exercise the real thing on Windows: real files, a real child process whose
image lives inside the install directory, real exclusive file locks (sharing
violations), the real registry key (redirected to a throwaway test key) and the
real deferred ``cmd`` helper, executed rather than mocked.

The two defects these lock down:

* **U-1** - uninstall invoked through the registered ``UninstallString`` runs the
  *installed* ``GisCodeSetup.exe``; the old code deleted that running image and
  raised ``PermissionError``, so the directory and registry entry survived.
* **U-2** - uninstall while GisCode was still running failed on the locked
  ``GisCode.exe`` and left everything behind.
"""
from __future__ import annotations

import ctypes
import importlib.util
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

SETUP_PATH = (Path(__file__).resolve().parents[1]
              / "packaging" / "windows_installer" / "giscode_setup.py")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the Windows installer is a Windows-only component",
)


def _load_setup_module():
    spec = importlib.util.spec_from_file_location("giscode_setup_under_test", SETUP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = _load_setup_module()


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


def _delete_registry_tree(key: str) -> None:
    """Remove the throwaway test key and its test-only parent.

    Only ever deletes paths this fixture created: walking up the path would end
    at ``Software``, which must never be touched.
    """
    import winreg

    for candidate in (key, "Software\\GisCodeTests"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, candidate)
        except (FileNotFoundError, PermissionError, OSError):
            continue


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated install root, shortcut root, profile and registry key."""
    local = tmp_path / "LocalAppData"
    roaming = tmp_path / "Roaming"
    profile = tmp_path / "profile"
    for folder in (local, roaming, profile):
        folder.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("GISCODE_HOME", str(profile))

    key = "Software\\GisCodeTests\\Uninstall\\" + uuid.uuid4().hex
    monkeypatch.setattr(setup, "UNINSTALL_KEY", key)

    namespace = SimpleNamespace(
        local=local,
        roaming=roaming,
        profile=profile,
        key=key,
        install=local / "Programs" / "GisCode",
        shortcut=roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "GisCode.lnk",
    )
    try:
        yield namespace
    finally:
        _delete_registry_tree(key)


def seed_install(env, *, app=True, setup_copy=True, shortcut=True, registry=True):
    env.install.mkdir(parents=True, exist_ok=True)
    if app:
        (env.install / "GisCode.exe").write_bytes(b"app-binary")
    if setup_copy:
        (env.install / "GisCodeSetup.exe").write_bytes(b"setup-binary")
    if shortcut:
        env.shortcut.parent.mkdir(parents=True, exist_ok=True)
        env.shortcut.write_bytes(b"shortcut")
    if registry:
        setup.write_uninstall_entry(env.install / "GisCode.exe")


def seed_profile(env, name="chats.db", payload=b"real chat history"):
    target = env.profile / name
    target.write_bytes(payload)
    return target


def start_process_from(path: Path):
    """Launch a real process whose executable image is *path*.

    A copy of the running interpreter is used as the "application": it makes the
    file genuinely locked while it runs, which is exactly the U-2 condition.
    """
    process = subprocess.Popen([str(path), "-c", "import time; time.sleep(120)"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        images = {pid: image for pid, image in setup.process_image_paths()}
        if images.get(process.pid) and Path(images[process.pid]) == path:
            return process
        if process.poll() is not None:
            break
        time.sleep(0.05)
    process.kill()
    raise AssertionError(f"helper process never started from {path}")


def stop_process(process) -> None:
    if process is None:
        return
    try:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    except Exception:
        pass


@contextmanager
def exclusive_handle(path: Path):
    """Hold a Windows handle with no sharing: any delete fails with WinError 5/32."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    file_attribute_normal = 0x80
    invalid = ctypes.c_void_p(-1).value
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, 0, None,
                                  OPEN_EXISTING, file_attribute_normal, None)
    assert handle not in (None, invalid), ctypes.get_last_error()
    try:
        yield handle
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


# ---------------------------------------------------------------------------
# happy path and user data
# ---------------------------------------------------------------------------


def test_install_then_uninstall_roundtrip_keeps_user_data(env, tmp_path, monkeypatch):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "GisCode.exe").write_bytes(b"app-binary")
    (payload / "GisCodeSetup.exe").write_bytes(b"setup-binary")
    monkeypatch.setattr(setup, "payload_dir", lambda: payload)

    installed = setup.install()
    assert Path(installed["install_dir"]) == env.install
    assert (env.install / "GisCode.exe").read_bytes() == b"app-binary"
    assert env.shortcut.is_file()
    assert setup.uninstall_entry_exists()

    chat = seed_profile(env)
    result = setup.uninstall()

    assert result["cleanup_complete"] is True
    assert not env.install.exists()
    assert not env.shortcut.exists()
    assert not setup.uninstall_entry_exists()
    assert chat.is_file() and chat.read_bytes() == b"real chat history"


def test_uninstall_after_app_has_exited_cleans_everything(env):
    seed_install(env)
    env.install.joinpath("GisCode.exe").write_bytes(b"x")
    result = setup.uninstall()
    assert result["cleanup_complete"] is True
    assert result["app_processes"]["found"] == []
    assert not env.install.exists()
    assert not setup.uninstall_entry_exists()


# ---------------------------------------------------------------------------
# U-1: uninstall through the installed (running) uninstaller
# ---------------------------------------------------------------------------


def test_self_uninstall_defers_instead_of_deleting_running_image(env, monkeypatch):
    seed_install(env)
    running = env.install / "GisCodeSetup.exe"
    monkeypatch.setattr(setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(setup.sys, "executable", str(running))

    # allow_defer=False isolates the decision: with only our own image left and
    # no helper, the result must say "not complete" rather than claim success.
    result = setup.uninstall(allow_defer=False)

    assert result["cleanup_complete"] is False
    assert result["deferred_self_delete"] is False
    assert result["leftover"] == ["GisCodeSetup.exe"]
    assert running.is_file()
    assert setup.uninstall_entry_exists(), "registry entry must survive for a retry"


def test_deferred_cleanup_script_is_written_with_real_targets(env, monkeypatch):
    seed_install(env)
    running = env.install / "GisCodeSetup.exe"
    spawned: list[list[str]] = []

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            spawned.append(list(argv))

    monkeypatch.setattr(setup.subprocess, "Popen", _FakePopen)
    script = setup.spawn_deferred_cleanup(4321, running, env.install, env.local / "log.txt")

    assert script is not None and script.is_file()
    text = script.read_text(encoding="utf-8")
    assert str(running) in text
    assert str(env.install) in text
    assert env.key in text
    assert "4321" in text
    assert spawned and spawned[0][:2] == ["cmd", "/c"]
    script.unlink()


def test_deferred_cleanup_script_completes_after_owner_exits(env):
    """Execute the real helper with an already-dead PID and verify it finishes."""
    seed_install(env)
    running = env.install / "GisCodeSetup.exe"
    log = env.local / "cleanup.log"

    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(timeout=30)

    script = env.local / "cleanup.cmd"
    script.write_text(
        setup.cleanup_script(finished.pid, running, env.install, log, f"HKCU\\{env.key}"),
        encoding="utf-8", newline="",
    )
    completed = subprocess.run(["cmd", "/c", str(script)], capture_output=True,
                               text=True, timeout=120)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not running.exists()
    assert not env.install.exists()
    assert not setup.uninstall_entry_exists()
    assert "DEFERRED_OK" in log.read_text(encoding="utf-8")
    assert not script.exists(), "the helper removes itself"


# ---------------------------------------------------------------------------
# U-2: uninstall while the application is running
# ---------------------------------------------------------------------------


def test_uninstall_stops_running_app_then_removes_everything(env):
    seed_install(env)
    app = env.install / "GisCode.exe"
    app.unlink()
    import shutil

    shutil.copy2(sys.executable, app)
    process = start_process_from(app)
    try:
        result = setup.uninstall(app_timeout=15)
    finally:
        stop_process(process)

    assert result["app_processes"]["found"], "the running app must be detected"
    assert result["app_processes"]["remaining"] == []
    assert setup._pid_alive(process.pid) is False
    assert result["cleanup_complete"] is True
    assert not env.install.exists()
    assert not setup.uninstall_entry_exists()


def test_processes_outside_the_install_dir_are_never_touched(env, tmp_path):
    seed_install(env)
    elsewhere = tmp_path / "other" / "GisCode.exe"
    elsewhere.parent.mkdir()
    import shutil

    shutil.copy2(sys.executable, elsewhere)
    process = start_process_from(elsewhere)
    try:
        result = setup.uninstall(app_timeout=5)
        assert result["cleanup_complete"] is True
        assert process.poll() is None, "an unrelated GisCode.exe must not be killed"
        assert setup._pid_alive(process.pid) is True
    finally:
        stop_process(process)


# ---------------------------------------------------------------------------
# locked files, partial state, repeated runs, honest failure
# ---------------------------------------------------------------------------


def test_locked_executable_is_reported_incomplete_then_recovered(env):
    seed_install(env)
    app = env.install / "GisCode.exe"

    with exclusive_handle(app):
        result = setup.uninstall(delete_retries=4, delete_delay=0.05)
        assert result["cleanup_complete"] is False
        assert "GisCode.exe" in result["leftover"]
        assert env.install.is_dir()
        assert setup.uninstall_entry_exists()

    recovered = setup.uninstall(delete_retries=10, delete_delay=0.05)
    assert recovered["cleanup_complete"] is True
    assert not env.install.exists()
    assert not setup.uninstall_entry_exists()


def test_partially_removed_install_is_finished_by_a_rerun(env):
    seed_install(env)
    (env.install / "GisCode.exe").unlink()
    result = setup.uninstall()
    assert result["cleanup_complete"] is True
    assert not env.install.exists()


def test_repeated_uninstall_is_idempotent(env):
    seed_install(env)
    setup.uninstall()
    second = setup.uninstall()
    assert second["cleanup_complete"] is True
    assert second["removed"] == []
    assert second["leftover"] == []


def test_main_returns_nonzero_when_cleanup_is_incomplete(env, capsys):
    seed_install(env)
    with exclusive_handle(env.install / "GisCode.exe"):
        code = setup.main(["GisCodeSetup.exe", "--uninstall", "--silent"])
    output = capsys.readouterr().out
    assert code == 2
    assert "CLEANUP INCOMPLETE" in output
    assert "LEFTOVER" in output


def test_main_returns_zero_when_cleanup_completes(env, capsys):
    seed_install(env)
    code = setup.main(["GisCodeSetup.exe", "--uninstall", "--silent"])
    output = capsys.readouterr().out
    assert code == 0
    assert "CLEANUP COMPLETE" in output


def test_uninstall_refuses_when_profile_overlaps_install_dir(env, monkeypatch):
    seed_install(env)
    monkeypatch.setenv("GISCODE_HOME", str(env.install))

    with pytest.raises(SystemExit) as excinfo:
        setup.uninstall()

    assert "Refusing to uninstall" in str(excinfo.value)
    assert (env.install / "GisCode.exe").is_file(), "nothing may be deleted"
    assert setup.uninstall_entry_exists()


# ---------------------------------------------------------------------------
# U-3: the application's own ``workspace`` folder, created on first launch
# ---------------------------------------------------------------------------

def scaffold_workspace(env):
    """Recreate exactly what the app creates in its working directory.

    ``giscode/workspace/workspace.py`` makes ``workspace/{src,tests,docs,build,logs}``
    next to the executable when the Start Menu shortcut launches it.
    """
    workspace = env.install / "workspace"
    for name in ("src", "tests", "docs", "build", "logs"):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    return workspace


def test_uninstall_removes_empty_workspace_scaffold(env):
    seed_install(env)
    workspace = scaffold_workspace(env)
    assert workspace.is_dir()

    result = setup.uninstall()

    assert result["cleanup_complete"] is True
    assert result["leftover"] == []
    assert result["user_data_kept"] == []
    assert not env.install.exists()
    assert not env.shortcut.exists()
    assert not setup.uninstall_entry_exists()


def test_uninstall_keeps_user_files_in_workspace_and_removes_registration(env):
    seed_install(env)
    workspace = scaffold_workspace(env)
    project = workspace / "src" / "app.py"
    project.write_text("print('mine')\n", encoding="utf-8")

    result = setup.uninstall()

    assert result["cleanup_complete"] is True
    assert result["leftover"] == []
    assert result["user_data_kept"] == [str(workspace)]
    assert project.read_text(encoding="utf-8") == "print('mine')\n"
    assert not (env.install / "GisCode.exe").exists()
    assert not (env.install / "GisCodeSetup.exe").exists()
    assert not env.shortcut.exists()
    assert not setup.uninstall_entry_exists()


def test_cli_reports_kept_workspace_and_exits_zero(env, capsys):
    seed_install(env)
    workspace = scaffold_workspace(env)
    notes = workspace / "docs" / "notes.md"
    notes.write_text("keep me\n", encoding="utf-8")

    code = setup.main(["GisCodeSetup.exe", "--uninstall", "--silent"])
    output = capsys.readouterr().out

    assert code == 0
    assert f"USER_DATA_KEPT {workspace}" in output
    assert "CLEANUP COMPLETE" in output
    assert notes.is_file()


def test_locked_program_file_with_workspace_is_incomplete_then_recovers(env):
    seed_install(env)
    workspace = scaffold_workspace(env)
    keep = workspace / "logs" / "session.log"
    keep.write_text("user log\n", encoding="utf-8")

    with exclusive_handle(env.install / "GisCode.exe"):
        result = setup.uninstall(delete_retries=4, delete_delay=0.05)
        assert result["cleanup_complete"] is False
        assert "GisCode.exe" in result["leftover"]
        assert setup.uninstall_entry_exists(), "registration must survive a failed uninstall"
        assert keep.is_file()

    recovered = setup.uninstall(delete_retries=10, delete_delay=0.05)
    assert recovered["cleanup_complete"] is True
    assert recovered["leftover"] == []
    assert keep.is_file()
    assert not setup.uninstall_entry_exists()


def test_reinstall_after_uninstall_preserves_workspace(env, tmp_path, monkeypatch):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "GisCode.exe").write_bytes(b"app-binary-v1")
    (payload / "GisCodeSetup.exe").write_bytes(b"setup-binary")
    monkeypatch.setattr(setup, "payload_dir", lambda: payload)

    setup.install()
    workspace = scaffold_workspace(env)
    project = workspace / "src" / "app.py"
    project.write_text("mine\n", encoding="utf-8")

    setup.uninstall()
    assert project.is_file()

    again = setup.install()
    assert Path(again["install_dir"]) == env.install
    assert (env.install / "GisCode.exe").read_bytes() == b"app-binary-v1"
    assert env.shortcut.is_file()
    assert setup.uninstall_entry_exists()
    assert project.read_text(encoding="utf-8") == "mine\n"


def test_deferred_cleanup_keeps_workspace_and_removes_registration(env):
    seed_install(env)
    workspace = scaffold_workspace(env)
    keep = workspace / "docs" / "plan.md"
    keep.write_text("roadmap\n", encoding="utf-8")
    log = env.local / "cleanup.log"

    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(timeout=30)

    script = env.local / "cleanup-user-data.cmd"
    script.write_text(
        setup.cleanup_script(finished.pid, env.install / "GisCodeSetup.exe",
                             env.install, log, f"HKCU\\{env.key}"),
        encoding="utf-8", newline="",
    )
    completed = subprocess.run(["cmd", "/c", str(script)], capture_output=True,
                               text=True, timeout=120)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert keep.is_file()
    assert not (env.install / "GisCode.exe").exists()
    assert not setup.uninstall_entry_exists()
    text = log.read_text(encoding="utf-8")
    assert "DEFERRED_OK user_data_kept" in text
    assert not script.exists(), "the helper removes itself"
