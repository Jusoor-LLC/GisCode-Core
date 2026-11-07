"""Deterministic Core task execution, independent of GisAI.

The public Core edition ships without ``giscode.gisai``, so the work it can do
is the work that needs no model: parse the arguments, confine them to the open
project, execute through the same executor, guarded writer and snapshot manager
the agentic loop uses, then verify by reading the result back.

What Core can do is declared in :mod:`giscode.agentic.capabilities` - five
deterministic capabilities, two of which write and one of which needs a person's
approval. Everything else is refused with a distinct status (``model_required``,
``pro_only``, ``unsupported``, ``invalid_request``) and an explanation of what
would be needed, because Core does not guess and must never imply a model ran.

The module deliberately imports nothing from ``giscode.gisai``: the packaged
Core edition does not contain it, and the public Core dependency boundary must
stay free of the assistant package.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from giscode.agentic import capabilities
from giscode.agentic.actions import WorkspaceActionExecutor
from giscode.agentic.guarded_writer import unified_diff
from giscode.agentic.project_view import list_files, read_text_file, search_text
from giscode.agentic.types import ActionType, ExecutionStep
from giscode.agentic.write_safety import SnapshotManager
from giscode.workspace.workspace import Workspace

#: What Core can do without a model, in one line. Derived from the contract, so
#: a refusal message and the tests cannot drift from the implementation.
SUPPORTED_SUMMARY = capabilities.summary()

#: Capability ids, in contract order (re-exported for callers and tests).
SUPPORTED_CAPABILITIES = capabilities.CAPABILITY_IDS

#: How much file text a read result quotes in its human-readable report.
_REPORT_TEXT_CHARS = 4000


class CoreTaskError(Exception):
    """Base class for a Core task that could not be carried out."""


class Refused(CoreTaskError):
    """A request Core will not execute, carrying the contract's status."""

    status = capabilities.UNSUPPORTED
    capability = "unknown"

    def __init__(self, reason: str, *, status: str = "", capability: str = "") -> None:
        super().__init__(reason)
        if status:
            self.status = status
        if capability:
            self.capability = capability


class UnsupportedOperation(Refused):
    """The request needs capabilities this Core path does not have."""


class InvalidRequest(Refused):
    """A recognised capability with malformed or unsafe arguments."""

    status = capabilities.INVALID


class TaskCancelled(CoreTaskError):
    """The user stopped the task."""


class TaskTimedOut(CoreTaskError):
    """The task budget was exhausted."""


@dataclass(frozen=True)
class CoreOperation:
    """One create request: create *path* with exactly *content*."""

    path: str
    content: str


@dataclass(frozen=True)
class Operation:
    """One parsed request: which capability, and the arguments it needs."""

    capability: str
    path: str = ""
    content: Optional[str] = None
    term: str = ""
    find: str = ""
    replace: str = ""


@dataclass
class CoreTask:
    """The minimal task record the GUI needs to finish and report a Core task.

    It is duck-compatible with the few attributes ``MainWindow._finish_task``
    reads from an engine task (``task_id``, ``project_path``, ``status``), so
    the GUI report path stays single-sourced rather than duplicated.
    """

    task_id: str
    raw_request: str
    project_path: str
    status: str = "pending"
    metadata: dict = field(default_factory=dict)


_NAME_PATTERNS = (
    re.compile(r"\bfile\s+(?:named|called)\s+[`'\"]?([^`'\"\s,;]+)", re.IGNORECASE),
    re.compile(
        r"\b(?:create|write|add|make)\s+(?:a\s+|the\s+)?(?:new\s+)?file\s+"
        r"[`'\"]?([^`'\"\s,;]+)",
        re.IGNORECASE,
    ),
)

_CONTENT_PATTERNS = (
    re.compile(
        r"(?:contents?|content)\s*(?:must|should|has to|needs? to)?\s*"
        r"(?:be|equal)?\s*(?:exactly\s+)?(?:this\s+)?(?:single\s+)?(?:line)?\s*[:\-]\s*(.+)$",
        re.IGNORECASE,
    ),
    re.compile(r"\bwith\s+(?:the\s+)?(?:exact\s+)?contents?\s*[:\-]\s*(.+)$", re.IGNORECASE),
    re.compile(r"\bcontaining\s*[:\-]\s*(.+)$", re.IGNORECASE),
    re.compile(r"\bthat\s+says?\s*[:\-]\s*(.+)$", re.IGNORECASE),
)

_CREATE_VERB = re.compile(r"\b(create|write|add|make|put)\b", re.IGNORECASE)

_READ_PATTERNS = (
    re.compile(
        r"^\s*(?:please\s+)?(?:read|show|open|display|print|cat|view)\s+"
        r"(?:me\s+)?(?:the\s+)?(?:contents?\s+of\s+)?(?:the\s+file\s+)?(.+?)\s*[?.!]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat(?:'s| is)\s+in\s+(?:the\s+file\s+)?(.+?)\s*\??\s*$",
        re.IGNORECASE,
    ),
)

_LIST_PATTERNS = (
    re.compile(
        r"\blist\s+(?:the\s+)?(?:files|entries|folder|directory|contents)\b"
        r"(?:\s+(?:of|in|under|for)\s+(?:the\s+)?(?:folder\s+|directory\s+)?(.+?))?\s*[?.!]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat\s+files\s+(?:are\s+)?(?:in|under)\s+(.+?)\s*\??\s*$",
        re.IGNORECASE,
    ),
)

_SEARCH_PATTERNS = (
    re.compile(
        r"\b(?:search|find|grep|locate)\s+(?:for\s+)?(.+?)"
        r"(?:\s+(?:in|under|inside)\s+(?:the\s+)?(?:folder\s+)?(.+?))?\s*[?.!]?\s*$",
        re.IGNORECASE,
    ),
)

_REPLACE_PATTERNS = (
    re.compile(
        r"\breplace\s+(?:the\s+)?(?:text\s+)?[`'\"](?P<find>.+?)[`'\"]\s+with\s+"
        r"[`'\"](?P<replace>.*?)[`'\"]\s+in\s+(?:the\s+file\s+)?(?P<path>.+?)\s*[?.!]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\breplace\s+(?P<find>.+?)\s+with\s+(?P<replace>.+?)\s+in\s+"
        r"(?:the\s+file\s+)?(?P<path>\S+)\s*[?.!]?\s*$",
        re.IGNORECASE,
    ),
)

#: A read/list request whose "path" is really the word "files" is the other
#: capability; these words route it there instead of reading a file called
#: "files".
_COLLECTION_WORDS = ("files", "file list", "directory", "folder", "entries", "contents")


def _unwrap_quotes(value: str) -> str:
    text = value.strip()
    for quote in ('"', "'", "`"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            return text[1:-1]
    return text


def _clean_path(value: str) -> str:
    """Trim quoting and trailing punctuation, never a leading dot.

    ``strip(".,;:")`` turned ``../outside.py`` into ``/outside.py``, which
    reads as an absolute path inside the workspace - exactly the traversal
    the check below is meant to refuse.
    """
    text = _unwrap_quotes(str(value or "").strip())
    return text.rstrip(".,;:").strip()


def _parse_create(text: str) -> CoreOperation:
    """Parse a create-file request, or raise :class:`InvalidRequest`."""
    name = ""
    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            name = _clean_path(match.group(1))
            break
    if not name:
        raise InvalidRequest("no file name was found in the request")
    if ".." in Path(name).parts or name in {".", ".."}:
        raise InvalidRequest("the file name may not traverse outside the workspace")

    content = None
    for pattern in _CONTENT_PATTERNS:
        match = pattern.search(text)
        if match:
            content = _unwrap_quotes(match.group(1))
            break
    if content is None:
        raise InvalidRequest("the exact file content was not supplied")
    if not content:
        raise InvalidRequest("the supplied file content is empty")
    return CoreOperation(path=name, content=content)


def parse_request(message: str) -> Operation:
    """Decide which capability *message* asks for, and with which arguments.

    Parsing is narrow on purpose: a request that merely mentions a file is not
    a file task. A recognised capability with missing or unsafe arguments
    raises :class:`InvalidRequest`; a request no capability matches is
    classified by the contract into ``pro_only``, ``model_required`` or
    ``unsupported`` and refused with that status.
    """
    text = str(message or "").strip()
    if not text:
        raise InvalidRequest("the request is empty")

    # Replace first: its arguments contain arbitrary text that could otherwise
    # look like a create ("replace X with create Y in a.txt").
    for pattern in _REPLACE_PATTERNS:
        match = pattern.search(text)
        if match:
            path = _clean_path(match.group("path"))
            if not path:
                raise InvalidRequest("no file name was found in the request")
            if ".." in Path(path).parts or path in {".", ".."}:
                raise InvalidRequest("the file name may not traverse outside the workspace")
            find = _unwrap_quotes(match.group("find"))
            if not find:
                raise InvalidRequest("the text to replace is empty")
            return Operation(
                capability="replace_text",
                path=path,
                find=find,
                replace=_unwrap_quotes(match.group("replace")),
            )

    if _CREATE_VERB.search(text) and re.search(r"\bfile\b", text, re.IGNORECASE):
        operation = _parse_create(text)
        return Operation(
            capability="create_file", path=operation.path, content=operation.content
        )

    for pattern in _LIST_PATTERNS:
        match = pattern.search(text)
        if match:
            path = _clean_path(match.group(1) or "")
            if path.lower() in {"the project", "this project", "here", "the project root"}:
                path = ""
            return Operation(capability="list_files", path=path)

    for pattern in _READ_PATTERNS:
        match = pattern.search(text)
        if match:
            path = _clean_path(match.group(1))
            if not path:
                raise InvalidRequest("no file name was found in the request")
            if path.lower() in _COLLECTION_WORDS:
                # "show the files" is a listing, not a read of a file named
                # "files"; fall through to the search/list classification.
                continue
            if ".." in Path(path).parts:
                raise InvalidRequest("the file name may not traverse outside the workspace")
            return Operation(capability="read_file", path=path)

    for pattern in _SEARCH_PATTERNS:
        match = pattern.search(text)
        if match:
            term = _unwrap_quotes(match.group(1))
            if not term:
                raise InvalidRequest("no search term was found in the request")
            folder = _clean_path(match.group(2) or "")
            if folder.lower() in {"the project", "this project", "here"}:
                folder = ""
            return Operation(capability="search_text", term=term, path=folder)

    status, capability, reason = capabilities.classify(text)
    raise UnsupportedOperation(reason, status=status, capability=capability)


def parse_operation(message: str) -> CoreOperation:
    """Parse a *create-file* request, or raise :class:`UnsupportedOperation`.

    Kept as the single-operation entry point for existing callers and tests.
    Parsing is narrow on purpose. A request that merely *contains* a filename
    is not treated as a file-creation task; it must also ask to create or write
    it and supply the exact content.
    """
    text = str(message or "").strip()
    if not text:
        raise InvalidRequest("the request is empty")
    if not _CREATE_VERB.search(text) or not re.search(r"\bfile\b", text, re.IGNORECASE):
        status, capability, reason = capabilities.classify(text)
        raise UnsupportedOperation(reason, status=status, capability=capability)
    try:
        return _parse_create(text)
    except InvalidRequest as exc:
        raise UnsupportedOperation(str(exc), status=capabilities.UNSUPPORTED) from exc


def request_needs_workspace(message: str) -> bool:
    """True when *message* names a file or folder operation Core performs.

    Used before a project is selected: Core has no intent classifier, so the
    answer comes from the dispatcher itself. A request it refuses is not a
    reason to ask for a workspace, except for the literal create-style wording
    that names a file, which needs one even when the content is missing.
    """
    text = str(message or "")
    try:
        parse_request(text)
        return True
    except Refused:
        pass
    if any(pattern.search(text) for pattern in _NAME_PATTERNS):
        return True
    return bool(_CREATE_VERB.search(text) and re.search(r"\bfile\b", text, re.IGNORECASE))


def verify_written_file(path: Path, expected: str) -> tuple[bool, str, int]:
    """Read the file back and compare bytes, never the write call's claim.

    Returns ``(ok, sha256_hex, byte_count)``. ``sha256_hex`` is empty when the
    file is missing or unreadable, so a caller cannot report a hash it did not
    compute from disk.
    """
    try:
        if not path.is_file():
            return False, "", 0
        data = path.read_bytes()
    except OSError:
        return False, "", 0
    digest = hashlib.sha256(data).hexdigest()
    try:
        actual = data.decode("utf-8")
    except UnicodeDecodeError:
        return False, digest, len(data)
    return _same_text(actual, expected), digest, len(data)


def _same_text(actual: str, expected: str) -> bool:
    """Compare text ignoring the platform's newline translation.

    ``Path.write_text`` translates ``\n`` to ``\r\n`` on Windows, so a
    multiline write is correct on disk while differing from the caller's
    string byte-for-byte. Newlines are normalised; every other character
    must still match exactly.
    """
    if actual == expected:
        return True
    return actual.replace("\r\n", "\n") == expected.replace("\r\n", "\n")


def _result(
    *,
    task_id: str,
    capability: str,
    status: str,
    response: str,
    error: Optional[str],
    work_result: dict,
) -> dict:
    return {
        "task_id": task_id,
        "capability": capability,
        "status": status,
        "success": error is None and status == capabilities.COMPLETED,
        "response": response,
        "coding_result": response,
        "error": error,
        "work_result": work_result,
    }


def _work_result(
    capability: str,
    status: str,
    *,
    actions: Optional[list] = None,
    verification: Optional[dict] = None,
    observations: Optional[list] = None,
    error: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    result = {
        "capability": capability,
        "status": status,
        "actions": actions or [],
        "verification": verification or {},
        "observations": observations or [],
        "error": error,
        "failure_reason": error if status != capabilities.COMPLETED else None,
        "repair_attempts": 0,
    }
    if extra:
        result.update(extra)
    return result


def _failure(
    task_id: str,
    capability: str,
    reason: str,
    *,
    status: str = capabilities.FAILED,
    actions: Optional[list] = None,
    verification: Optional[dict] = None,
    observations: Optional[list] = None,
    extra: Optional[dict] = None,
) -> dict:
    work_result = _work_result(
        capability,
        status,
        actions=actions,
        verification=verification,
        observations=observations,
        error=reason,
        extra=extra,
    )
    return _result(
        task_id=task_id,
        capability=capability,
        status=status,
        response=f"Core task failed: {reason}\n\nNothing was changed by this task.",
        error=reason,
        work_result=work_result,
    )


def _refused(task_id: str, capability: str, status: str, reason: str, root: str = "") -> dict:
    work_result = _work_result(capability, status, error=reason)
    return _result(
        task_id=task_id,
        capability=capability,
        status=status,
        response=capabilities.refusal_message(status, capability, reason, root=root),
        error=f"Refused by Core ({status}): {reason}",
        work_result=work_result,
    )


def _verify_read_back(target: Path, expected: str) -> tuple[bool, dict]:
    ok, digest, size = verify_written_file(target, expected)
    verification = {
        "success": bool(ok),
        "errors": [] if ok else ["the file on disk does not match the expected content"],
        "details": [f"sha256={digest or 'unavailable'}", f"bytes={size}"],
        "test_execution_status": "unavailable",
    }
    return ok, verification


def _model_selected(router) -> bool:
    """True when a profile or a legacy model setting actually names a model.

    With nothing selected, Core keeps its existing refusal: a user who has not
    chosen a model should read the contract, not "provider unreachable".
    """
    if router is None:
        return False
    target_fn = getattr(router, "active_target", None)
    if not callable(target_fn):
        return True
    try:
        target = target_fn()
    except Exception:
        return False
    return bool(getattr(target, "profile_id", "") or getattr(target, "model", ""))


def _should_ask_the_model(status: str, capability: str, router) -> bool:
    """Route only the two cases a model can answer honestly.

    ``pro_only`` and a named ``unsupported`` capability (run the tests, delete a
    file, commit) stay refused even with a model connected: connecting a model
    must never widen what Core is allowed to touch.
    """
    if status == capabilities.PRO_ONLY:
        return False
    if status not in (capabilities.MODEL_REQUIRED, capabilities.UNSUPPORTED):
        return False
    if status == capabilities.UNSUPPORTED and capability != "unknown":
        return False
    return _model_selected(router)


def execute_request(
    message: str,
    project_root: str,
    *,
    task_id: str = "core-task",
    should_cancel: Optional[Callable[[], bool]] = None,
    deadline_s: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    approve: Optional[Callable[[Path, str], bool]] = None,
    model_router: Optional[object] = None,
    model_purpose: str = "chat",
) -> dict:
    """Run one supported deterministic request in *project_root*.

    Returns a result dictionary with the same keys the GUI already consumes
    (``response``, ``error``, ``work_result``), plus ``capability`` and the
    contract ``status``. Success is possible only when the capability's own
    verification passed and nothing was rolled back.

    ``approve`` is the host's diff dialog. A capability that requires approval
    does not run without it, and a rejected diff changes nothing.
    """
    cancelled = should_cancel or (lambda: False)
    started = clock()
    deadline = None if deadline_s is None else started + max(0.0, float(deadline_s))

    def check() -> None:
        if cancelled():
            raise TaskCancelled("task cancelled")
        if deadline is not None and clock() > deadline:
            raise TaskTimedOut("task exceeded its time limit")

    try:
        check()
        operation = parse_request(message)
    except Refused as exc:
        capability = getattr(exc, "capability", "unknown")
        status = getattr(exc, "status", capabilities.UNSUPPORTED)
        if _should_ask_the_model(status, capability, model_router):
            from giscode.agentic.model_chat import answer_with_model

            return answer_with_model(
                message,
                project_root,
                router=model_router,
                task_id=task_id,
                should_cancel=cancelled,
                deadline_s=deadline_s,
                purpose=model_purpose,
            )
        return _refused(
            task_id,
            capability,
            status,
            str(exc),
        )
    except TaskCancelled as exc:
        return _failure(task_id, "unknown", str(exc), status=capabilities.CANCELLED)
    except TaskTimedOut as exc:
        return _failure(task_id, "unknown", str(exc), status=capabilities.TIMED_OUT)

    root = Path(project_root).expanduser().resolve() if project_root else None
    if root is None or not root.is_dir():
        return _failure(
            task_id, operation.capability, "no project folder is open", status=capabilities.FAILED
        )

    handler = _HANDLERS.get(operation.capability)
    if handler is None:  # pragma: no cover - the contract and handlers are paired by test
        return _failure(
            task_id,
            operation.capability,
            "this build has no executor for that capability",
            status=capabilities.UNSUPPORTED,
        )
    try:
        return handler(
            operation,
            root=root,
            task_id=task_id,
            check=check,
            approve=approve,
        )
    except TaskCancelled as exc:
        return _failure(task_id, operation.capability, str(exc), status=capabilities.CANCELLED)
    except TaskTimedOut as exc:
        return _failure(task_id, operation.capability, str(exc), status=capabilities.TIMED_OUT)


# --------------------------------------------------------------- create_file
def _run_create(operation: Operation, *, root: Path, task_id: str, check, approve, **_) -> dict:
    workspace = Workspace(root, ensure_structure=False)
    snapshot = SnapshotManager(task_id)
    executor = WorkspaceActionExecutor(workspace)
    # A new file cannot destroy anything, so creation keeps its existing
    # no-approval policy; existing-file writes below always ask.
    executor.configure_write_safety(snapshot, approval_callback=None)
    step = ExecutionStep(
        step_id=f"{task_id}-1",
        description="Create the requested file with its exact content",
        action_type=ActionType.CREATE,
        target_path=operation.path,
        content=operation.content or "",
        expected_result="the file exists with the requested bytes",
    )

    action = executor.execute(step)
    action_record = _action_record(operation.path, action, ActionType.CREATE)
    if not action.success:
        return _failure(
            task_id,
            "create_file",
            action.error or "the file could not be created",
            actions=[action_record],
        )

    target = Path(action.target_path)
    try:
        check()
    except (TaskCancelled, TaskTimedOut) as exc:
        snapshot.rollback(root)
        return _failure(
            task_id,
            "create_file",
            str(exc),
            status=capabilities.CANCELLED
            if isinstance(exc, TaskCancelled)
            else capabilities.TIMED_OUT,
            actions=[action_record],
            observations=["rollback: the file created by this task was removed"],
        )

    ok, verification = _verify_read_back(target, operation.content or "")
    if not ok:
        snapshot.rollback(root)
        return _failure(
            task_id,
            "create_file",
            "verification failed: the file on disk does not match the requested content",
            actions=[action_record],
            verification=verification,
            observations=["rollback: the file created by this task was removed"],
        )

    relative = _relative(target, root)
    digest = verification["details"][0].split("=", 1)[1]
    size = target.stat().st_size
    verification["details"] = [
        f"path={target}",
        f"sha256={digest}",
        f"bytes={size}",
        "content matches the requested text exactly",
    ]
    observations = ["verification: independent read-back matched the requested bytes"]
    report = "\n".join(
        [
            "Completed: created 1 file.",
            f"Capability: create_file",
            "",
            f"Created: {relative}",
            f"Path: {target}",
            f"Bytes: {size}",
            f"SHA-256: {digest}",
            "Verification: PASSED - the file exists and its content matches the "
            "requested text exactly.",
            f"Workspace: {root}",
            "Executed deterministically by Core; no model was used.",
        ]
    )
    return _result(
        task_id=task_id,
        capability="create_file",
        status=capabilities.COMPLETED,
        response=report,
        error=None,
        work_result=_work_result(
            "create_file",
            capabilities.COMPLETED,
            actions=[action_record],
            verification=verification,
            observations=observations,
        ),
    )


# ----------------------------------------------------------------- read_file
_READ_FAILURES = {
    "missing": capabilities.FAILED,
    "denied": capabilities.INVALID,
    "directory": capabilities.INVALID,
    "binary": capabilities.FAILED,
    "too_large": capabilities.FAILED,
    "unreadable": capabilities.FAILED,
}


def _run_read(operation: Operation, *, root: Path, task_id: str, check, approve, **_) -> dict:
    check()
    view = read_text_file(root, operation.path)
    if view["status"] != "ok":
        status = _READ_FAILURES.get(view["status"], capabilities.FAILED)
        hint = ""
        if view["status"] == "directory":
            hint = " Use list_files for a folder."
        return _failure(
            task_id,
            "read_file",
            f"{view['error']}.{hint}".strip(),
            status=status,
        )

    text = view["text"]
    quoted = text if len(text) <= _REPORT_TEXT_CHARS else (
        text[:_REPORT_TEXT_CHARS] + "\n... [content clipped in this report]"
    )
    verification = {
        "success": True,
        "errors": [],
        "details": [
            f"path={view['path']}",
            f"sha256={view['sha256']}",
            f"bytes={view['bytes']}",
            f"lines={view['lines']}",
            "content read back from disk",
        ],
        "test_execution_status": "unavailable",
    }
    observations = [
        "read-only: nothing was written",
        f"sha256 recomputed from disk: {view['sha256']}",
    ]
    report = "\n".join(
        [
            "Completed: read 1 file.",
            "Capability: read_file",
            "",
            f"Path: {view['path']}",
            f"Bytes: {view['bytes']}",
            f"Lines: {view['lines']}",
            f"SHA-256: {view['sha256']}",
            f"Workspace: {root}",
            "Executed deterministically by Core; no model was used.",
            "",
            "--- content ---",
            quoted,
        ]
    )
    return _result(
        task_id=task_id,
        capability="read_file",
        status=capabilities.COMPLETED,
        response=report,
        error=None,
        work_result=_work_result(
            "read_file",
            capabilities.COMPLETED,
            verification=verification,
            observations=observations,
            extra={"path": view["path"], "bytes": view["bytes"], "sha256": view["sha256"]},
        ),
    )


# ---------------------------------------------------------------- list_files
def _run_list(operation: Operation, *, root: Path, task_id: str, check, approve, **_) -> dict:
    check()
    view = list_files(root, operation.path)
    if view["status"] != "ok":
        status = capabilities.INVALID if view["status"] in {"denied", "not_a_directory"} else capabilities.FAILED
        return _failure(task_id, "list_files", view["error"], status=status)

    entries = view["entries"]
    lines = [
        f"Completed: listed {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}.",
        "Capability: list_files",
        "",
        f"Folder: {view['path'] or '.'}",
    ]
    for entry in entries:
        if entry["kind"] == "dir":
            lines.append(f"  [dir]  {entry['path']}/")
        else:
            lines.append(f"  [file] {entry['path']} ({entry['bytes']} bytes)")
    if view["truncated"]:
        lines.append("  ... [listing truncated; narrow the folder to see more]")
    lines.append(f"Workspace: {root}")
    lines.append("Executed deterministically by Core; no model was used.")

    verification = {
        "success": True,
        "errors": [],
        "details": [
            f"folder={view['path'] or '.'}",
            f"entries={len(entries)}",
            f"truncated={view['truncated']}",
        ],
        "test_execution_status": "unavailable",
    }
    return _result(
        task_id=task_id,
        capability="list_files",
        status=capabilities.COMPLETED,
        response="\n".join(lines),
        error=None,
        work_result=_work_result(
            "list_files",
            capabilities.COMPLETED,
            verification=verification,
            observations=["read-only: nothing was written"],
            extra={"entries": entries, "truncated": view["truncated"]},
        ),
    )


# --------------------------------------------------------------- search_text
def _run_search(operation: Operation, *, root: Path, task_id: str, check, approve, **_) -> dict:
    check()
    view = search_text(root, operation.term, operation.path)
    if view["status"] != "ok":
        status = capabilities.INVALID if view["status"] in {"denied", "empty_term"} else capabilities.FAILED
        return _failure(task_id, "search_text", view["error"], status=status)

    matches = view["matches"]
    lines = [
        f"Completed: {len(matches)} match{'es' if len(matches) != 1 else ''} for "
        f"{view['term']!r} in {view['files_scanned']} file(s) scanned.",
        "Capability: search_text",
        "",
        f"Term: {view['term']}",
        f"Folder: {view['path'] or '.'}",
    ]
    if not matches:
        lines.append("No file in the project contains that literal text.")
    for match in matches:
        lines.append(f"  {match['path']}:{match['line']}: {match['text']}")
    if view["truncated"]:
        lines.append("  ... [results truncated; refine the term or the folder]")
    lines.append(f"Workspace: {root}")
    lines.append("Executed deterministically by Core; no model was used.")

    verification = {
        "success": True,
        "errors": [],
        "details": [
            f"term={view['term']}",
            f"matches={len(matches)}",
            f"files_scanned={view['files_scanned']}",
            f"truncated={view['truncated']}",
        ],
        "test_execution_status": "unavailable",
    }
    return _result(
        task_id=task_id,
        capability="search_text",
        status=capabilities.COMPLETED,
        response="\n".join(lines),
        error=None,
        work_result=_work_result(
            "search_text",
            capabilities.COMPLETED,
            verification=verification,
            observations=["read-only: nothing was written"],
            extra={"matches": matches, "truncated": view["truncated"]},
        ),
    )


# --------------------------------------------------------------- replace_text
def _run_replace(operation: Operation, *, root: Path, task_id: str, check, approve, **_) -> dict:
    check()
    view = read_text_file(root, operation.path)
    if view["status"] != "ok":
        status = capabilities.INVALID if view["status"] in {"denied", "directory"} else capabilities.FAILED
        return _failure(task_id, "replace_text", view["error"], status=status)

    # Replace inside LF text: ``Path.write_text`` translates "\n" to the
    # platform newline, so editing text that already contains "\r\n" would
    # double every line break on Windows.
    before = view["text"].replace("\r\n", "\n")
    find = operation.find.replace("\r\n", "\n")
    replacement = operation.replace.replace("\r\n", "\n")
    occurrences = before.count(find)
    if occurrences == 0:
        return _failure(
            task_id,
            "replace_text",
            f"the text {operation.find!r} does not appear in {operation.path}",
            status=capabilities.INVALID,
        )
    if occurrences > 1:
        return _failure(
            task_id,
            "replace_text",
            (
                f"the text {find!r} appears {occurrences} times in "
                f"{operation.path}; Core replaces exactly one occurrence"
            ),
            status=capabilities.INVALID,
        )
    expected = before.replace(find, replacement, 1)
    if expected == before:
        return _failure(
            task_id,
            "replace_text",
            "the replacement is identical to the existing text, so nothing would change",
            status=capabilities.INVALID,
        )

    target = (root / operation.path).resolve()
    diff = unified_diff(target, before, expected)
    if approve is None:
        return _failure(
            task_id,
            "replace_text",
            (
                "this capability edits an existing file, so it needs an approval "
                "dialog and none is connected"
            ),
            status=capabilities.APPROVAL_REQUIRED,
        )
    try:
        accepted = bool(approve(target, diff))
    except Exception as exc:  # a broken dialog must fail closed
        return _failure(
            task_id,
            "replace_text",
            f"the approval dialog failed, so nothing was changed: {exc}",
            status=capabilities.APPROVAL_REQUIRED,
        )
    if not accepted:
        return _failure(
            task_id,
            "replace_text",
            "the edit was not approved, so nothing was changed",
            status=capabilities.REJECTED,
        )

    snapshot = SnapshotManager(task_id)
    executor = WorkspaceActionExecutor(Workspace(root, ensure_structure=False))
    # The diff above is the approval; the guarded writer is told so explicitly
    # instead of opening a second dialog for the same edit.
    executor.configure_write_safety(
        snapshot, approval_callback=None, auto_approve_existing=True
    )
    step = ExecutionStep(
        step_id=f"{task_id}-1",
        description="Replace the approved occurrence",
        action_type=ActionType.MODIFY,
        target_path=operation.path,
        content=expected,
        expected_result="the file contains the approved replacement",
    )
    action = executor.execute(step)
    action_record = _action_record(operation.path, action, ActionType.MODIFY)
    if not action.success:
        return _failure(
            task_id,
            "replace_text",
            action.error or "the file could not be updated",
            actions=[action_record],
        )

    try:
        check()
    except (TaskCancelled, TaskTimedOut) as exc:
        snapshot.rollback(root)
        return _failure(
            task_id,
            "replace_text",
            str(exc),
            status=capabilities.CANCELLED
            if isinstance(exc, TaskCancelled)
            else capabilities.TIMED_OUT,
            actions=[action_record],
            observations=["rollback: the approved edit was reverted"],
        )

    ok, verification = _verify_read_back(Path(action.target_path), expected)
    if not ok:
        snapshot.rollback(root)
        return _failure(
            task_id,
            "replace_text",
            "verification failed: the file on disk does not match the approved content",
            actions=[action_record],
            verification=verification,
            observations=["rollback: the approved edit was reverted"],
        )

    digest = verification["details"][0].split("=", 1)[1]
    size = Path(action.target_path).stat().st_size
    relative = _relative(Path(action.target_path), root)
    verification["details"] = [
        f"path={action.target_path}",
        f"sha256={digest}",
        f"bytes={size}",
        "content matches the approved replacement exactly",
    ]
    report = "\n".join(
        [
            "Completed: replaced 1 occurrence in 1 file.",
            "Capability: replace_text",
            "",
            f"Edited: {relative}",
            f"Path: {action.target_path}",
            f"Replaced: {operation.find}",
            f"With: {operation.replace}",
            f"Bytes: {size}",
            f"SHA-256: {digest}",
            "Verification: PASSED - the file on disk matches the approved content.",
            f"Workspace: {root}",
            "Executed deterministically by Core; no model was used. A pre-edit "
            "snapshot was recorded for rollback.",
        ]
    )
    return _result(
        task_id=task_id,
        capability="replace_text",
        status=capabilities.COMPLETED,
        response=report,
        error=None,
        work_result=_work_result(
            "replace_text",
            capabilities.COMPLETED,
            actions=[action_record],
            verification=verification,
            observations=[
                "verification: independent read-back matched the approved bytes",
                "snapshot: the pre-edit file was recorded for rollback",
            ],
        ),
    )


def _action_record(relative: str, action, action_type: ActionType) -> dict:
    return {
        "tool": "write_file" if action_type in {ActionType.CREATE, ActionType.MODIFY} else "read_file",
        "args": {"path": relative},
        "success": bool(action.success),
        "original_content": getattr(action, "original_content", None),
        "error": action.error,
    }


def _relative(target: Path, root: Path) -> str:
    try:
        return target.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(target)


#: One executor per declared capability. A test asserts the keys match
#: ``capabilities.ids()`` exactly, so a declared capability cannot be a stub.
_HANDLERS = {
    "create_file": _run_create,
    "read_file": _run_read,
    "list_files": _run_list,
    "search_text": _run_search,
    "replace_text": _run_replace,
}
