"""Safety boundary for acceptance scenarios that modify or delete files.

The acceptance executable is allowed to run ``Forget Session``, which removes a
browser profile directory. That power must never reach a real user profile or an
arbitrary path, so it is protected by *possession* rather than by a flag being
set: the runner must create the home for this run and write a random token into
a marker file inside it. A real profile does not contain that marker, so it
cannot be passed off as disposable by pointing ``GISCODE_HOME`` at it.

Every entry point fails closed and validates canonical paths, reparse points
(symlinks and junctions) and containment before returning anything.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

ENV_HOME = "GISCODE_HOME"
ENV_DISPOSABLE = "GISCODE_ACCEPTANCE_DISPOSABLE_HOME"
ENV_TOKEN = "GISCODE_ACCEPTANCE_DISPOSABLE_TOKEN"
ENV_ALLOW_DESTRUCTIVE = "GISCODE_ACCEPTANCE_ALLOW_DESTRUCTIVE"

MARKER_NAME = ".giscode-acceptance-disposable"
MIN_TOKEN_LENGTH = 16
_TRUTHY = {"1", "true", "yes", "on"}


class DisposableHomeError(RuntimeError):
    """The home is not proven disposable; nothing may be modified."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _is_reparse(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _is_reparse_path(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return _is_reparse(info)


def _canonical(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.normcase(os.path.realpath(os.path.abspath(str(path)))))


def _inside(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    canonical_child = _canonical(child)
    canonical_parent = _canonical(parent)
    if canonical_child == canonical_parent:
        return False
    try:
        canonical_child.relative_to(canonical_parent)
    except ValueError:
        return False
    return True


def _has_reparse_component(path: Path) -> bool:
    """True when ``path`` or any existing ancestor is a symlink or junction."""

    current = Path(path)
    while True:
        if _is_reparse_path(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _protected_profile_roots(environ: dict) -> list[Path]:
    """The real GisCode profiles and the user's own home directory.

    The temporary directory lives *inside* the Windows user profile, so being
    under ``C:\\Users\\<name>`` cannot by itself be disqualifying. What must
    never be a disposable home is ``<user>\\.giscode`` (the real profile) or the
    user's home directory itself.
    """

    roots = [Path.home() / ".giscode"]
    profile = (environ.get("USERPROFILE") or "").strip()
    if profile:
        roots.append(Path(profile) / ".giscode")
    return roots


def _forbidden_exact_paths(environ: dict) -> list[Path]:
    roots = [Path.home()]
    profile = (environ.get("USERPROFILE") or "").strip()
    if profile:
        roots.append(Path(profile))
    return roots


def verify_disposable_home(
    *,
    environ: dict | None = None,
    temp_root: str | os.PathLike[str] | None = None,
    require_destructive: bool = False,
) -> Path:
    """Return the verified disposable home, or raise :class:`DisposableHomeError`."""

    env = os.environ if environ is None else environ
    home_raw = (env.get(ENV_HOME) or "").strip()
    if not home_raw:
        raise DisposableHomeError("GISCODE_HOME is not set")

    declared_raw = (env.get(ENV_DISPOSABLE) or "").strip()
    if not declared_raw:
        raise DisposableHomeError(f"{ENV_DISPOSABLE} is not set")

    home = Path(home_raw).expanduser()
    if _canonical(home) != _canonical(declared_raw):
        raise DisposableHomeError(
            f"{ENV_DISPOSABLE} does not match GISCODE_HOME"
        )
    if not home.is_dir():
        raise DisposableHomeError("the disposable home is not an existing directory")
    if _is_reparse_path(home):
        raise DisposableHomeError("the disposable home is a reparse point")
    if _has_reparse_component(home):
        raise DisposableHomeError("the disposable home path contains a reparse point")

    root = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
    if _is_reparse_path(root) or not _inside(home, root):
        raise DisposableHomeError("the disposable home is outside the temporary root")

    for profile_root in _protected_profile_roots(env):
        if _canonical(home) == _canonical(profile_root) or _inside(home, profile_root):
            raise DisposableHomeError("the disposable home is the real user profile")
    for forbidden in _forbidden_exact_paths(env):
        if _canonical(home) == _canonical(forbidden):
            raise DisposableHomeError("the disposable home is the user's home directory")
    if (home / ".git").exists():
        raise DisposableHomeError("the disposable home looks like a project checkout")

    token = (env.get(ENV_TOKEN) or "").strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise DisposableHomeError(
            f"{ENV_TOKEN} is missing or shorter than {MIN_TOKEN_LENGTH} characters"
        )
    marker = home / MARKER_NAME
    if not marker.is_file() or _is_reparse_path(marker):
        raise DisposableHomeError("the disposable home has no marker file")
    try:
        marker_text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        raise DisposableHomeError("the disposable home marker cannot be read") from None
    if marker_text != token:
        raise DisposableHomeError("the disposable home marker does not match the token")

    if require_destructive and not _truthy(env.get(ENV_ALLOW_DESTRUCTIVE)):
        raise DisposableHomeError(
            f"destructive scenarios need {ENV_ALLOW_DESTRUCTIVE}=1"
        )
    return home.resolve()


def verify_destructive_target(target: str | os.PathLike[str], home: Path) -> Path:
    """Return ``target`` only when it is a deletable directory inside ``home``."""

    candidate = Path(target)
    if _canonical(candidate) == _canonical(home):
        raise DisposableHomeError("refusing to delete the disposable home itself")
    if not _inside(candidate, home):
        raise DisposableHomeError("the delete target is outside the disposable home")
    if _is_reparse_path(candidate):
        raise DisposableHomeError("the delete target is a reparse point")
    if _has_reparse_component(candidate):
        raise DisposableHomeError("the delete target path contains a reparse point")
    return candidate


def remove_tree(target: str | os.PathLike[str], home: Path) -> None:
    """Delete ``target`` after containment checks; never follows a link out."""

    verified = verify_destructive_target(target, home)
    shutil.rmtree(verified)


def write_disposable_marker(home: Path, token: str) -> Path:
    """Create the possession marker a runner must write before a scenario runs."""

    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    marker = home / MARKER_NAME
    marker.write_text(str(token), encoding="utf-8")
    return marker
