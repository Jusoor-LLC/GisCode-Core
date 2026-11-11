"""The Core capability contract: what Core can do with no model at all.

The public Core edition ships without ``giscode.gisai``, so every request it
accepts has to be deterministic: parse the arguments, confine them to the open
project, execute through the same executor, guarded writer and snapshot manager
the agentic loop uses, then verify by reading the result back.

This module is the *declaration* of that set - ids, inputs, whether a
capability writes, whether it needs a person to approve it, and whether it needs
a model. It also classifies the requests Core must refuse, so "unsupported"
means one specific thing instead of "a regex did not match". Execution lives in
``core_tasks``; read-only inspection lives in ``project_view``.

Nothing here imports ``giscode.gisai``: the packaged Core edition does not
contain it, and the public dependency boundary must stay free of the assistant
package. The contract is data, so the refusal message, the GUI hint and the
tests cannot drift from the implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Statuses a Core request can end in. ``COMPLETED`` is the only success.
COMPLETED = "completed"
FAILED = "failed"
INVALID = "invalid_request"
UNSUPPORTED = "unsupported"
MODEL_REQUIRED = "model_required"
PRO_ONLY = "pro_only"
APPROVAL_REQUIRED = "approval_required"
REJECTED = "rejected"
CANCELLED = "cancelled"
TIMED_OUT = "timed_out"

#: Every non-success status, for a caller that needs to report one.
FAILURE_STATUSES: tuple[str, ...] = (
    FAILED,
    INVALID,
    UNSUPPORTED,
    MODEL_REQUIRED,
    PRO_ONLY,
    APPROVAL_REQUIRED,
    REJECTED,
    CANCELLED,
    TIMED_OUT,
)


@dataclass(frozen=True)
class Capability:
    """One declared Core capability.

    ``inputs`` names the arguments the parser must supply; a capability whose
    inputs are not all present is an ``invalid_request``, never a partial run.
    """

    id: str
    title: str
    summary: str
    inputs: tuple[str, ...]
    writes: bool
    requires_approval: bool
    needs_model: bool
    example: str


#: The deterministic capabilities Core actually performs. Order is the order the
#: contract table, the CLI hint and the docs use.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="create_file",
        title="Create a file",
        summary="create one new text file whose exact content is supplied",
        inputs=("path", "content"),
        writes=True,
        requires_approval=False,
        needs_model=False,
        example=(
            "Create a new file named notes.txt. Its contents must be exactly: "
            "first line"
        ),
    ),
    Capability(
        id="read_file",
        title="Read a file",
        summary="show one file's text, size, line count and SHA-256",
        inputs=("path",),
        writes=False,
        requires_approval=False,
        needs_model=False,
        example="Read notes.txt",
    ),
    Capability(
        id="list_files",
        title="List files",
        summary="list the entries of a project folder, sorted and bounded",
        inputs=("path",),
        writes=False,
        requires_approval=False,
        needs_model=False,
        example="List the files in src",
    ),
    Capability(
        id="search_text",
        title="Search text",
        summary="find a literal term in the project's text files, bounded",
        inputs=("term", "path"),
        writes=False,
        requires_approval=False,
        needs_model=False,
        example="Search for TODO in src",
    ),
    Capability(
        id="replace_text",
        title="Replace text in one file",
        summary=(
            "replace exactly one literal occurrence in an existing text file, "
            "after the diff is approved"
        ),
        inputs=("path", "find", "replace"),
        writes=True,
        requires_approval=True,
        needs_model=False,
        example='Replace "old name" with "new name" in src/app.py',
    ),
)

#: Capability ids, in contract order. A stable tuple, not a dict view.
CAPABILITY_IDS: tuple[str, ...] = tuple(item.id for item in CAPABILITIES)

_BY_ID = {item.id: item for item in CAPABILITIES}

#: Ids of capabilities that write. Kept explicit so a reviewer can grep it.
WRITING_CAPABILITIES: tuple[str, ...] = tuple(
    item.id for item in CAPABILITIES if item.writes
)

#: Ids of capabilities that cannot run without a person approving the diff.
APPROVAL_CAPABILITIES: tuple[str, ...] = tuple(
    item.id for item in CAPABILITIES if item.requires_approval
)

#: Pro surfaces a Core request may name. Core refuses them by name and imports
#: none of the modules behind them.
PRO_ONLY_CAPABILITIES: dict[str, str] = {
    "terminal": "run shell commands in the Terminal tab",
    "ios_xtool": "build and install iOS apps with xtool",
    "gislogo": "generate brand logos with GisLogo",
    "release_package": "package, sign or publish a release",
    "device_preview": "preview a device or emulator",
    "generate_video": "generate a video",
    "project_doctor": "run a project health audit",
    "hosted_writer": "use the hosted writer",
    "self_improvement": "change GisAI's own self-improvement settings",
}

#: Generative work that needs a model and the GisAI planner/engine. Core has
#: neither, so these can only be refused - never approximated.
MODEL_REQUIRED_CAPABILITIES: dict[str, str] = {
    "explain": "explain or summarise code",
    "fix": "fix, repair or debug code",
    "refactor": "refactor or restructure code",
    "review": "review or improve code",
    "rename": "choose new names or rename symbols across a project",
    "generate": "write code that the request does not spell out",
    "plan": "plan a multi-step change",
}

#: Deterministic work Core does not implement. Destructive operations are here
#: deliberately: Core has no approval surface for them yet.
UNSUPPORTED_CAPABILITIES: dict[str, str] = {
    "delete": "delete a file or folder",
    "move": "move or copy a file",
    "run_tests": "run a project's tests",
    "run_build": "build or install dependencies",
    "vcs": "commit, push or publish",
}

_PRO_ONLY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("terminal", re.compile(r"\b(terminal|shell command|command prompt|powershell|cmd\.exe)\b", re.I)),
    ("ios_xtool", re.compile(r"\b(ios|xtool|iphone|ipad|testflight)\b", re.I)),
    ("gislogo", re.compile(r"\b(gis\s?logo|logo generator)\b", re.I)),
    (
        "release_package",
        re.compile(r"\b(package|packaging|sign|publish|release build|installer|pyinstaller)\b", re.I),
    ),
    ("device_preview", re.compile(r"\b(device preview|emulator|simulator)\b", re.I)),
    ("generate_video", re.compile(r"\b(generate (a )?video|make (a )?video|render (a )?video)\b", re.I)),
    ("project_doctor", re.compile(r"\bproject doctor\b", re.I)),
    ("hosted_writer", re.compile(r"\bhosted writer\b", re.I)),
    ("self_improvement", re.compile(r"\bself[- ]improvement\b", re.I)),
)

_MODEL_REQUIRED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("refactor", re.compile(r"\brefactor|\brestructur|\brewrite\b", re.I)),
    ("fix", re.compile(r"\bfix\b|\brepair\b|\bdebug\b|\bbroken\b|\bregression\b", re.I)),
    ("explain", re.compile(r"\bexplain\b|\bsummar(y|ise|ize)\b|\bwhat does\b|\bwhy\b", re.I)),
    ("review", re.compile(r"\breview\b|\bimprove\b|\boptimi[sz]e\b|\bclean up\b", re.I)),
    ("rename", re.compile(r"\brename\b|\bchoose (a )?name\b", re.I)),
    (
        "generate",
        re.compile(
            r"\bgenerate\b|\bscaffold\b|\bboilerplate\b|"
            r"\b(?:add|create|implement|write|update|extend|build|make)\b",
            re.I,
        ),
    ),
    ("plan", re.compile(r"\bplan\b|\bdesign\b|\barchitect\b", re.I)),
)

_UNSUPPORTED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("delete", re.compile(r"\bdelete\b|\bremove\b", re.I)),
    ("move", re.compile(r"\bmove\b|\bcopy\b|\brename the file\b", re.I)),
    ("run_tests", re.compile(r"\brun (the )?tests?\b|\bpytest\b|\bunit tests?\b", re.I)),
    ("run_build", re.compile(r"\binstall\b|\bupgrade\b|\bdownload\b|\bbuild the project\b", re.I)),
    ("vcs", re.compile(r"\bcommit\b|\bpush\b|\bmerge\b", re.I)),
)


def by_id(capability_id: str) -> Capability | None:
    """The declared capability with *capability_id*, or ``None``."""
    return _BY_ID.get(str(capability_id or "").strip())


def ids() -> tuple[str, ...]:
    """Capability ids, in contract order."""
    return CAPABILITY_IDS


def summary() -> str:
    """One line naming what Core can do, for a refusal message or a hint."""
    parts = [f"{item.id} ({item.title.lower()})" for item in CAPABILITIES]
    return ", ".join(parts)


def contract_table() -> str:
    """A plain-text table of the contract, for docs and for a test to compare."""
    rows = [
        "| Capability | What it does | Writes | Approval | Model |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in CAPABILITIES:
        rows.append(
            "| `{}` | {} | {} | {} | {} |".format(
                item.id,
                item.summary,
                "yes" if item.writes else "no",
                "required" if item.requires_approval else "none",
                "required" if item.needs_model else "none",
            )
        )
    return "\n".join(rows)


def classify(text: str) -> tuple[str, str, str]:
    """Classify a request Core will not perform.

    Returns ``(status, capability, reason)`` where ``status`` is one of
    :data:`PRO_ONLY`, :data:`MODEL_REQUIRED` or :data:`UNSUPPORTED`. Order
    matters: a Pro surface named in the request wins, because "package the
    release" also contains no generative verb and "generate a video" contains
    one.
    """
    request = str(text or "")
    for name, pattern in _PRO_ONLY_PATTERNS:
        if pattern.search(request):
            return (
                PRO_ONLY,
                name,
                f"{PRO_ONLY_CAPABILITIES[name]} is a GisCode Pro capability",
            )
    for name, pattern in _MODEL_REQUIRED_PATTERNS:
        if pattern.search(request):
            return (
                MODEL_REQUIRED,
                name,
                f"this request needs a model to {MODEL_REQUIRED_CAPABILITIES[name]}",
            )
    for name, pattern in _UNSUPPORTED_PATTERNS:
        if pattern.search(request):
            return (
                UNSUPPORTED,
                name,
                f"Core has no supported way to {UNSUPPORTED_CAPABILITIES[name]}",
            )
    return (UNSUPPORTED, "unknown", "no supported Core capability matches this request")


def refusal_message(status: str, capability: str, reason: str, *, root: str = "") -> str:
    """The explanation a refused request gets. Never claims work was done."""
    lines = [
        "GisCode Core cannot carry out that request without GisAI.",
        "",
        f"Supported here: {summary()}.",
        f"Request status: {status} ({capability}).",
        f"Reason: {reason}.",
        "",
    ]
    if status == PRO_ONLY:
        lines.append(
            "GisCode Pro adds this surface. The public Core build does not "
            "contain its code, so it cannot be started from here - installing "
            "Pro is the supported route."
        )
    elif status == MODEL_REQUIRED:
        lines.append(
            "This needs a language model plus the GisAI engine, which the "
            "public Core build does not include. GisCode Pro performs it; Core "
            "will not guess at it with a heuristic."
        )
    else:
        lines.append(
            "No deterministic Core operation performs it, and Core does not "
            "improvise."
        )
    lines.append("Nothing was changed.")
    if root:
        lines.append(f"Workspace: {root}")
    return "\n".join(lines)
