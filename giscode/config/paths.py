"""Where GisCode keeps its per-user profile files.

Single source of truth so that an isolated run can redirect every profile
write with one environment variable (``GISCODE_HOME``) instead of trying to
fake the Windows home folder.

Why ``USERPROFILE``/``HOME`` do not work here:

* ``os.environ.setdefault("USERPROFILE", ...)`` never overrides a variable
  that Windows always defines, so the real profile keeps being written.
* Several modules snapshot ``Path.home()`` into a module level constant at
  *import* time (``gisai/history.py``, ``gisai/learning/store.py``,
  ``projects/store.py``, ...). Flipping the variable after ``giscode`` has
  been imported is already too late.

``GISCODE_HOME`` is therefore read on every call. Set it before the process
starts (preferred) or at least before the first profile file is touched, and
every ``~/.giscode`` file -- ``config.json``, ``gisai_history.db``,
``learning.db``, ``knowledge.db``, ``chats.db``, snapshots, logs -- lands
inside the isolated profile.

Things deliberately *not* redirected: ``~/.lmstudio`` (model weights stay
where the user installed them) and user-typed paths that are expanded with
``os.path.expanduser`` for display.

``GISCODE_TEST_MODE`` is the separate, explicit switch for that second case.
An isolated profile still leaves ``~/.lmstudio`` visible, so a test run on a
developer's machine would discover - and, at startup, try to load - their
personal models. Setting ``GISCODE_TEST_MODE=1`` makes model *discovery* use
only the configured models folder and stops the startup autoload. It changes
nothing about how the application behaves when it is unset.
"""
from __future__ import annotations

import os
from pathlib import Path

GISCODE_HOME_ENV = "GISCODE_HOME"
GISCODE_TEST_MODE_ENV = "GISCODE_TEST_MODE"
_TRUTHY = {"1", "true", "yes", "on"}


def test_mode() -> bool:
    """True only when the process was explicitly started as an isolated test.

    Read on every call (like ``GISCODE_HOME``) so a test can switch it off and
    assert the normal behaviour is unchanged.
    """
    return os.environ.get(GISCODE_TEST_MODE_ENV, "").strip().lower() in _TRUTHY


def giscode_home() -> Path:
    """Directory holding profile files (the equivalent of ``~/.giscode``)."""
    override = os.environ.get(GISCODE_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".giscode"


def profile_root() -> Path:
    """Directory that *contains* the profile.

    Safety checks such as ``KnowledgeStore``'s "must live under home" rule
    compare against this, so an isolated profile is as valid as the real one.
    """
    return giscode_home().parent


def set_giscode_home(path: str | os.PathLike[str]) -> Path:
    """Point every profile write at ``path`` for the rest of this process."""
    resolved = Path(path).expanduser().resolve()
    os.environ[GISCODE_HOME_ENV] = str(resolved)
    return resolved


def giscode_home_configured() -> bool:
    """True when profile writes are redirected away from ``~/.giscode``."""
    return bool(os.environ.get(GISCODE_HOME_ENV, "").strip())


def public_models_dir(*, os_name: str | None = None) -> Path | None:
    """The machine-wide LM Studio models folder, or ``None`` if it has none.

    This used to be the literal ``"C:/Users/Public/lmstudio/models"`` in two
    modules, which is a Windows-only assumption sitting in shared discovery
    code. Resolved here instead, from ``%PUBLIC%`` when the platform defines it,
    so a relocated or redirected Windows profile still points at the right
    place and no other operating system is given a Windows path.

    ``None`` means "this platform has no machine-wide models folder": on Linux
    and macOS LM Studio keeps its weights in the user's own profile, which
    discovery already covers.
    """
    if (os_name or os.name) != "nt":
        return None
    base = os.environ.get("PUBLIC", "").strip() or r"C:\Users\Public"
    return Path(base) / "lmstudio" / "models"
