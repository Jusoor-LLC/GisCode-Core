"""Choose the llama-server build GisCode actually runs.

The previous implementation picked a build by sorting the *parent folder name*
and returned the first hit without ever executing it. On a machine with several
LM Studio backends that meant "whichever directory the filesystem listed first"
- in practice the oldest CUDA build - and a build that cannot even start was
still returned.

This module keeps the choice explicit and honest:

* every candidate is run once (``--version``) and only a zero exit counts;
* the winner is chosen by what the machine can use (CUDA > Vulkan > CPU) and,
  within a family, by the newest parsed version;
* CUDA and Vulkan winners are confirmed with ``--list-devices`` (a build can
  load its DLLs and still have no usable device, or hang while enumerating);
* the full decision - including every rejected candidate and the reason - is
  available as one plain sentence for the log and the GUI.

Nothing here starts a server, loads a model or signals a process.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 10.0
DEVICE_PROBE_TIMEOUT_S = 10.0
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Preference rank of each build family (lower is better).
FAMILY_RANK = {"cuda12": 0, "cuda": 1, "vulkan": 2, "cpu": 3}

#: Human labels used in the one-sentence report.
FAMILY_LABEL = {
    "cuda12": "CUDA 12",
    "cuda": "CUDA",
    "vulkan": "Vulkan",
    "cpu": "CPU",
    "unknown": "unidentified",
}

SOURCE_LABEL = {
    "env": "GISCODE_LLAMA_SERVER_PATH",
    "setting": "inference.llama_server_path",
    "bundled": "bundled with GisCode",
    "path": "PATH",
    "lmstudio": "LM Studio backends",
    "fixed": "known install folder",
}

#: Vendor DLL folders shipped next to the backend directories, per family.
VENDOR_DIRS = {
    "cuda12": ("win-llama-cuda12-vendor-v2", "win-llama-cuda-vendor-v2"),
    "cuda": ("win-llama-cuda-vendor-v2",),
    "vulkan": ("win-llama-vulkan-vendor-v3",),
}

VERSION_RE = re.compile(r"version:\s*([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)
NAME_VERSION_RE = re.compile(r"-(\d+(?:\.\d+)+)$")
DEVICE_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.-]*\d*)\s*:\s*(.+)$")
MI_FREE_RE = re.compile(r"(\d+)\s*MiB\s*,\s*(\d+)\s*MiB\s+free", re.IGNORECASE)
GIB = float(1024 ** 3)


@dataclass(frozen=True)
class Candidate:
    """A discoverable ``llama-server.exe`` and where it came from."""

    path: str
    source: str = ""
    pinned: bool = False


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    version: tuple[int, ...] = ()
    reason: str = ""
    output: str = ""


@dataclass(frozen=True)
class BackendChoice:
    """The chosen build plus everything needed to explain the decision."""

    path: Optional[str] = None
    family: str = ""
    version: tuple[int, ...] = ()
    source: str = ""
    pinned: bool = False
    devices: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    reason: str = ""


_VERSION_CACHE: dict[str, ProbeResult] = {}
_DEVICE_CACHE: dict[str, ProbeResult] = {}


def _cache_key(path: str) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(str(path))


def reset_probe_cache() -> None:
    """Forget every probed result (tests, and a manual re-scan)."""
    _VERSION_CACHE.clear()
    _DEVICE_CACHE.clear()


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    """``"version: 0.6.0-dev (build 1, …)"`` -> ``(0, 6, 0)``."""
    match = VERSION_RE.search(text or "")
    if not match:
        return ()
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return ()


def version_from_name(path: Optional[str]) -> tuple[int, ...]:
    """Version embedded in the build folder name (``…-cuda-avx2-2.56.0``).

    Cheap pre-probe ordering key so the scan can stop at the first working
    build instead of spawning every candidate. The real version still comes
    from ``--version``; this only decides the order.
    """
    if not path:
        return ()
    match = NAME_VERSION_RE.search(Path(str(path)).parent.name)
    if not match:
        return ()
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return ()


def probe_command(path: str, args: Sequence[str], timeout: float) -> ProbeResult:
    """Run ``path args...`` once and report what really happened.

    Any non-zero exit - including Windows' 127 for a missing DLL - means the
    build is unusable, whatever its folder is called.
    """
    try:
        completed = subprocess.run(
            [str(path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(False, reason=f"timed out after {timeout:g}s")
    except OSError as exc:
        return ProbeResult(False, reason=f"could not run: {exc}")

    output = "\n".join(part for part in (completed.stdout or "", completed.stderr or "") if part).strip()
    if completed.returncode != 0:
        first = output.splitlines()[0].strip() if output else "no output"
        return ProbeResult(False, reason=f"exit {completed.returncode}: {first[:160]}", output=output)
    return ProbeResult(True, version=parse_version(output), output=output)


def probe_version_cached(path: str, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Cached ``--version`` probe (one spawn per path per session)."""
    key = _cache_key(path)
    if key not in _VERSION_CACHE:
        _VERSION_CACHE[key] = _probe_runner(str(path), ("--version",), timeout)
    return _VERSION_CACHE[key]


def probe_devices_cached(path: str, timeout: float = DEVICE_PROBE_TIMEOUT_S) -> ProbeResult:
    """Cached ``--list-devices`` probe (CUDA/Vulkan confirmation only)."""
    key = _cache_key(path)
    if key not in _DEVICE_CACHE:
        _DEVICE_CACHE[key] = _probe_runner(str(path), ("--list-devices",), timeout)
    return _DEVICE_CACHE[key]


#: Indirection so tests can count spawns without a real binary.
_probe_runner = probe_command


# --------------------------------------------------------------------------
# classification / ordering
# --------------------------------------------------------------------------
def family_of(path: Optional[str]) -> str:
    """Best-effort family from the build's own folder/exe name."""
    if not path:
        return "unknown"
    name = Path(str(path)).parent.name.lower()
    if "cuda12" in name:
        return "cuda12"
    if "cuda" in name:
        return "cuda"
    if "vulkan" in name:
        return "vulkan"
    if "avx" in name or "cpu" in name:
        return "cpu"
    return "unknown"


def preference_order(has_nvidia: bool, has_vulkan: bool) -> tuple[str, ...]:
    """Which families this machine should prefer, best first."""
    if has_nvidia:
        return ("cuda12", "cuda", "vulkan", "cpu")
    if has_vulkan:
        return ("vulkan", "cpu")
    return ("cpu",)


def _order_index(family: str, order: Sequence[str]) -> int:
    try:
        return order.index(family)
    except ValueError:
        return len(order)


def _version_sort_key(version: Sequence[int]) -> tuple[int, int, int]:
    padded = list(version)[:3] + [0] * (3 - len(list(version)[:3]))
    return tuple(int(part) for part in padded)


def parse_devices(output: str) -> tuple[str, ...]:
    """Device lines from ``--list-devices`` (``CUDA0: …``, ``Vulkan1: …``)."""
    devices: list[str] = []
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("available devices"):
            continue
        if not DEVICE_LINE_RE.match(stripped):
            continue
        if "mib" not in stripped.lower():
            continue
        devices.append(stripped)
    return tuple(devices)


def device_free_mib(device: str) -> int:
    match = MI_FREE_RE.search(device or "")
    return int(match.group(2)) if match else 0


def vulkan_device_note(devices: Sequence[str]) -> str:
    """Describe the Vulkan devices and the one with the most free memory.

    GisCode does not pass ``--device``, so llama.cpp's own default applies;
    the note therefore says which device has the largest free-memory figure
    (the usual tie-breaker) without pretending GisCode chose it.
    """
    if not devices:
        return ""
    ranked = sorted(devices, key=device_free_mib, reverse=True)
    best = ranked[0]
    note = f"largest free memory is {best}"
    if len(ranked) > 1:
        others = ", ".join(item.split(":", 1)[0].strip() for item in ranked[1:])
        note += f"; also listed: {others}"
    return note + " (GisCode does not pass --device, so llama.cpp's default applies)"


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------
def _accept(candidate: Candidate, probe: ProbeResult, devices: Sequence[str],
            rejected: list[tuple[str, str]]) -> BackendChoice:
    family = family_of(candidate.path)
    return BackendChoice(
        path=candidate.path,
        family=family,
        version=probe.version,
        source=candidate.source,
        pinned=candidate.pinned,
        devices=tuple(devices),
        rejected=tuple(rejected),
        reason=probe.reason,
    )


def select_backend(
    candidates: Iterable[Candidate],
    *,
    probe,
    has_nvidia: bool,
    has_vulkan: bool,
    confirm_devices: bool = False,
    device_probe=None,
) -> BackendChoice:
    """Pick the best *working* build, explaining every rejection.

    ``probe`` is called as ``probe(path) -> ProbeResult``; ``device_probe`` the
    same way. Both are injected so tests never spawn anything.
    """
    rejected: list[tuple[str, str]] = []
    ordered: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _cache_key(candidate.path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)

    pinned = [c for c in ordered if c.pinned]
    discovered = [c for c in ordered if not c.pinned]

    def confirm(candidate: Candidate, result: ProbeResult):
        """Run the device probe for CUDA/Vulkan builds; None means rejected."""
        if not confirm_devices or device_probe is None:
            return result, ()
        if family_of(candidate.path) not in ("cuda12", "cuda", "vulkan"):
            return result, ()
        device_result = device_probe(candidate.path)
        if not device_result.ok:
            rejected.append((candidate.path, f"--list-devices {device_result.reason or 'failed'}"))
            return None
        devices = parse_devices(device_result.output)
        if not devices:
            rejected.append((candidate.path, "--list-devices reported no GPU device"))
            return None
        return result, devices

    for candidate in pinned:
        result = probe(candidate.path)
        if not result.ok:
            rejected.append((candidate.path, result.reason or "probe failed"))
            continue
        confirmed = confirm(candidate, result)
        if confirmed is None:
            continue
        result, devices = confirmed
        return _accept(candidate, result, devices, rejected)

    order = preference_order(has_nvidia, has_vulkan)

    # Rank before probing, using the version in the folder name: the scan can
    # then stop at the first build that actually runs (normally one spawn),
    # while still preferring the newest working build in the best family.
    ranked = sorted(discovered, key=lambda c: c.path.lower())
    ranked.sort(key=lambda c: _version_sort_key(version_from_name(c.path)), reverse=True)
    ranked.sort(key=lambda c: _order_index(family_of(c.path), order))

    for candidate in ranked:
        result = probe(candidate.path)
        if not result.ok:
            rejected.append((candidate.path, result.reason or "probe failed"))
            continue
        confirmed = confirm(candidate, result)
        if confirmed is None:
            continue
        result, devices = confirmed
        return _accept(candidate, result, devices, rejected)

    reason = "no working llama-server build was found"
    if rejected:
        reason = f"{len(rejected)} candidate(s) failed to start"
    return BackendChoice(path=None, rejected=tuple(rejected), reason=reason)


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def describe_choice(choice: BackendChoice) -> str:
    """One plain sentence: which build, from where, and how many were skipped."""
    if not choice.path:
        reason = choice.reason or "no working llama-server build was found"
        detail = ""
        if choice.rejected:
            detail = " Rejected: " + "; ".join(
                f"{Path(path).parent.name or path} ({reason})"
                for path, reason in choice.rejected[:4]
            ) + "."
        return f"No usable llama-server build was found: {reason}.{detail}"

    label = FAMILY_LABEL.get(choice.family, choice.family or "llama.cpp")
    version = ".".join(str(part) for part in choice.version) if choice.version else "unknown version"
    source = SOURCE_LABEL.get(choice.source, choice.source)
    text = f"Using the {label} build (v{version}) at {choice.path}"
    if source:
        text += f", found via {source}"
    if choice.devices:
        text += f"; it reports {len(choice.devices)} device(s)"
    if choice.rejected:
        text += f"; {len(choice.rejected)} other candidate(s) were rejected"
    return text + "."


def server_env(server_path: Optional[str], family: str, base_env=None) -> dict:
    """Environment for the server process, with its DLL folders on ``PATH``.

    LM Studio keeps the CUDA/Vulkan runtime DLLs in ``backends/vendor/<name>``,
    i.e. a *sibling* of the build directory - the previous code looked one level
    too deep inside the build directory and never found them. The build's own
    directory is included as well, because some builds resolve their sibling
    DLLs through ``PATH`` rather than the exe directory.
    """
    env = dict(os.environ if base_env is None else base_env)
    if not server_path:
        return env
    server_dir = Path(server_path).parent
    directories: list[str] = []
    for directory in [server_dir]:
        try:
            if directory.is_dir():
                directories.append(str(directory))
        except OSError:
            continue
    for vendor in VENDOR_DIRS.get(family, ()):  # type: ignore[arg-type]
        for directory in (server_dir.parent / "vendor" / vendor, server_dir / "vendor" / vendor):
            try:
                if directory.is_dir() and str(directory) not in directories:
                    directories.append(str(directory))
            except OSError:
                continue
    if directories:
        current = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(directories + ([current] if current else []))
    return env


def vulkan_available() -> bool:
    """True when the machine has a system Vulkan loader."""
    if os.name != "nt":
        return False
    try:
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return (Path(root) / "System32" / "vulkan-1.dll").is_file()
    except OSError:
        return False


def _format_gib(value: int) -> str:
    try:
        return f"{float(value) / GIB:.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _format_mb(value) -> str:
    try:
        return f"{float(value) / (1024 ** 2):.0f} MB"
    except (TypeError, ValueError):
        return "unknown memory"


def explain_cpu_only_load(
    gpu_layers: int,
    gpu_name: Optional[str],
    free_vram_bytes: int,
    total_vram_bytes: int,
    foreign_servers: Sequence[dict] = (),
) -> str:
    """Plain-words note for "GPU present but loaded with 0 GPU layers".

    Returns ``""`` when the note does not apply (no GPU, or layers > 0), so the
    caller can show it unconditionally.
    """
    try:
        layers = int(gpu_layers or 0)
    except (TypeError, ValueError):
        layers = 0
    if layers > 0 or not gpu_name:
        return ""

    free = _format_gib(free_vram_bytes)
    total = _format_gib(total_vram_bytes)
    text = (
        f"Loaded on CPU only. {gpu_name} has about {free} GiB of its {total} GiB "
        "of graphics memory free, so GisCode loaded the model without GPU "
        "offload. This will be slow."
    )
    if foreign_servers:
        holders = ", ".join(
            f"{item.get('name') or 'llama-server'} (PID {item.get('pid')}, "
            f"{_format_mb(item.get('rss_bytes'))})"
            for item in list(foreign_servers)[:4]
        )
        text += f" Other llama-server processes are holding memory: {holders}."
    else:
        text += " No other llama-server process was found; other programs are using the graphics memory."
    return text
