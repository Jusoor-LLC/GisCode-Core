"""GisCode Core per-user installer / uninstaller for Windows.

Built with PyInstaller into ``GisCodeSetup.exe`` with ``GisCode.exe`` as a data
file. It installs per user (no administrator rights, no service, no machine-wide
state):

* files  -> ``%LOCALAPPDATA%\\Programs\\GisCode``
* shortcut -> ``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\GisCode.lnk``
* metadata -> ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\GisCode``

User data lives in ``%USERPROFILE%\\.giscode`` (or ``GISCODE_HOME``) and is
never written, moved or deleted by this program. Uninstall removes the program
files, the shortcut and the registry entry and leaves the profile untouched.

Uninstall lifecycle (see :func:`uninstall`):

1. every process running *from the install directory* is asked to close
   (``WM_CLOSE``), and only if it refuses is it terminated - always by PID whose
   image path still lies inside that directory, never by image name;
2. files are deleted with bounded retries, so a process that is still exiting
   does not fail the uninstall;
3. the running uninstaller cannot delete itself, so that single file is handed
   to a detached ``cmd`` helper which waits for this process to exit, deletes it,
   removes the directory, then deletes the registry entry and records what it did
   in ``%LOCALAPPDATA%\\GisCode-uninstall.log``;
4. the result distinguishes *complete*, *deferred* and *incomplete*, and an
   incomplete uninstall exits non-zero while keeping the registry entry so it can
   be retried.

Usage:
    GisCodeSetup.exe                 # small GUI
    GisCodeSetup.exe --install --silent [--dir PATH]
    GisCodeSetup.exe --uninstall --silent [--dir PATH]
    GisCodeSetup.exe --version
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "GisCode"
DISPLAY_NAME = "GisCode Core"
PUBLISHER = "Jusoor LLC"
VERSION = "0.1.1"
EXE_NAME = "GisCode.exe"
SETUP_NAME = "GisCodeSetup.exe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\GisCode"
CLEANUP_LOG_NAME = "GisCode-uninstall.log"

WM_CLOSE = 0x0010
CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259


def payload_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def start_menu_shortcut() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"


def profile_dir() -> Path:
    explicit = os.environ.get("GISCODE_HOME")
    if explicit:
        return Path(explicit)
    return Path.home() / ".giscode"


def cleanup_log_path() -> Path:
    """Where deferred cleanup reports what it did.

    Deliberately outside the install directory: the directory is what gets
    removed, so a log inside it would vanish exactly when it is needed.
    """
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / CLEANUP_LOG_NAME


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_shortcut(target: Path) -> Path:
    """Create the Start Menu link through WScript.Shell.

    The frozen build does not ship ``pywin32``, so the COM object is reached
    through PowerShell instead of ``win32com.client``. A failure here is raised,
    never swallowed: an install without its shortcut and uninstall entry is not
    a successful install.
    """
    link = start_menu_shortcut()
    link.parent.mkdir(parents=True, exist_ok=True)

    def q(value: object) -> str:
        return str(value).replace("'", "''")

    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath='%s';"
        "$s.WorkingDirectory='%s';"
        "$s.Description='%s';"
        "$s.IconLocation='%s,0';"
        "$s.Save()" % (q(link), q(target), q(target.parent),
                       q(f"{DISPLAY_NAME} — local-first coding agent"), q(target))
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    if not link.is_file():
        raise SystemExit(
            f"Shortcut could not be created ({link}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return link


def write_uninstall_entry(target: Path) -> None:
    import winreg

    uninstaller = target.parent / SETUP_NAME
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        values = {
            "DisplayName": f"{DISPLAY_NAME} {VERSION}",
            "DisplayVersion": VERSION,
            "Publisher": PUBLISHER,
            "InstallLocation": str(target.parent),
            "DisplayIcon": str(target),
            "UninstallString": f'"{uninstaller}" --uninstall --silent',
            "QuietUninstallString": f'"{uninstaller}" --uninstall --silent',
            "EstimatedSize": max(1, sum(p.stat().st_size for p in target.parent.glob("*")
                                        if p.is_file()) // 1024),
            "NoModify": 1,
            "NoRepair": 1,
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ if isinstance(value, str) else winreg.REG_DWORD, value)


def remove_uninstall_entry() -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


def uninstall_entry_exists() -> bool:
    """True when the Add/Remove Programs entry for this product is present."""
    import winreg

    try:
        handle = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    try:
        winreg.CloseKey(handle)
    except OSError:
        pass
    return True


def install(directory: Path | None = None) -> dict:
    source = payload_dir() / EXE_NAME
    if not source.is_file():
        raise SystemExit(f"Payload missing: {source}")
    target_dir = Path(directory) if directory else default_install_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    previous = ""
    installed = target_dir / EXE_NAME
    if installed.is_file():
        previous = sha256(installed)
    shutil.copy2(source, installed)
    # The uninstall command in the registry points at the installed copy, so a
    # real copy of this setup binary must exist there. In a frozen onefile build
    # the running executable is the setup binary itself.
    setup_source = None
    if getattr(sys, "frozen", False):
        setup_source = Path(sys.executable)
    elif (payload_dir() / SETUP_NAME).is_file():
        setup_source = payload_dir() / SETUP_NAME
    if setup_source and setup_source.is_file() and setup_source.name.lower() != "python.exe":
        shutil.copy2(setup_source, target_dir / SETUP_NAME)
    link = make_shortcut(installed)
    write_uninstall_entry(installed)
    return {
        "install_dir": str(target_dir),
        "exe": str(installed),
        "exe_sha256": sha256(installed),
        "payload_sha256": sha256(source),
        "shortcut": str(link),
        "previous_sha256": previous,
        "upgraded": bool(previous) and previous != sha256(installed),
        "profile_dir_untouched": str(profile_dir()),
    }


# ---------------------------------------------------------------------------
# process inspection (ctypes only: the frozen build ships no psutil)
# ---------------------------------------------------------------------------


def process_image_paths() -> list[tuple[int, Path]]:
    """``(pid, executable path)`` for every process this user can query."""
    if os.name != "nt":  # pragma: no cover - Windows-only product
        return []
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    array = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(array), ctypes.sizeof(array), ctypes.byref(needed)):
        return []
    found: list[tuple[int, Path]] = []
    for index in range(needed.value // ctypes.sizeof(wintypes.DWORD)):
        pid = int(array[index])
        if pid <= 0:
            continue
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                found.append((pid, Path(buffer.value)))
        finally:
            kernel32.CloseHandle(handle)
    return found


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":  # pragma: no cover
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _post_close_to_windows(pids: set[int]) -> None:
    """Ask every top-level window owned by *pids* to close (WM_CLOSE)."""
    if os.name != "nt" or not pids:  # pragma: no cover - Windows-only
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    user32.EnumWindows(callback_type(callback), 0)


def _terminate_pid(pid: int) -> bool:
    if os.name != "nt":  # pragma: no cover
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _inside(path: Path, directory: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - vanished process image
        return False
    return resolved.parent == directory or directory in resolved.parents


def own_process_chain() -> set[int]:
    """This process plus its ancestors.

    A frozen onefile build is a parent bootloader plus a child interpreter, both
    running the same executable. Without this, the uninstaller sees its own
    bootloader inside the install directory, treats it as "the application", and
    kills the process that is waiting to report its exit code - which is exactly
    how a successful uninstall came to exit 1.
    """
    pids = {os.getpid()}
    if os.name != "nt":  # pragma: no cover - Windows-only product
        return pids
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        return pids
    try:
        parents: dict[int, int] = {}
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if kernel32.Process32First(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32Next(ctypes.c_void_p(snapshot), ctypes.byref(entry)):
                    break
        current = os.getpid()
        while current in parents and parents[current] not in (0, current):
            current = parents[current]
            pids.add(current)
    except Exception:  # pragma: no cover - inspection must never break uninstall
        pass
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snapshot))
    return pids


def _same_file(left: Path, right: Path) -> bool:
    try:
        return str(left.resolve()).lower() == str(right.resolve()).lower()
    except OSError:  # pragma: no cover - vanished image
        return False


def stop_app_processes(target_dir: Path, timeout: float = 15.0) -> dict:
    """Close, then if necessary terminate, processes running from *target_dir*.

    Scoping is by executable path, so a ``GisCode.exe`` copy elsewhere - or any
    unrelated Python process - is never touched. The uninstaller's own process
    tree and its own image are excluded, so a frozen onefile build never tries
    to stop the bootloader that is waiting to report its exit code. ``terminate``
    is only reached after a polite ``WM_CLOSE`` and a re-check that the image is
    still inside the install directory.
    """
    target = target_dir.resolve()
    protected = own_process_chain()
    own_image = Path(sys.executable) if getattr(sys, "frozen", False) else None
    found: list[dict] = []
    pids: set[int] = set()
    for pid, image in process_image_paths():
        if pid in protected or not _inside(image, target):
            continue
        if own_image is not None and _same_file(image, own_image):
            continue
        pids.add(pid)
        found.append({"pid": pid, "path": str(image)})
    result = {"found": found, "closed": False, "forced": [], "remaining": []}
    if not pids:
        return result

    _post_close_to_windows(pids)
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            break
        time.sleep(0.1)
    result["closed"] = not any(_pid_alive(pid) for pid in pids)

    for pid in sorted(pids):
        if not _pid_alive(pid):
            continue
        # Re-read the image path: a recycled PID must never be terminated.
        image = dict(process_image_paths()).get(pid)
        if image is None or not _inside(image, target):
            continue
        if _terminate_pid(pid):
            result["forced"].append(pid)
    if result["forced"]:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in result["forced"]):
            time.sleep(0.1)

    result["remaining"] = sorted(pid for pid in pids if _pid_alive(pid))
    return result


# ---------------------------------------------------------------------------
# deletion helpers
# ---------------------------------------------------------------------------


def unlink_with_retry(path: Path, retries: int = 40, delay: float = 0.25) -> bool:
    """Delete *path*, tolerating a short-lived sharing violation.

    A file still held for a moment by an exiting process is not a failure: the
    retries cover that. ``True`` means the path no longer exists.
    """
    for _attempt in range(max(1, retries)):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(delay)
    return not path.exists()


def defer_failure_log(log_path: Path, message: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:  # pragma: no cover - logging must never break uninstall
        pass


def cleanup_script(pid: int, running: Path, target_dir: Path, log_path: Path,
                   registry_key: str) -> str:
    """The detached batch file that finishes an uninstall after we exit.

    It waits (bounded) for *pid* to disappear, deletes the running uninstaller,
    removes the directory, and only then deletes the registry entry - so a
    failure anywhere leaves the entry in place and the uninstall can be retried.
    Both outcomes are appended to *log_path*.
    """
    # Absolute System32 paths: a developer shell can put a Unix ``find`` (or any
    # other namesake) ahead of the Windows tool on PATH, and the helper must not
    # depend on how PATH happens to be ordered when it runs.
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    lines = [
        "@echo off",
        "setlocal",
        f'set "SYS={system32}"',
        f'set "RUNNING={running}"',
        f'set "TARGET={target_dir}"',
        f'set "LOG={log_path}"',
        f"rem wait up to 180 s for the uninstaller (PID {pid}) to exit",
        "for /l %%i in (1,1,180) do (",
        f'  "%SYS%\\tasklist.exe" /nh /fi "PID eq {pid}" 2>nul | "%SYS%\\find.exe" "{pid}" >nul || goto gone',
        '  "%SYS%\\ping.exe" -n 2 127.0.0.1 >nul',
        ")",
        ":gone",
        "rem the file may stay locked for a moment after exit: retry",
        "for /l %%j in (1,1,120) do (",
        '  del /f /q "%RUNNING%" >nul 2>&1',
        '  if not exist "%RUNNING%" goto deleted',
        '  "%SYS%\\ping.exe" -n 2 127.0.0.1 >nul',
        ")",
        ":deleted",
        "rem clear anything else left behind, so an interrupted uninstall recovers",
        'del /f /q "%TARGET%\\*" >nul 2>&1',
        'rmdir /q "%TARGET%" >nul 2>&1',
        'if exist "%TARGET%" (',
        f'  echo %DATE% %TIME% DEFERRED_FAILED dir_still_exists >>"%LOG%"',
        "  exit /b 4",
        ")",
        f'"%SYS%\\reg.exe" delete "{registry_key}" /f >>"%LOG%" 2>&1',
        'if errorlevel 1 (',
        f'  echo %DATE% %TIME% DEFERRED_FAILED registry_delete_failed >>"%LOG%"',
        "  exit /b 5",
        ")",
        f'echo %DATE% %TIME% DEFERRED_OK install_dir_removed registry_removed >>"%LOG%"',
        # ``(goto)`` makes cmd stop reading this script before it removes
        # itself; without it cmd reports "The batch file cannot be found" and
        # exits non-zero for a cleanup that actually succeeded.
        '(goto) 2>nul & del /f /q "%~f0"',
        "exit /b 0",
    ]
    return "\r\n".join(lines) + "\r\n"


def spawn_deferred_cleanup(pid: int, running: Path, target_dir: Path,
                           log_path: Path) -> Path | None:
    """Write and start the detached cleanup helper. ``None`` if it cannot start."""
    script = Path(tempfile.gettempdir()) / f"giscode-uninstall-{pid}.cmd"
    try:
        script.write_text(
            cleanup_script(pid, running, target_dir, log_path, f"HKCU\\{UNINSTALL_KEY}"),
            encoding="utf-8", newline="",
        )
        # ``start /b`` is required: a DETACHED_PROCESS child dies with the
        # frozen parent's job object, which silently broke deferred cleanup.
        # ``start`` breaks away from that job, so the helper outlives us.
        subprocess.Popen(
            ["cmd", "/c", "start", "", "/b", "cmd", "/c", str(script)],
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
            cwd=tempfile.gettempdir(),
        )
    except Exception:
        return None
    return script


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def uninstall(directory: Path | None = None, *, app_timeout: float = 15.0,
              delete_retries: int = 40, delete_delay: float = 0.25,
              allow_defer: bool = True) -> dict:
    """Remove the program files, shortcut and registry entry.

    Returns a result whose ``cleanup_complete`` is only ``True`` when nothing
    remains synchronously. ``deferred_self_delete`` means the only remaining
    file is this running uninstaller and a helper will remove it and the
    registry entry; anything else left behind is reported as incomplete.
    """
    target_dir = Path(directory) if directory else default_install_dir()
    target = target_dir.resolve()
    profile = profile_dir().resolve()
    if (target == profile or target in profile.parents or profile in target.parents):
        raise SystemExit(
            f"Refusing to uninstall: the user-data folder '{profile}' overlaps the "
            f"install folder '{target}'. Move GISCODE_HOME (or pass --dir for the "
            f"install folder) and try again. Nothing was deleted."
        )

    removed: list[str] = []
    log_path = cleanup_log_path()
    defer_failure_log(log_path, f"UNINSTALL start target={target} pid={os.getpid()}")

    running: Path | None = None
    if getattr(sys, "frozen", False):
        running = Path(sys.executable).resolve()
    self_inside = running is not None and running.parent == target

    app = stop_app_processes(target, app_timeout)
    if app["remaining"]:
        return {
            "removed": removed,
            "app_processes": app,
            "deferred_self_delete": False,
            "cleanup_script": "",
            "install_dir_exists": target.is_dir(),
            "shortcut_exists": start_menu_shortcut().is_file(),
            "registry_exists": uninstall_entry_exists(),
            "profile_dir": str(profile),
            "profile_dir_exists": profile.exists(),
            "leftover": [],
            "cleanup_complete": False,
            "cleanup_log": str(log_path),
            "error": "a GisCode process from the install folder could not be stopped",
        }

    exe = target / EXE_NAME
    if exe.exists():
        if unlink_with_retry(exe, delete_retries, delete_delay):
            removed.append(str(exe))

    link = start_menu_shortcut()
    if link.exists() and unlink_with_retry(link, delete_retries, delete_delay):
        removed.append(str(link))

    for entry in sorted(target.iterdir()) if target.is_dir() else []:
        if entry == running or not entry.is_file():
            continue
        if unlink_with_retry(entry, delete_retries, delete_delay):
            removed.append(str(entry))

    deferred = False
    script_path = ""
    leftover: list[Path] = []

    if self_inside:
        leftover = sorted(p for p in target.iterdir()) if target.is_dir() else []
        if leftover == [running] and allow_defer:
            script = spawn_deferred_cleanup(os.getpid(), running, target, log_path)
            if script is not None:
                deferred = True
                script_path = str(script)
                defer_failure_log(log_path, f"DEFERRED_STARTED pid={os.getpid()} script={script}")
    else:
        if target.is_dir():
            try:
                target.rmdir()
                removed.append(str(target))
            except OSError:
                pass
        if not target.is_dir():
            remove_uninstall_entry()

    if not deferred and target.is_dir():
        leftover = sorted(target.iterdir())
        if not leftover:
            try:
                target.rmdir()
                removed.append(str(target))
            except OSError:
                leftover = sorted(target.iterdir())
        if not target.is_dir():
            remove_uninstall_entry()

    result = {
        "removed": removed,
        "app_processes": app,
        "deferred_self_delete": deferred,
        "cleanup_script": script_path,
        "install_dir_exists": target.is_dir(),
        "shortcut_exists": link.is_file(),
        "registry_exists": uninstall_entry_exists(),
        "profile_dir": str(profile),
        "profile_dir_exists": profile.exists(),
        "leftover": [p.name for p in leftover],
        "cleanup_complete": (not target.is_dir()) and not link.is_file()
                            and not uninstall_entry_exists(),
        "cleanup_log": str(log_path),
    }
    defer_failure_log(
        log_path,
        "UNINSTALL result complete={0} deferred={1} leftover={2}".format(
            result["cleanup_complete"], deferred, result["leftover"]),
    )
    return result


def launch(exe: Path) -> int:
    return subprocess.Popen([str(exe)]).pid


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"{DISPLAY_NAME} Setup {VERSION}")
    root.geometry("560x320")
    root.resizable(False, False)

    ttk.Label(root, text=f"{DISPLAY_NAME} {VERSION}", font=("Segoe UI", 14, "bold")).pack(
        anchor="w", padx=16, pady=(16, 4))
    ttk.Label(root, text=f"Installs for the current user. No administrator rights needed.\n"
                         f"Your chats, projects and settings in %USERPROFILE%\\.giscode are never "
                         f"modified.").pack(anchor="w", padx=16)
    directory = tk.StringVar(value=str(default_install_dir()))
    row = ttk.Frame(root)
    row.pack(fill="x", padx=16, pady=12)
    ttk.Label(row, text="Install folder").pack(side="left")
    ttk.Entry(row, textvariable=directory, width=48).pack(side="left", padx=8)
    ttk.Button(row, text="Browse…", command=lambda: directory.set(
        filedialog.askdirectory(initialdir=directory.get()) or directory.get())).pack(side="left")
    log = tk.Text(root, height=6, wrap="word")
    log.pack(fill="both", expand=True, padx=16)

    def report(text: str) -> None:
        log.insert("end", text + "\n")
        log.see("end")
        root.update_idletasks()

    def do_install() -> None:
        try:
            result = install(Path(directory.get()))
        except Exception as exc:  # surfaced, never hidden
            messagebox.showerror("Install failed", str(exc))
            return
        report(f"Installed to {result['install_dir']}")
        report(f"SHA-256: {result['exe_sha256']}")
        report(f"Start Menu shortcut: {result['shortcut']}")
        messagebox.showinfo("Installed", f"{DISPLAY_NAME} {VERSION} is installed.")

    def do_uninstall() -> None:
        result = uninstall(Path(directory.get()))
        report(f"Removed {len(result['removed'])} item(s); user data kept at {result['profile_dir']}")
        if result["cleanup_complete"]:
            messagebox.showinfo("Uninstalled", "Program files removed. Your chats and settings were kept.")
        elif result["deferred_self_delete"]:
            messagebox.showinfo(
                "Uninstall finishing",
                "Program files removed. The last file is deleted automatically in a few seconds.")
        else:
            messagebox.showerror(
                "Uninstall incomplete",
                "Some items could not be removed:\n"
                + ("\n".join(result["leftover"]) or "a GisCode process is still running")
                + f"\n\nDetails: {result['cleanup_log']}")

    buttons = ttk.Frame(root)
    buttons.pack(fill="x", padx=16, pady=(0, 16))
    ttk.Button(buttons, text="Install", command=do_install).pack(side="right")
    ttk.Button(buttons, text="Uninstall", command=do_uninstall).pack(side="right", padx=8)
    ttk.Button(buttons, text="Close", command=root.destroy).pack(side="right")
    root.mainloop()
    return 0


def main(argv: list[str]) -> int:
    args = [a.lower() for a in argv[1:]]
    if "--version" in args:
        print(f"{DISPLAY_NAME} Setup {VERSION}")
        return 0
    directory = None
    if "--dir" in args:
        directory = Path(argv[argv.index("--dir") + 1])
    if "--install" in args:
        result = install(directory)
        print(f"INSTALLED {result['install_dir']}")
        print(f"EXE_SHA256 {result['exe_sha256']}")
        print(f"SHORTCUT {result['shortcut']}")
        print(f"UPGRADED {result['upgraded']}")
        return 0
    if "--uninstall" in args:
        result = uninstall(directory)
        print(f"REMOVED {len(result['removed'])}")
        print(f"INSTALL_DIR_EXISTS {result['install_dir_exists']}")
        print(f"SHORTCUT_EXISTS {result['shortcut_exists']}")
        print(f"REGISTRY_EXISTS {result['registry_exists']}")
        print(f"PROFILE_EXISTS {result['profile_dir_exists']}")
        print(f"APP_PROCESSES_STOPPED {len(result['app_processes']['found'])}")
        if result["deferred_self_delete"]:
            print(f"DEFERRED_CLEANUP_STARTED {result['cleanup_script']}")
        for item in result["leftover"]:
            print(f"LEFTOVER {item}")
        print(f"CLEANUP_LOG {result['cleanup_log']}")
        if result["cleanup_complete"]:
            print("CLEANUP COMPLETE")
            return 0
        if result["deferred_self_delete"]:
            print("CLEANUP DEFERRED")
            return 0
        print("CLEANUP INCOMPLETE")
        return 2
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
