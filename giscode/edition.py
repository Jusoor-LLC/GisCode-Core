"""Which edition of GisCode is running, and what that edition contains.

GisCode ships in two editions:

* **Core** - the free, public edition. Chat, projects, attachment analysis,
  the Model Manager (so a user can browse, choose and switch models), essential
  settings and the GisCode AdSpace panel.
* **Pro** - everything in Core plus the developer and release tools: terminal,
  iOS/xtool, GisLogo, Developer & Release, Device Preview, Generate Video,
  Project Doctor, hosted-writer and self-improvement settings. Pro is ad-free.

One registry decides what each edition contains, so a surface cannot appear in
Core because a builder forgot to check. The selection is a launch-time
configuration, not a licence check: this build contains no activation or
entitlement mechanism, and inventing one was explicitly out of scope.

Resolve it with the ``GISCODE_EDITION`` environment variable::

    GISCODE_EDITION=pro  giscode      # Pro surfaces
    giscode                           # Core (the default)

The value is read on every call, so a test can switch editions in-process and
a package can set it once for its process.
"""
from __future__ import annotations

import importlib.util
import os

CORE = "core"
PRO = "pro"

#: Environment variable that selects the edition.
EDITION_ENV = "GISCODE_EDITION"

#: Surfaces both editions have. Core is a complete product for the work its
#: projects, attachment analysis, the Model Manager and essential settings are
#: in Pro as well, and a feature that lives here can never be taken away from
#: Core by editing one of the lists below.
SHARED_FEATURES: frozenset[str] = frozenset(
    {
        "chat",
        "projects",
        "project_workflows",
        "attachment_analysis",
        "model_manager",
        "settings",
    }
)

#: Core-only surfaces.
CORE_ONLY_FEATURES: frozenset[str] = frozenset({"adspace"})

#: Pro-only surfaces. Adding a developer tool means adding it here, which is
#: what keeps it out of Core.
PRO_ONLY_FEATURES: frozenset[str] = frozenset(
    {
        "terminal",
        "history_tab",
        "status_tab",
        "ios_xtool",
        "gislogo",
        "developer_release",
        "device_preview",
        "generate_video",
        "project_doctor",
        "code_task",
        "analyze_tab",
        "hosted_writer",
        "self_improvement",
        "custom_logo",
        "agent_registry",
        "agent_runtime",
    }
)

#: Surfaces whose code lives in a module that the public Core export does not
#: ship. A Pro build in a Core-only tree must degrade instead of crashing: the
#: tab is skipped, and the header keeps working. The list is the boundary the
#: release exporter enforces and the boundary the tests assert.
PRO_IMPLEMENTATION: dict[str, tuple[str, ...]] = {
    "developer_release": (
        "giscode.developer_release",
        "giscode.gui.developer_release_workspace",
    ),
    "gislogo": ("giscode.gui.gislogo_view",),
    "ios_xtool": ("giscode.gui.ios_xtool_view",),
    # The tab builders below used to live inside the shared main window, so a
    # Core export shipped Pro UI code and only a callback kept it off screen.
    # They now live in giscode.gui.pro_tabs, which the Core export drops: with
    # the module absent the registry reports the surface unavailable, the tab is
    # not listed, and the builder cannot be reached.
    "terminal": ("giscode.gui.pro_tabs",),
    "history_tab": ("giscode.gui.pro_tabs",),
    "status_tab": ("giscode.gui.pro_tabs",),
    "device_preview": ("giscode.gui.pro_tabs",),
    "generate_video": ("giscode.gui.pro_tabs",),
    "project_doctor": ("giscode.gui.pro_tabs",),
    "code_task": ("giscode.gui.pro_tabs",),
    "analyze_tab": ("giscode.gui.pro_tabs",),
    # The Agent Registry pane and the service behind it. The service is the
    # production entry point (Registry -> Gateway -> authorized capability) and
    # is where the Pro edition gate lives; the pane only asks it for a snapshot
    # or an authorization decision.
    "agent_registry": (
        "giscode.agent_registry.service",
        "giscode.gui.agent_registry_view",
        "giscode.gui.pro_tabs",
    ),
    # Managed runtimes are Pro-owned: the manager, resource accounting,
    # process ownership and the GisMac adapter live in this package. Core
    # exports neither the package nor its pane.
    "agent_runtime": (
        "giscode.agent_runtime.manager",
        "giscode.agent_runtime.gismac_runtime",
        "giscode.agent_registry.service",
    ),
}

#: Navigation tab label -> feature id. A tab that is not listed is never built.
TAB_FEATURES: dict[str, str] = {
    "Chat": "chat",
    "Projects": "projects",
    "Terminal": "terminal",
    "Settings": "settings",
    "Model Manager": "model_manager",
    "History": "history_tab",
    "Status": "status_tab",
    "iOS / xtool": "ios_xtool",
    "GisLogo": "gislogo",
    "Developer & Release": "developer_release",
    "Device Preview": "device_preview",
    "Generate Video": "generate_video",
    "Project Doctor": "project_doctor",
    "Code Task": "code_task",
    "Analyze": "analyze_tab",
    "AdSpace": "adspace",
    "Agent Registry": "agent_registry",
}

#: Visible tab order per edition. Anything not in the tuple is not built.
CORE_TABS = ("Chat", "Projects", "Model Manager", "Settings", "AdSpace")
PRO_TABS = (
    "Chat",
    "Projects",
    "Terminal",
    "Settings",
    "Model Manager",
    "History",
    "Status",
    "iOS / xtool",
    "GisLogo",
    "Developer & Release",
    "Device Preview",
    "Generate Video",
    "Project Doctor",
    "Code Task",
    "Analyze",
    "Agent Registry",
)


#: Every tab label either edition can show, for validation rather than building.
ALL_TABS: tuple[str, ...] = tuple(dict.fromkeys(PRO_TABS + CORE_TABS))


def edition() -> str:
    """The running edition: ``"core"`` unless Pro was asked for explicitly."""
    raw = os.environ.get(EDITION_ENV, "").strip().lower()
    return PRO if raw in {"pro", "professional", "full"} else CORE


def is_pro() -> bool:
    return edition() == PRO


def is_core() -> bool:
    return edition() == CORE


def feature_enabled(feature: str) -> bool:
    """True when the running edition has *feature*.

    An unknown feature is disabled: a typo must not silently expose a surface,
    and a new tool is Pro until it is deliberately added to the shared set.
    """
    name = str(feature or "").strip()
    if name in SHARED_FEATURES:
        return True
    if name in CORE_ONLY_FEATURES:
        return is_core()
    if name in PRO_ONLY_FEATURES:
        return is_pro()
    return False


def tab_enabled(tab: str) -> bool:
    """True when *tab* belongs to the running edition."""
    feature = TAB_FEATURES.get(str(tab or ""))
    return bool(feature) and feature_enabled(feature)


def missing_implementation(feature: str) -> tuple[str, ...]:
    """Modules a surface needs that this installation does not contain.

    Empty for every feature that has no separate module, so a Core build is
    never affected by the check.
    """
    missing: list[str] = []
    for module in PRO_IMPLEMENTATION.get(str(feature or "").strip(), ()):
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError, AttributeError):
            found = False
        if not found:
            missing.append(module)
    return tuple(missing)


def implementation_available(feature: str) -> bool:
    """True when *feature*'s code is present in this tree.

    A public Core export ships without the Pro modules; the edition variable can
    still say ``pro`` (it is a preference, not a licence), so every caller that
    builds a surface checks this and degrades instead of raising.
    """
    return not missing_implementation(feature)


def tabs() -> tuple[str, ...]:
    """The navigation tabs this edition builds, in display order."""
    order = PRO_TABS if is_pro() else CORE_TABS
    return tuple(
        tab
        for tab in order
        if tab_enabled(tab) and implementation_available(TAB_FEATURES.get(tab, ""))
    )


def pro_only_tabs() -> tuple[str, ...]:
    """Tabs Core must not contain, for a test or a reviewer to assert against."""
    return tuple(tab for tab in PRO_TABS if tab not in CORE_TABS)


def unavailable_pro_features() -> tuple[str, ...]:
    """Pro surfaces this tree cannot build because their code is absent."""
    return tuple(
        sorted(
            feature
            for feature in PRO_ONLY_FEATURES
            if not implementation_available(feature)
        )
    )


def describe() -> str:
    """One line for a log or an about box."""
    if is_pro():
        if unavailable_pro_features():
            return (
                "GisCode Pro tools are not part of this build; the Core tools are "
                "running instead."
            )
        return "GisCode Pro"
    return "GisCode Core (free edition; advanced developer tools are in Pro)"
