"""GisCode Pro parity for the Windows uninstall cleanup fix.

This checkout ships exactly one Windows installer:
``packaging/windows_installer/giscode_setup.py``. The Pro edition is produced
from the same code base - ``GisCode.spec`` selects ``GisCodePro.exe`` at build
time and ``GisCodeSetup.spec`` packs whichever application binary
``GISCODE_APP_EXE`` points at - so there is no separate Pro uninstaller: a Pro
build runs ``uninstall()`` verbatim.

These tests pin that claim from the Pro side:

* the installer module imports nothing from ``giscode``, so the Core installer
  does not depend on Pro (or on Core application code), and neither does a Pro
  build of it;
* the workspace scenarios the Core suite covers behave identically under Pro
  branding (``GisCode Pro``, ``GisCodePro.exe``, a ``GisCodePro`` uninstall key);
* the generated deferred helper names the Pro program files and the Pro
  registry key instead of Core's hard-coded names.
"""
from __future__ import annotations

import ast
import ctypes
import importlib.util
import sys
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
    spec = importlib.util.spec_from_file_location("giscode_setup_pro_parity", SETUP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = _load_setup_module()


def _delete_registry_tree(key: str) -> None:
    import winreg

    for candidate in (key, "Software\\GisCodeTests"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, candidate)
        except (FileNotFoundError, PermissionError, OSError):
            continue


@pytest.fixture
def pro_env(tmp_path, monkeypatch):
    """A Pro-branded install root, shortcut, profile and uninstall key."""
    local = tmp_path / "LocalAppData"
    roaming = tmp_path / "Roaming"
    profile = tmp_path / "profile"
    for folder in (local, roaming, profile):
        folder.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("GISCODE_HOME", str(profile))

    # Exactly the branding constants a Pro build of this installer would carry.
    monkeypatch.setattr(setup, "APP_NAME", "GisCode Pro")
    monkeypatch.setattr(setup, "DISPLAY_NAME", "GisCode Pro")
    monkeypatch.setattr(setup, "EXE_NAME", "GisCodePro.exe")

    key = "Software\\GisCodeTests\\Uninstall\\Pro" + uuid.uuid4().hex
    monkeypatch.setattr(setup, "UNINSTALL_KEY", key)

    namespace = SimpleNamespace(
        local=local,
        roaming=roaming,
        profile=profile,
        key=key,
        install=setup.default_install_dir(),
        shortcut=setup.start_menu_shortcut(),
    )
    try:
        yield namespace
    finally:
        _delete_registry_tree(key)


def seed_pro_install(env) -> None:
    env.install.mkdir(parents=True, exist_ok=True)
    (env.install / setup.EXE_NAME).write_bytes(b"pro-binary")
    (env.install / setup.SETUP_NAME).write_bytes(b"setup-binary")
    env.shortcut.parent.mkdir(parents=True, exist_ok=True)
    env.shortcut.write_bytes(b"shortcut")
    setup.write_uninstall_entry(env.install / setup.EXE_NAME)


def scaffold_workspace(env) -> Path:
    """Exactly what the application creates in its working directory."""
    workspace = env.install / "workspace"
    for name in ("src", "tests", "docs", "build", "logs"):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    return workspace


@contextmanager
def exclusive_handle(path: Path):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    invalid = ctypes.c_void_p(-1).value
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, 0, None,
                                  OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    assert handle not in (None, invalid), ctypes.get_last_error()
    try:
        yield handle
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


# ---------------------------------------------------------------------------
# independence
# ---------------------------------------------------------------------------

def test_the_installer_shares_no_code_with_the_application():
    """The installer is edition-agnostic: it imports no ``giscode`` module."""
    tree = ast.parse(SETUP_PATH.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    off_limits = sorted(
        name for name in modules
        if name == "giscode" or name.startswith("giscode.")
    )
    assert off_limits == [], f"installer imports application code: {off_limits}"


# ---------------------------------------------------------------------------
# the workspace scenarios, under Pro branding
# ---------------------------------------------------------------------------

def test_pro_uninstall_removes_empty_workspace_scaffold(pro_env):
    seed_pro_install(pro_env)
    workspace = scaffold_workspace(pro_env)
    assert workspace.is_dir()

    result = setup.uninstall()

    assert result["cleanup_complete"] is True
    assert result["leftover"] == []
    assert result["user_data_kept"] == []
    assert not pro_env.install.exists()
    assert not pro_env.shortcut.exists()
    assert not setup.uninstall_entry_exists()


def test_pro_uninstall_keeps_user_files_and_removes_registration(pro_env):
    seed_pro_install(pro_env)
    workspace = scaffold_workspace(pro_env)
    project = workspace / "src" / "app.py"
    project.write_text("print('pro user data')\n", encoding="utf-8")

    result = setup.uninstall()

    assert result["cleanup_complete"] is True
    assert result["leftover"] == []
    assert result["user_data_kept"] == [str(workspace)]
    assert project.read_text(encoding="utf-8") == "print('pro user data')\n"
    assert not (pro_env.install / "GisCodePro.exe").exists()
    assert not (pro_env.install / "GisCodeSetup.exe").exists()
    assert not pro_env.shortcut.exists()
    assert not setup.uninstall_entry_exists()


def test_pro_locked_program_file_reports_incomplete_and_keeps_registration(pro_env):
    seed_pro_install(pro_env)
    workspace = scaffold_workspace(pro_env)
    keep = workspace / "docs" / "notes.md"
    keep.write_text("keep\n", encoding="utf-8")

    with exclusive_handle(pro_env.install / "GisCodePro.exe"):
        result = setup.uninstall(delete_retries=4, delete_delay=0.05)
        assert result["cleanup_complete"] is False
        assert "GisCodePro.exe" in result["leftover"]
        assert setup.uninstall_entry_exists(), "registration must survive a failed uninstall"
        assert keep.is_file()

    recovered = setup.uninstall(delete_retries=10, delete_delay=0.05)
    assert recovered["cleanup_complete"] is True
    assert recovered["leftover"] == []
    assert keep.is_file()
    assert not setup.uninstall_entry_exists()


def test_pro_uninstall_entry_records_pro_branding(pro_env):
    seed_pro_install(pro_env)
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, pro_env.key) as key:
        display_name = winreg.QueryValueEx(key, "DisplayName")[0]
        install_location = winreg.QueryValueEx(key, "InstallLocation")[0]

    assert display_name == f"GisCode Pro {setup.VERSION}"
    assert install_location == str(pro_env.install)


# ---------------------------------------------------------------------------
# deferred helper (uninstall run from the installed copy)
# ---------------------------------------------------------------------------

def test_pro_deferred_helper_targets_pro_names_and_registry_key(pro_env):
    log = pro_env.local / "cleanup.log"
    text = setup.cleanup_script(
        4321,
        pro_env.install / "GisCodeSetup.exe",
        pro_env.install,
        log,
        f"HKCU\\{pro_env.key}",
    )

    assert "set \"PROGRAM_FILES=GisCodePro.exe GisCodeSetup.exe\"" in text
    assert "GisCode.exe" not in text.replace("GisCodePro.exe", ""), (
        "the deferred helper must not assume Core's executable name"
    )
    assert f"HKCU\\{pro_env.key}" in text
    assert str(pro_env.install) in text
